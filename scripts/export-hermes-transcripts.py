#!/usr/bin/env python3
"""Export Hermes session transcripts (state.db) to JSONL files for ocbrain harvest.

Hermes keeps its real transcript store in SQLite, which ocbrain's file-based
harvest cannot read directly. This exporter dumps each session to
``~/.hermes/sessions/export/hermes-<session_id>.jsonl`` — a path below
``.hermes`` so ``history_runtime()`` attributes it to ``hermes``.

There are two stores, and both must be swept. The single legacy
``~/.hermes/state.db`` was the whole fleet until the gateways moved to one home
per agent profile under ``~/.hermes/profiles/<name>/state.db``. Exporting only
the legacy path fails silently — it still finds the frozen sessions, still
reports ``unchanged``, and never mentions the profiles it did not look at — so
every profile is swept on every run and reported by name.

Profile sessions land in a per-profile subdirectory
(``.../export/<profile>/hermes-<session_id>.jsonl``). ``history_files``
recurses, and ``history_runtime`` derives the runtime from the ``.hermes`` path
component, so the subdirectory needs no harvest-side change and keeps the
runtime ``hermes``. Session ids embed their creation time and do not collide
across stores, but the subdirectory keeps the legacy filenames untouched
regardless.

Writes are content-compared before replacing, so unchanged sessions keep their
mtime and ocbrain's fingerprint gate skips them on recurring runs.

Usage: export-hermes-transcripts.py [--db PATH] [--out DIR]
                                    [--profiles-root DIR | --no-profiles]
                                    [--max-file-bytes N]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB = Path.home() / ".hermes" / "state.db"
DEFAULT_OUT = Path.home() / ".hermes" / "sessions" / "export"
DEFAULT_PROFILES_ROOT = Path.home() / ".hermes" / "profiles"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat()


def export_session(conn: sqlite3.Connection, session_id: str, meta: dict, max_bytes: int) -> str:
    """Render one session as JSONL text (bounded to max_bytes)."""
    rows = conn.execute(
        "SELECT role, content, tool_name, timestamp FROM messages "
        "WHERE session_id = ? AND active = 1 ORDER BY timestamp, id",
        (session_id,),
    )
    lines = [json.dumps({"_meta": meta}, ensure_ascii=False)]
    total = len(lines[0]) + 1
    for role, content, tool_name, ts in rows:
        if not content:
            continue
        record = {
            "role": role,
            "timestamp": iso(ts),
            "content": content,
        }
        if tool_name:
            record["tool"] = tool_name
        line = json.dumps(record, ensure_ascii=False)
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


def export_db(
    db_path: Path,
    out_dir: Path,
    max_bytes: int,
    profile: str | None = None,
) -> dict[str, int]:
    """Export every non-empty session in one state.db. Returns write counts.

    The source is opened read-only: these are live gateway databases and the
    exporter must never take a write lock on one. ``profile`` is stamped into
    the ``_meta`` line so the owning agent survives in the evidence body as well
    as in the source path, and is omitted entirely for the legacy store so its
    existing exports stay byte-identical.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sessions = conn.execute(
            "SELECT id, source, display_name, model, started_at, ended_at, message_count "
            "FROM sessions WHERE message_count > 0"
        ).fetchall()
        exported = unchanged = 0
        for sid, source, display, model, started, ended, msg_count in sessions:
            meta = {
                "session_id": sid,
                "source": source,
                "display_name": display,
                "model": model,
                "started_at": iso(started),
                "ended_at": iso(ended),
                "message_count": msg_count,
            }
            if profile is not None:
                meta["profile"] = profile
            text = export_session(conn, sid, meta, max_bytes)
            if write_if_changed(out_dir / f"hermes-{sid}.jsonl", text):
                exported += 1
            else:
                unchanged += 1
    finally:
        conn.close()
    return {"exported": exported, "unchanged": unchanged}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--profiles-root", type=Path, default=DEFAULT_PROFILES_ROOT)
    parser.add_argument(
        "--no-profiles",
        action="store_true",
        help="sweep only --db, for single-store invocation",
    )
    parser.add_argument("--max-file-bytes", type=int, default=200_000)
    args = parser.parse_args()

    payload: dict = {
        "exported": 0,
        "unchanged": 0,
        "legacy": None,
        "profiles": {},
        "errors": [],
        "out": str(args.out),
    }

    def record(result: dict[str, int]) -> dict[str, int]:
        payload["exported"] += result["exported"]
        payload["unchanged"] += result["unchanged"]
        return result

    if args.db.exists():
        payload["legacy"] = record(export_db(args.db, args.out, args.max_file_bytes))
    else:
        payload["legacy"] = {"exported": 0, "unchanged": 0, "reason": f"no state.db at {args.db}"}

    if not args.no_profiles:
        for profile_db in sorted(args.profiles_root.glob("*/state.db")):
            name = profile_db.parent.name
            # One sick profile must not cost us the other four: a gateway
            # mid-migration, a permissions change, or a corrupt page should
            # show up as a named error, not as an aborted sweep.
            try:
                result = export_db(profile_db, args.out / name, args.max_file_bytes, profile=name)
            except (sqlite3.Error, OSError) as exc:
                payload["errors"].append({"profile": name, "error": str(exc)})
                continue
            payload["profiles"][name] = record(result)

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
