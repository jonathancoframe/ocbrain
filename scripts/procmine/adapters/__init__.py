"""Per-runtime trace adapters.

Each adapter answers one question: given this runtime's on-disk history, what
were the tool calls, in order, and did each one work? Everything downstream sees
only :class:`~procmine.normalize.Trace`, so a new runtime is a new adapter and
nothing else.

Implementation status is deliberately explicit — see ``ADAPTER_STATUS`` — because
the atlas has to report which runtimes it can actually see into.
"""

from __future__ import annotations

from .claude_code import claude_files, iter_claude_traces, parse_claude_traces
from .codex import codex_files, iter_codex_traces, parse_codex_traces
from .cursor import iter_cursor_traces
from .hermes import (
    hermes_legacy_files,
    hermes_profile_files,
    iter_hermes_legacy_traces,
    iter_hermes_profile_traces,
    parse_hermes_legacy_traces,
    parse_hermes_profile_db,
)

ADAPTER_STATUS: dict[str, str] = {
    "claude-code": "full: tool_use blocks carry name+input, tool_result carries is_error",
    "codex": "full: custom_tool_call/function_call/mcp_tool_call_end with outputs",
    "hermes": "full: per-profile state.db assistant.tool_calls + tool-role results",
    "hermes-legacy": "partial: export JSONL records the tool name but not its arguments",
    "cursor": "stub: the export carries no tool calls at all, only role/content",
}

__all__ = [
    "ADAPTER_STATUS",
    "claude_files",
    "codex_files",
    "hermes_legacy_files",
    "hermes_profile_files",
    "iter_claude_traces",
    "iter_codex_traces",
    "iter_cursor_traces",
    "iter_hermes_legacy_traces",
    "iter_hermes_profile_traces",
    "parse_claude_traces",
    "parse_codex_traces",
    "parse_hermes_legacy_traces",
    "parse_hermes_profile_db",
]
