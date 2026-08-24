"""Claude Code adapter.

Transcripts are one JSONL per session under
``~/.claude/projects/<slug>/<session-uuid>.jsonl``, with subagent transcripts in
a ``subagents/`` subdirectory. Assistant messages carry ``tool_use`` content
blocks (name + input); the matching ``tool_result`` arrives in the following
user message and carries ``is_error``.

Pairing is by ``tool_use_id``, not by adjacency: a single assistant turn can
issue several parallel tool calls whose results come back interleaved.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from ..normalize import Trace, TraceEvent, arg_signature, error_fingerprint, result_class

CLAUDE_ROOT = Path(os.path.expanduser("~/.claude/projects"))
_BAD = {"error", "refused", "timeout"}


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content)[:4000]
    return ""


def parse_claude_file(path: Path, *, max_bytes: int = 400_000_000) -> Trace | None:
    """Parse one Claude Code session file into a normalized trace."""
    calls: dict[str, tuple[str, str, str | None]] = {}
    order: list[str] = []
    results: dict[str, tuple[str, str | None]] = {}
    session_id = path.stem
    started: str | None = None
    ended: str | None = None
    cwd: str | None = None
    truncated = False

    read = 0
    try:
        handle = path.open("r", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            read += len(line)
            if read > max_bytes:
                truncated = True
                break
            if '"tool_use"' not in line and '"tool_result"' not in line and '"cwd"' not in line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            timestamp = record.get("timestamp")
            if isinstance(timestamp, str):
                if started is None or timestamp < started:
                    started = timestamp
                if ended is None or timestamp > ended:
                    ended = timestamp
            if cwd is None and isinstance(record.get("cwd"), str):
                cwd = record["cwd"]
            if isinstance(record.get("sessionId"), str):
                session_id = record["sessionId"]

            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use":
                    use_id = block.get("id")
                    if not isinstance(use_id, str):
                        continue
                    name = block.get("name") or "unknown"
                    calls[use_id] = (name, arg_signature(name, block.get("input")), timestamp)
                    order.append(use_id)
                elif kind == "tool_result":
                    use_id = block.get("tool_use_id")
                    if not isinstance(use_id, str):
                        continue
                    body = _result_text(block.get("content"))
                    outcome = result_class(
                        is_error=bool(block.get("is_error")) if "is_error" in block else None,
                        text=body,
                    )
                    results[use_id] = (
                        outcome,
                        error_fingerprint(body) if outcome in _BAD else None,
                    )

    if not order:
        return None

    events = [
        TraceEvent(
            step_index=index,
            tool=calls[use_id][0],
            arg_signature=calls[use_id][1],
            result_class=results.get(use_id, ("ok", None))[0],
            at=calls[use_id][2],
            error=results.get(use_id, ("ok", None))[1],
        )
        for index, use_id in enumerate(order)
    ]
    is_subagent = path.parent.name == "subagents"
    return Trace(
        trace_id=session_id,
        runtime="claude-code-subagent" if is_subagent else "claude-code",
        source_uri=str(path),
        started_at=started,
        ended_at=ended,
        cwd=cwd,
        events=events,
        truncated=truncated,
    )


def iter_claude_traces(root: Path | None = None) -> Iterator[Trace]:
    base = root or CLAUDE_ROOT
    if not base.exists():
        return
    for path in sorted(base.rglob("*.jsonl")):
        trace = parse_claude_file(path)
        if trace is not None:
            yield trace
