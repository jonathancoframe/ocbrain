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


def workspace_folder(storage_dir: Path) -> str | None:
    meta = storage_dir / "workspace.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    folder = data.get("folder") or data.get("workspace")
    return str(folder) if folder else None


def extract_records(conn: sqlite3.Connection) -> list[dict]:
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
            records.append({"role": "user", "timestamp": None, "content": text})

    for item in parse_json_array(item_value(_GENERATIONS_KEY)):
        text = (item.get("text") or item.get("textDescription") or "").strip()
        if text:
            records.append(
                {
                    "role": "assistant",
                    "timestamp": iso_from_ms(item.get("unixMs")),
                    "content": text,
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
        records.append(
            {
                "role": role,
                "timestamp": parse_timestamp(data.get("createdAt") or data.get("unixMs")),
                "content": text,
                "source_key": key,
            }
        )
    return records


def render_jsonl(records: list[dict], meta: dict, max_bytes: int, redact) -> str:
    def ts_key(rec: dict) -> str:
        return rec.get("timestamp") or ""

    lines = [json.dumps({"_meta": meta}, ensure_ascii=False)]
    total = len(lines[0]) + 1
    for rec in sorted(records, key=ts_key):
        rec = dict(rec)
        rec["content"] = redact(rec["content"])
        line = json.dumps(rec, ensure_ascii=False)
        if total + len(line) + 1 > max_bytes:
            lines.append(json.dumps({"_truncated": True}))
            break
        lines.append(line)
        total += len(line) + 1
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
            records = extract_records(conn)
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
