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

Known gap, measured 2026-08-24: composer chats are no longer workspace-local.
``cursorDiskKV`` exists in every workspace DB here and is empty in all nine;
the bubbles live in ``globalStorage/state.vscdb``, attributed to a workspace
through its ``composerHeaders.workspaceId``. That store holds 215,674 bubbles
totalling 2.1GB of JSON, so reading it is not a variation on this sweep — it
needs recency bounding and a truncation policy of its own, and is deliberately
left out here. Note for whoever picks it up: ``key LIKE 'bubbleId:<id>:%'``
full-scans the 7.8GB table, while the equivalent ``key >= 'bubbleId:<id>:' AND
key < 'bubbleId:<id>;'`` range uses the index and returns in milliseconds.

Writes are content-compared before replacing, so unchanged workspaces keep their
mtime and ocbrain's fingerprint gate skips them on recurring runs. All text is
passed through ``ocbrain.text.redact_secrets`` before touching disk.

Usage: export-cursor-chats.py [--storage DIR] [--out DIR] [--max-file-bytes N]
                              [--max-record-bytes N]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_STORAGE = (
    Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "workspaceStorage"
)
DEFAULT_OUT = Path.home() / ".ocbrain" / "exports" / "cursor"

# Generations carry full text in some versions, only a summary in others.
_PROMPTS_KEY = "aiService.prompts"
_GENERATIONS_KEY = "aiService.generations"

# Skips are reported in full when few, which is the normal case: this machine
# has nine workspaces. The cap only stops a pathological storage dir from
# burying the counts under its own listing.
_SKIPPED_SAMPLE = 20

# Room set aside for a truncation marker, in bytes. The longest marker this
# module writes is under 60 bytes; the slack covers a byte count that grows
# past the sizes seen so far.
_MARKER_RESERVE = 96

# Bubble text past this length is the exception, not the rule: on this
# machine's chat p50 is 146 bytes, p99 is 13,629, and a 16,000-byte cap
# truncates 0.76% of records. It also sits below import-history's own
# 20,000-byte per-file index window, so a single message can never crowd out a
# whole conversation.
DEFAULT_MAX_RECORD_BYTES = 16_000


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
    ones a reader cannot recover from an earlier export. Filling from the
    newest end inverts that. It compounds downstream: ``import-history`` indexes
    a 10KB head and a 10KB tail of each file, so a file truncated at its oldest
    200KB gave the brain the tail of the OLD chat and never saw the new.
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage", type=Path, default=DEFAULT_STORAGE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--max-file-bytes", type=int, default=200_000)
    parser.add_argument("--max-record-bytes", type=int, default=DEFAULT_MAX_RECORD_BYTES)
    args = parser.parse_args()

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
    exported = unchanged = 0
    # A bare skip counter cannot distinguish "nothing was ever typed here" from
    # "this workspace moved to a shape we do not read", and three workspaces --
    # including the most recently used one -- sat in that blind spot. Name the
    # workspace and the reason so the next silent drop is one line of output.
    skipped: list[dict[str, str]] = []
    for state_db in sorted(args.storage.glob("*/state.vscdb")):
        workspace_dir = state_db.parent
        if workspace_dir.name == "empty-window":
            skipped.append({"workspace": workspace_dir.name, "reason": "empty-window"})
            continue
        try:
            conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            skipped.append({"workspace": workspace_dir.name, "reason": f"sqlite_error:{exc}"})
            continue
        try:
            # sqlite3.connect defers reading the header, and extract_records
            # tolerates a missing table on purpose because the schema varies by
            # Cursor version. Without this probe an unreadable file reaches the
            # end of extraction with no records and is filed as an idle
            # workspace -- the exact confusion this reporting exists to end.
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            records = extract_records(conn, args.max_record_bytes)
        except sqlite3.Error as exc:
            skipped.append({"workspace": workspace_dir.name, "reason": f"sqlite_error:{exc}"})
            continue
        finally:
            conn.close()
        if not records:
            skipped.append({"workspace": workspace_dir.name, "reason": "no_records"})
            continue
        meta = {
            "workspace_id": workspace_dir.name,
            "workspace_folder": workspace_folder(workspace_dir),
            "record_count": len(records),
        }
        text = render_jsonl(records, meta, args.max_file_bytes, redact)
        if write_if_changed(args.out / f"cursor-{workspace_dir.name}.jsonl", text):
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
                "out": str(args.out),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
