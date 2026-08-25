"""Hermes adapter — two sources of very different quality.

Default and per-profile ``state.db`` stores (rich)
    ``~/.hermes/state.db`` and ``~/.hermes/profiles/<profile>/state.db`` have a
    ``messages`` table where
    assistant rows carry ``tool_calls`` (OpenAI-shaped ``function.name`` +
    ``arguments`` JSON) and the following tool rows carry the result, usually a
    JSON body with ``exit_code``/``error``. Joined to ``sessions`` for identity
    and timing. This is the richest tool-call corpus on the machine.

Legacy export (thin)
    ``~/.hermes/sessions/export/*.jsonl`` records the tool *name* and a rendered
    one-line description, but not the arguments. Signatures from this source are
    tool-level only, which is stated in ``ADAPTER_STATUS`` and again in the atlas
    so nobody reads a hermes-legacy step as if it were argument-shaped.

Both are opened strictly read-only (``mode=ro``); these are live stores.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..normalize import Trace, TraceEvent, arg_signature, error_fingerprint, result_class

_BAD = {"error", "refused", "timeout"}
HERMES_PROFILES = Path(os.path.expanduser("~/.hermes/profiles"))
HERMES_DEFAULT_DB = Path(os.path.expanduser("~/.hermes/state.db"))
HERMES_LEGACY_EXPORT = Path(os.path.expanduser("~/.hermes/sessions/export"))


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


_PAYLOAD_KEYS = ("output", "result", "content", "stdout", "text", "message")


def _result_from_tool_content(content: str | None) -> str:
    """Classify a hermes tool row.

    Hermes tool results are almost always a JSON object, but the shape varies by
    tool: ``terminal`` uses ``exit_code``/``error``/``output`` while ``skill_view``
    and ``read_file`` use ``success``/``content``. Reading only ``output`` makes
    every non-shell tool look empty, so fall through the known payload keys and
    finally treat the whole object as the body.
    """
    if not content:
        return "empty"
    text = content.strip()
    if not text.startswith("{"):
        return result_class(text=text)
    try:
        body = json.loads(text)
    except Exception:
        return result_class(text=text)
    if not isinstance(body, dict):
        return result_class(text=text)

    exit_code = body.get("exit_code")
    error = body.get("error")
    success = body.get("success")
    is_error: bool | None = None
    if error:
        is_error = True
    elif isinstance(success, bool):
        is_error = not success

    payload: str | None = None
    for key in _PAYLOAD_KEYS:
        value = body.get(key)
        if isinstance(value, str):
            payload = value
            break
        if value is not None:
            payload = json.dumps(value)[:2000]
            break
    if payload is None:
        # No recognizable payload key: the object itself is the result.
        payload = text[:2000]
    return result_class(
        is_error=is_error,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        text=payload,
    )


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def iter_hermes_profile_traces(root: Path | None = None) -> Iterator[Trace]:
    base = root or HERMES_PROFILES
    stores: list[tuple[str, Path]] = []
    if root is None and HERMES_DEFAULT_DB.exists():
        stores.append(("default", HERMES_DEFAULT_DB))
    if base.exists():
        stores.extend(
            (profile_dir.name, profile_dir / "state.db")
            for profile_dir in sorted(p for p in base.iterdir() if p.is_dir())
            if (profile_dir / "state.db").exists()
        )
    for profile, db_path in stores:
        try:
            conn = _open_ro(db_path)
        except sqlite3.Error:
            continue
        try:
            yield from _profile_traces(conn, profile, db_path)
        finally:
            conn.close()


def _profile_traces(conn: sqlite3.Connection, profile: str, db_path: Path) -> Iterator[Trace]:
    sessions = {
        row[0]: row for row in conn.execute("select id, started_at, ended_at, cwd from sessions")
    }
    rows = conn.execute(
        "select session_id, role, tool_calls, tool_name, content, timestamp, id, tool_call_id "
        "from messages where role in ('assistant','tool') "
        "and (tool_calls is not null or tool_name is not null) "
        "order by session_id, id"
    )
    current: str | None = None
    # Pairing is by ``tool_call_id``: an assistant turn can issue several calls
    # and their results do not have to come back in issue order.
    pending: dict[str, tuple[str, str, str | None]] = {}
    pending_order: list[str] = []
    events: list[TraceEvent] = []

    def flush() -> Trace | None:
        if current is None or not events:
            return None
        meta = sessions.get(current)
        return Trace(
            trace_id=current,
            runtime=f"hermes:{profile}",
            source_uri=f"{db_path}#session={current}",
            started_at=_iso(meta[1]) if meta else None,
            ended_at=_iso(meta[2]) if meta else None,
            cwd=meta[3] if meta else None,
            events=list(events),
        )

    def drain_pending() -> None:
        """Calls that never got a result row still happened; keep them, unclassed."""
        for call_id in pending_order:
            entry = pending.get(call_id)
            if entry is None:
                continue
            name, signature, stamp = entry
            events.append(
                TraceEvent(
                    step_index=len(events),
                    tool=name,
                    arg_signature=signature,
                    result_class="empty",
                    at=stamp,
                )
            )
        pending.clear()
        pending_order.clear()

    for session_id, role, tool_calls, tool_name, content, timestamp, _row_id, call_id in rows:
        if session_id != current:
            drain_pending()
            trace = flush()
            if trace is not None:
                yield trace
            current = session_id
            events = []
        stamp = _iso(timestamp)
        if role == "assistant" and tool_calls:
            try:
                parsed = json.loads(tool_calls)
            except Exception:
                parsed = []
            if isinstance(parsed, list):
                for index, call in enumerate(parsed):
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") or {}
                    name = function.get("name") or call.get("name") or "unknown"
                    signature = arg_signature(name, function.get("arguments"))
                    key = call.get("call_id") or call.get("id") or f"anon-{len(events)}-{index}"
                    if key in pending:
                        # Hermes re-sends the same assistant row after a
                        # compaction; keep both calls but keep the keys unique.
                        key = f"{key}#{len(pending_order)}"
                    pending[key] = (name, signature, stamp)
                    pending_order.append(key)
        elif role == "tool":
            outcome = _result_from_tool_content(content)
            failure = error_fingerprint(content) if outcome in _BAD else None
            key = call_id if call_id in pending else (pending_order[0] if pending_order else None)
            if key is not None:
                name, signature, stamp_at = pending.pop(key)
                pending_order.remove(key)
            else:
                name = tool_name or "unknown"
                signature = f"tool:{name}"
                stamp_at = stamp
            events.append(
                TraceEvent(
                    step_index=len(events),
                    tool=name,
                    arg_signature=signature,
                    result_class=outcome,
                    at=stamp_at,
                    error=failure,
                )
            )
    drain_pending()
    trace = flush()
    if trace is not None:
        yield trace


def parse_hermes_legacy_file(path: Path) -> Trace | None:
    session_id = path.stem
    started: str | None = None
    ended: str | None = None
    events: list[TraceEvent] = []
    try:
        handle = path.open("r", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            meta = record.get("_meta")
            if isinstance(meta, dict):
                session_id = meta.get("session_id") or session_id
                started = meta.get("started_at") or started
                ended = meta.get("ended_at") or ended
                continue
            if record.get("role") != "tool":
                continue
            name = record.get("tool") or "unknown"
            content = record.get("content") or ""
            events.append(
                TraceEvent(
                    step_index=len(events),
                    tool=name,
                    # The export never stored arguments. Say so in the token
                    # rather than inventing a shape from the rendered summary.
                    arg_signature=f"toolonly:{name}",
                    result_class=result_class(text=content),
                    at=record.get("timestamp"),
                )
            )
    if not events:
        return None
    return Trace(
        trace_id=session_id,
        runtime="hermes-legacy",
        source_uri=str(path),
        started_at=started,
        ended_at=ended,
        cwd=None,
        events=events,
    )


def iter_hermes_legacy_traces(root: Path | None = None) -> Iterator[Trace]:
    base = root or HERMES_LEGACY_EXPORT
    if not base.exists():
        return
    for path in sorted(base.glob("*.jsonl")):
        trace = parse_hermes_legacy_file(path)
        if trace is not None:
            yield trace
