"""Codex adapter.

Rollouts live at ``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`` (plus
a small ``archived_sessions/`` tail). Four record shapes matter:

``custom_tool_call``
    Almost always ``name="exec"``, whose ``input`` is a *JavaScript program* that
    calls ``tools.exec_command({cmd: "..."})``. The shell command therefore has
    to be dug out of the script; a codex trace read naively looks like one tool
    repeated a thousand times.
``function_call``
    A conventional name + JSON ``arguments`` (``exec_command``, ``send_message``,
    ``spawn_agent`` ...).
``mcp_tool_call_end``
    Carries ``invocation.server``/``invocation.tool`` and a ``result`` that is
    ``{"Ok": ...}`` or ``{"Err": ...}`` — the cleanest success signal in the corpus.
``web_search_end``
    Provider-side search.

Rollouts total roughly 8 GB, dominated by base64 ``reasoning.encrypted_content``
blobs. Lines are substring-prefiltered before ``json.loads`` so a full pass stays
minutes rather than an hour.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

from ..normalize import (
    Trace,
    TraceEvent,
    arg_signature,
    error_fingerprint,
    result_class,
    shell_signature,
)

CODEX_ROOTS = [
    Path(os.path.expanduser("~/.codex/sessions")),
    Path(os.path.expanduser("~/.codex/archived_sessions")),
]

_INTERESTING = (
    '"custom_tool_call"',
    '"custom_tool_call_output"',
    '"function_call"',
    '"function_call_output"',
    '"mcp_tool_call_end"',
    '"web_search_end"',
    '"session_meta"',
    '"turn_context"',
)

# ``tools.exec_command({cmd: "git status", ...})`` — single or double quoted,
# with backticks for the heredoc-ish multi-line form codex likes.
_JS_CMD = re.compile(
    r"""(?:cmd|command)\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`)""",
    re.S,
)
_JS_TOOLCALL = re.compile(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_BAD = {"error", "refused", "timeout"}


def _js_shell_command(script: str) -> str | None:
    match = _JS_CMD.search(script)
    if not match:
        return None
    raw = next(group for group in match.groups() if group is not None)
    try:
        return json.loads(f'"{raw}"') if '\\' in raw else raw
    except Exception:
        return raw


def _exec_signature(script: str) -> str:
    command = _js_shell_command(script)
    if command:
        return shell_signature(command)
    inner = _JS_TOOLCALL.findall(script or "")
    if inner:
        return "codex:exec:" + "+".join(sorted(set(inner))[:3])
    return "codex:exec:<script>"


def _output_text(output: object) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = []
        for block in output:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    if isinstance(output, dict):
        return json.dumps(output)[:4000]
    return ""


def parse_codex_file(path: Path, *, max_bytes: int = 400_000_000) -> Trace | None:
    """Parse one codex rollout into a normalized trace."""
    calls: dict[str, tuple[str, str, str | None]] = {}
    order: list[str] = []
    results: dict[str, str] = {}
    errors: dict[str, str] = {}
    session_id = ""
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
            if not any(marker in line for marker in _INTERESTING):
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            timestamp = record.get("timestamp")
            if isinstance(timestamp, str):
                if started is None or timestamp < started:
                    started = timestamp
                if ended is None or timestamp > ended:
                    ended = timestamp

            kind = payload.get("type")
            if record.get("type") == "session_meta":
                session_id = payload.get("session_id") or payload.get("id") or session_id
                if isinstance(payload.get("cwd"), str):
                    cwd = payload["cwd"]
                continue
            if record.get("type") == "turn_context":
                if cwd is None and isinstance(payload.get("cwd"), str):
                    cwd = payload["cwd"]
                continue

            if kind == "custom_tool_call":
                call_id = payload.get("call_id")
                if not isinstance(call_id, str):
                    continue
                name = payload.get("name") or "exec"
                script = payload.get("input")
                signature = (
                    _exec_signature(script if isinstance(script, str) else "")
                    if name in {"exec", "js"}
                    else arg_signature(name, script)
                )
                calls[call_id] = (name, signature, timestamp)
                order.append(call_id)
                if payload.get("status") not in (None, "completed", "in_progress"):
                    results[call_id] = "error"
            elif kind == "function_call":
                call_id = payload.get("call_id")
                if not isinstance(call_id, str):
                    continue
                name = payload.get("name") or "unknown"
                arguments = payload.get("arguments")
                if name in {"exec_command", "shell", "local_shell"}:
                    parsed = arguments
                    if isinstance(parsed, str):
                        try:
                            parsed = json.loads(parsed)
                        except Exception:
                            parsed = {}
                    command = None
                    if isinstance(parsed, dict):
                        command = parsed.get("cmd") or parsed.get("command")
                    if isinstance(command, list):
                        command = " ".join(str(part) for part in command)
                    signature = (
                        shell_signature(command) if isinstance(command, str) else "bash:<unknown>"
                    )
                else:
                    signature = arg_signature(name, arguments)
                calls[call_id] = (name, signature, timestamp)
                order.append(call_id)
            elif kind in {"custom_tool_call_output", "function_call_output"}:
                call_id = payload.get("call_id")
                if not isinstance(call_id, str):
                    continue
                text = _output_text(payload.get("output"))
                outcome = result_class(text=text)
                results[call_id] = outcome
                if outcome in _BAD:
                    fingerprint = error_fingerprint(text)
                    if fingerprint:
                        errors[call_id] = fingerprint
            elif kind == "mcp_tool_call_end":
                call_id = payload.get("call_id")
                invocation = payload.get("invocation") or {}
                server = invocation.get("server") or "unknown"
                tool = invocation.get("tool") or "unknown"
                signature = f"mcp:{server}.{tool}"
                if not isinstance(call_id, str):
                    call_id = f"mcp-{len(order)}"
                calls[call_id] = (f"mcp:{tool}", signature, timestamp)
                order.append(call_id)
                outcome = payload.get("result")
                if isinstance(outcome, dict) and "Err" in outcome:
                    detail = str(outcome.get("Err"))
                    results[call_id] = result_class(is_error=True, text=detail)
                    fingerprint = error_fingerprint(detail)
                    if fingerprint:
                        errors[call_id] = fingerprint
                elif isinstance(outcome, dict) and "Ok" in outcome:
                    results[call_id] = "ok"
            elif kind == "web_search_end":
                call_id = payload.get("call_id") or f"web-{len(order)}"
                calls[call_id] = ("web_search", "web:search", timestamp)
                order.append(call_id)
                results[call_id] = "ok" if payload.get("results") else "empty"

    if not order:
        return None
    if not session_id:
        match = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path.name
        )
        session_id = match.group(1) if match else path.stem

    seen: set[str] = set()
    events: list[TraceEvent] = []
    for call_id in order:
        if call_id in seen:
            continue
        seen.add(call_id)
        name, signature, timestamp = calls[call_id]
        events.append(
            TraceEvent(
                step_index=len(events),
                tool=name,
                arg_signature=signature,
                result_class=results.get(call_id, "ok"),
                at=timestamp,
                error=errors.get(call_id),
            )
        )
    return Trace(
        trace_id=session_id,
        runtime="codex",
        source_uri=str(path),
        started_at=started,
        ended_at=ended,
        cwd=cwd,
        events=events,
        truncated=truncated,
    )


def iter_codex_traces(roots: list[Path] | None = None) -> Iterator[Trace]:
    for base in roots or CODEX_ROOTS:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.jsonl")):
            trace = parse_codex_file(path)
            if trace is not None:
                yield trace
