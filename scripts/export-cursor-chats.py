#!/usr/bin/env python3
"""Export Cursor AI chat history (state.vscdb) to JSONL files for ocbrain harvest.

Cursor stores per-workspace chat state in
``~/Library/Application Support/Cursor/User/workspaceStorage/<hash>/state.vscdb``
(SQLite), which ocbrain's file-based harvest cannot read directly. This exporter
renders each workspace's AI chat to
``~/.ocbrain/exports/cursor/cursor-<workspace_hash>.jsonl`` — a path containing
``cursor`` so ``history_runtime()`` attributes it to the ``cursor`` runtime.

Sources per workspace DB (all optional; schema varies by Cursor version):
  - ItemTable key ``aiService.prompts``      — user prompts (JSON array)
  - ItemTable key ``aiService.generations``  — assistant generations (JSON array)
  - cursorDiskKV ``bubbleId:*`` / ``composerData:*`` — newer composer bubbles

Composer chats are no longer workspace-local, which is where nearly all of the
volume went. ``cursorDiskKV`` exists in every workspace DB on this machine and
is empty in all ten; the bubbles live in the single
``globalStorage/state.vscdb`` (7.8GB, 215,696 bubbles, 2.10GB of JSON), each
attributed to a workspace through ``composerHeaders.workspaceId``. This
exporter sweeps that store too and merges the result into the owning
workspace's file, so a workspace whose local tables are empty is no longer
reported as idle when it holds months of chat.

Three things keep that sweep affordable enough for a 15-minute cron:

  - Range scans, never ``LIKE``. ``key LIKE 'bubbleId:<id>:%'`` plans as SCAN
    over every key in the store; the equivalent ``key >= 'bubbleId:<id>:' AND
    key < 'bubbleId:<id>;'`` plans as SEARCH on the covering index. For the
    same 22 rows on the live store that is 0.1-0.8ms against 1.1s warm and
    15.3s cold.
  - Newest first, and stop once a workspace has enough. Composers are walked in
    ``recency`` order and a workspace is dropped from the walk once it has
    accumulated ``--max-file-bytes`` of text, because everything older than
    that would be discarded by the file budget anyway. Measured: 116MB read
    instead of 2.10GB (5.5%), 85 composers of 808, 1.2s.
  - Hard per-run row and byte budgets as a circuit breaker, so a store that
    grows a new shape cannot turn the cron into a 2GB read.

The walk is a pure function of (store contents, window, budgets): composers are
ordered by ``recency DESC, composerId`` — the tiebreak matters, ties are common
— so a second run over unchanged chat rebuilds byte-identical files and
``write_if_changed`` leaves every mtime alone.

Writes are content-compared before replacing, so unchanged workspaces keep their
mtime and ocbrain's fingerprint gate skips them on recurring runs. All text is
passed through ``ocbrain.text.redact_secrets`` before touching disk.

Usage: export-cursor-chats.py [--storage DIR] [--out DIR] [--max-file-bytes N]
                              [--global-storage PATH | --no-global-storage]
                              [--since-days N] [--max-record-bytes N]
                              [--max-bubbles N] [--max-scan-bytes N]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_CURSOR_USER = Path.home() / "Library" / "Application Support" / "Cursor" / "User"
DEFAULT_STORAGE = _CURSOR_USER / "workspaceStorage"
DEFAULT_GLOBAL_STORAGE = _CURSOR_USER / "globalStorage" / "state.vscdb"
DEFAULT_OUT = Path.home() / ".ocbrain" / "exports" / "cursor"

# Generations carry full text in some versions, only a summary in others.
_PROMPTS_KEY = "aiService.prompts"
_GENERATIONS_KEY = "aiService.generations"

# Skips are reported in full when few, which is the normal case: this machine
# has ten workspaces. The cap only stops a pathological storage dir from
# burying the counts under its own listing.
_SKIPPED_SAMPLE = 20

# Composer bubbles whose workspace is not a directory under --storage. The
# workspace was deleted, or is a draft target that never had one. Their chat is
# still real, so it goes to its own file with the claimed id kept per record
# rather than being dropped for want of a home.
UNATTRIBUTED = "unattributed"

_BUBBLE_PREFIX = "bubbleId:"

# Room set aside for a truncation marker, in bytes. The longest marker this
# module writes is under 60 bytes; the slack covers a byte count that grows
# past the sizes seen so far.
_MARKER_RESERVE = 96

# Bubble text past this length is the exception, not the rule: on the live
# store p50 is 146 bytes, p99 is 13,629, and a 16,000-byte cap truncates 0.76%
# of records. It also sits below import-history's own 20,000-byte per-file
# index window, so a single message can never crowd out a whole conversation.
DEFAULT_MAX_RECORD_BYTES = 16_000

# Circuit breakers, not a paging scheme. The satisfaction rule below normally
# stops the walk long before either bites (measured: 16,263 bubbles / 116MB),
# so a run that reports budget_exhausted means the store changed shape and
# wants a human, not a bigger number.
DEFAULT_MAX_BUBBLES = 100_000
DEFAULT_MAX_SCAN_BYTES = 500_000_000

# Every composer on this machine is inside 45 days; the window exists so a
# long-lived store cannot grow the sweep without bound.
DEFAULT_SINCE_DAYS = 45


def iso_from_ms(ms: int | float | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, UTC).isoformat()
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def parse_timestamp(value: object) -> str | None:
    """Normalise the two spellings Cursor uses for a time.

    ``aiService`` rows carry epoch milliseconds; composer bubbles carry an ISO
    8601 string. Dividing the latter by 1000 raises TypeError, which the numeric
    path deliberately swallows rather than propagates — an unreadable timestamp
    must not cost us the message it belongs to.
    """
    if not value:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat()
        except ValueError:
            return None
    if isinstance(value, int | float):
        return iso_from_ms(value)
    return None


def parse_json_array(raw: bytes | str | None) -> list[dict]:
    if not raw:
        return []
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def bubble_key_range(composer_id: str) -> tuple[str, str]:
    """Half-open ``[lo, hi)`` key range covering one composer's bubbles.

    Bubble keys are ``bubbleId:<composerId>:<bubbleId>``. ``hi`` replaces the
    trailing ``:`` with ``;``, the next codepoint, which bounds the prefix
    without excluding any key that carries it. The point is the query plan:
    ``cursorDiskKV`` has a UNIQUE index on ``key``, so a range comparison plans
    as ``SEARCH ... USING COVERING INDEX (key>? AND key<?)`` while
    ``LIKE 'bubbleId:<id>:%'`` plans as ``SCAN`` over all 545k keys of a 7.8GB
    table -- SQLite will not use an index for LIKE unless the column is
    declared ``COLLATE NOCASE`` or ``case_sensitive_like`` is on. Same 22 rows
    on the live store: 0.1-0.8ms versus 1,075ms warm and 15,280ms cold.
    """
    low = f"{_BUBBLE_PREFIX}{composer_id}:"
    return low, low[:-1] + ";"


def truncate_middle(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cap one record at ``max_bytes``, keeping the head and the tail.

    Matches the convention ocbrain's own ``history_text_window`` uses: the
    opening of a message says what it is about and the close says what it
    concluded, so the middle is what a reader can most afford to lose. Returns
    the text and whether anything was dropped.
    """
    raw = text.encode("utf-8")
    if max_bytes <= 0 or len(raw) <= max_bytes:
        return text, False
    budget = max(max_bytes - _MARKER_RESERVE, 0)
    head_len = budget // 2
    tail_len = budget - head_len
    omitted = len(raw) - head_len - tail_len
    marker = f"\n[... {omitted} bytes omitted from middle ...]\n"
    head = raw[:head_len].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - tail_len :].decode("utf-8", errors="ignore") if tail_len else ""
    return head + marker + tail, True


def composer_index(conn: sqlite3.Connection, since_ms: float) -> list[tuple[str, str | None, int]]:
    """Composers touched since ``since_ms``, newest first.

    ``composerId`` breaks the ``recency`` tie deliberately. Cursor stamps
    composers opened together with the same millisecond — 27 of the 808 on this
    machine sit in such a pair — and an unordered tie makes the byte budget cut
    the walk at a different place from run to run, which would rewrite files
    that did not change.
    """
    try:
        rows = conn.execute(
            "SELECT composerId, workspaceId, recency FROM composerHeaders "
            "WHERE recency >= ? ORDER BY recency DESC, composerId",
            (since_ms,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(str(cid), ws, int(rec or 0)) for cid, ws, rec in rows if cid]


# json_extract runs the parse inside SQLite, so a 4.3MB bubble never becomes a
# Python dict: only its three interesting fields cross the boundary. json_valid
# guards it because one malformed row would otherwise abort the whole cursor
# mid-iteration and cost us every bubble behind it.
_BUBBLE_QUERY = (
    "SELECT key, length(value), "
    "CASE WHEN json_valid(value) THEN json_extract(value, '$.type') END, "
    "CASE WHEN json_valid(value) THEN json_extract(value, '$.createdAt') END, "
    "CASE WHEN json_valid(value) THEN json_extract(value, '$.text') END "
    "FROM cursorDiskKV WHERE key >= ? AND key < ?"
)


def harvest_global_bubbles(
    conn: sqlite3.Connection,
    *,
    known_workspaces: set[str],
    since_ms: float,
    file_budget: int,
    max_record_bytes: int,
    max_bubbles: int,
    max_scan_bytes: int,
) -> tuple[dict[str, list[dict]], dict]:
    """Sweep the global composer store into per-workspace record lists.

    Walks composers newest first and abandons a workspace once it holds
    ``file_budget`` of text, because ``render_jsonl`` keeps only the newest that
    much and every older bubble read for it would be discarded unread. That
    single rule is what turns a 2.10GB store into a 116MB read.
    """
    started = time.monotonic()
    by_workspace: dict[str, list[dict]] = {}
    filled: dict[str, int] = {}
    headers = composer_index(conn, since_ms)
    scanned = bubbles = kept = scan_bytes = truncated = 0
    exhausted: str | None = None
    floor_ms: int | None = None

    for composer_id, workspace_id, recency in headers:
        bucket = workspace_id if workspace_id in known_workspaces else UNATTRIBUTED
        if filled.get(bucket, 0) >= file_budget:
            continue
        scanned += 1
        floor_ms = recency
        try:
            rows = conn.execute(_BUBBLE_QUERY, bubble_key_range(composer_id))
            for key, size, bubble_type, created_at, text in rows:
                bubbles += 1
                scan_bytes += int(size or 0)
                if not text or not text.strip():
                    continue
                content, cut = truncate_middle(text.strip(), max_record_bytes)
                truncated += int(cut)
                record = {
                    "role": "user" if bubble_type == 1 else "assistant",
                    "timestamp": parse_timestamp(created_at),
                    "content": content,
                    "source_key": key,
                    "composer_id": composer_id,
                }
                if bucket == UNATTRIBUTED:
                    # The file name cannot say which workspace claimed this, so
                    # the record has to; the id outlives the directory.
                    record["workspace_id"] = workspace_id
                by_workspace.setdefault(bucket, []).append(record)
                filled[bucket] = filled.get(bucket, 0) + len(content)
                kept += 1
        except sqlite3.Error as exc:
            exhausted = f"sqlite_error:{exc}"
            break
        if bubbles >= max_bubbles:
            exhausted = "max_bubbles"
            break
        if scan_bytes >= max_scan_bytes:
            exhausted = "max_scan_bytes"
            break

    stats = {
        "composers_in_window": len(headers),
        "composers_scanned": scanned,
        "bubbles_read": bubbles,
        "bubbles_kept": kept,
        "bubbles_truncated": truncated,
        "scan_bytes": scan_bytes,
        "oldest_composer_scanned": iso_from_ms(floor_ms),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if exhausted:
        stats["budget_exhausted"] = exhausted
    return by_workspace, stats


def workspace_folder(storage_dir: Path) -> str | None:
    meta = storage_dir / "workspace.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    folder = data.get("folder") or data.get("workspace")
    return str(folder) if folder else None


def extract_records(
    conn: sqlite3.Connection, max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES
) -> list[dict]:
    """Pull user/assistant chat records from one workspace state DB."""
    records: list[dict] = []

    def item_value(key: str) -> bytes | str | None:
        try:
            row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        return row[0] if row else None

    for item in parse_json_array(item_value(_PROMPTS_KEY)):
        text = (item.get("text") or "").strip()
        if text:
            content, _ = truncate_middle(text, max_record_bytes)
            records.append({"role": "user", "timestamp": None, "content": content})

    for item in parse_json_array(item_value(_GENERATIONS_KEY)):
        text = (item.get("text") or item.get("textDescription") or "").strip()
        if text:
            content, _ = truncate_middle(text, max_record_bytes)
            records.append(
                {
                    "role": "assistant",
                    "timestamp": iso_from_ms(item.get("unixMs")),
                    "content": content,
                }
            )

    # Newer Cursor versions store composer bubbles in cursorDiskKV.
    try:
        rows = conn.execute(
            "SELECT key, value FROM cursorDiskKV "
            "WHERE key LIKE 'bubbleId:%' OR key LIKE 'composerData:%'"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    for key, value in rows:
        try:
            serialized = (
                value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else value
            )
            data = json.loads(serialized)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        text = (data.get("text") or "").strip()
        if not text:
            continue
        bubble_type = data.get("type")
        role = "user" if bubble_type == 1 else "assistant"
        content, _ = truncate_middle(text, max_record_bytes)
        records.append(
            {
                "role": role,
                "timestamp": parse_timestamp(data.get("createdAt") or data.get("unixMs")),
                "content": content,
                "source_key": key,
            }
        )
    return records


def _truncation_note(dropped: int, kept: int) -> str:
    return json.dumps(
        {"_truncated": {"dropped_oldest": dropped, "kept_newest": kept}},
        ensure_ascii=False,
    )


def render_jsonl(records: list[dict], meta: dict, max_bytes: int, redact) -> str:
    """Render newest-last JSONL, dropping the OLDEST records when over budget.

    The file budget used to be spent front to back, so the first records to be
    dropped were the newest — the ones a memory system most wants and the only
    ones a reader could not reconstruct from an earlier export. Filling from the
    newest end inverts that. It matters more now than it did: the global
    composer store puts orders of magnitude more chat behind this budget, so
    the wrong end is no longer a rounding error.
    """

    def ts_key(rec: dict) -> str:
        return rec.get("timestamp") or ""

    header = json.dumps({"_meta": meta}, ensure_ascii=False)
    budget = max_bytes - len(header) - 1
    ordered = sorted(records, key=ts_key)
    kept: list[str] = []  # newest first while filling, reversed before output
    total = 0
    for rec in reversed(ordered):
        rec = dict(rec)
        rec["content"] = redact(rec["content"])
        line = json.dumps(rec, ensure_ascii=False)
        if total + len(line) + 1 > budget:
            break
        kept.append(line)
        total += len(line) + 1

    lines = [header]
    dropped = len(ordered) - len(kept)
    if dropped:
        # The note is not free either. Evict from the oldest kept end until it
        # fits, rather than let the marker push the file past its budget.
        note = _truncation_note(dropped, len(kept))
        while kept and total + len(note) + 1 > budget:
            total -= len(kept.pop()) + 1
            dropped += 1
            note = _truncation_note(dropped, len(kept))
        lines.append(note)
    kept.reverse()
    lines.extend(kept)
    return "\n".join(lines) + "\n"


def write_if_changed(path: Path, text: str) -> bool:
    """Write only when content differs; preserves mtime for fingerprint gating."""
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
    path.write_text(text, encoding="utf-8")
    return True


def read_workspace_records(state_db: Path, max_record_bytes: int) -> tuple[list[dict], str | None]:
    """Records from one workspace state DB, plus the reason it yielded none."""
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [], f"sqlite_error:{exc}"
    try:
        # sqlite3.connect defers reading the header, and extract_records
        # tolerates a missing table on purpose because the schema varies by
        # Cursor version. Without this probe an unreadable file reaches the
        # end of extraction with no records and is filed as an idle
        # workspace -- the exact confusion this reporting exists to end.
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        records = extract_records(conn, max_record_bytes)
    except sqlite3.Error as exc:
        return [], f"sqlite_error:{exc}"
    finally:
        conn.close()
    return records, None if records else "no_records"


def sweep_global_storage(
    args: argparse.Namespace, known_workspaces: set[str]
) -> tuple[dict[str, list[dict]], dict]:
    """Open the global composer store read-only and harvest it, or say why not.

    The store belongs to a running editor. ``mode=ro`` is the whole guarantee
    that this exporter cannot corrupt a live 7.8GB database, so every failure
    below returns a reason instead of retrying on a writable handle.
    """
    if args.no_global_storage:
        return {}, {"status": "disabled"}
    if not args.global_storage.is_file():
        return {}, {"status": "absent", "path": str(args.global_storage)}
    since_ms = (time.time() - args.since_days * 86_400) * 1000
    try:
        conn = sqlite3.connect(f"file:{args.global_storage}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return {}, {"status": "error", "error": str(exc)}
    try:
        records, stats = harvest_global_bubbles(
            conn,
            known_workspaces=known_workspaces,
            since_ms=since_ms,
            file_budget=args.max_file_bytes,
            max_record_bytes=args.max_record_bytes,
            max_bubbles=args.max_bubbles,
            max_scan_bytes=args.max_scan_bytes,
        )
    finally:
        conn.close()
    stats["status"] = "ok"
    stats["since_days"] = args.since_days
    stats["workspaces"] = {name: len(rows) for name, rows in sorted(records.items())}
    return records, stats


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-file-bytes", type=int, default=200_000)
    parser.add_argument("--global-storage", type=Path, default=DEFAULT_GLOBAL_STORAGE)
    parser.add_argument(
        "--no-global-storage",
        action="store_true",
        help="sweep only the per-workspace stores",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=DEFAULT_SINCE_DAYS,
        help="only composers touched within this many days",
    )
    parser.add_argument("--max-record-bytes", type=int, default=DEFAULT_MAX_RECORD_BYTES)
    parser.add_argument("--max-bubbles", type=int, default=DEFAULT_MAX_BUBBLES)
    parser.add_argument("--max-scan-bytes", type=int, default=DEFAULT_MAX_SCAN_BYTES)
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()

    # Secret redaction comes from the ocbrain package when available; fall back
    # to a no-op so the exporter still works standalone (never silently skip).
    try:
        from ocbrain.text import redact_secrets as redact
    except ImportError:
        def redact(text: str) -> str:  # type: ignore[no-redef]
            return text

    if not args.storage.is_dir():
        print(json.dumps({"exported": 0, "reason": f"no workspaceStorage at {args.storage}"}))
        return 0

    args.out.mkdir(parents=True, exist_ok=True)

    # A bare skip counter cannot distinguish "nothing was ever typed here" from
    # "this workspace moved to a shape we do not read", and three workspaces --
    # including the most recently used one -- sat in that blind spot. Name the
    # workspace and the reason so the next silent drop is one line of output.
    local: dict[str, list[dict]] = {}
    reasons: dict[str, str] = {}
    workspaces = {path.name: path for path in sorted(args.storage.iterdir()) if path.is_dir()}
    for name, workspace_dir in workspaces.items():
        state_db = workspace_dir / "state.vscdb"
        if not state_db.is_file():
            reasons[name] = "no_state_db"
            continue
        records, reason = read_workspace_records(state_db, args.max_record_bytes)
        local[name] = records
        if reason:
            reasons[name] = reason

    global_records, global_stats = sweep_global_storage(args, set(workspaces))

    exported = unchanged = 0
    skipped: list[dict[str, str]] = []
    for name in sorted(set(local) | set(global_records) | set(reasons)):
        records = local.get(name, []) + global_records.get(name, [])
        if not records:
            skipped.append({"workspace": name, "reason": reasons.get(name, "no_records")})
            continue
        workspace_dir = workspaces.get(name)
        meta: dict[str, object] = {
            "workspace_id": name,
            "workspace_folder": workspace_folder(workspace_dir) if workspace_dir else None,
            "record_count": len(records),
            "composer_record_count": len(global_records.get(name, [])),
        }
        if name in reasons:
            # The local store failed but the global one carried the workspace.
            # Exporting it must not bury why half of it is missing.
            meta["local_store"] = reasons[name]
        text = render_jsonl(records, meta, args.max_file_bytes, redact)
        if write_if_changed(args.out / f"cursor-{name}.jsonl", text):
            exported += 1
        else:
            unchanged += 1

    # Group by reason class: a sqlite error carries a distinct message per
    # workspace, which would make the tally as unreadable as the raw list.
    by_reason: dict[str, int] = {}
    for entry in skipped:
        klass = entry["reason"].split(":", 1)[0]
        by_reason[klass] = by_reason.get(klass, 0) + 1

    print(
        json.dumps(
            {
                "exported": exported,
                "unchanged": unchanged,
                "skipped": len(skipped),
                "skipped_by_reason": by_reason,
                "skipped_sample": skipped[:_SKIPPED_SAMPLE],
                "global_storage": global_stats,
                "out": str(args.out),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
