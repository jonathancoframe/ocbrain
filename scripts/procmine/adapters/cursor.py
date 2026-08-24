"""Cursor adapter — deliberately a stub, and the reason is the finding.

``scripts/export-cursor-chats.py`` writes ``~/.ocbrain/exports/cursor/*.jsonl``
with one record per message: ``{role, timestamp, content}``. There is no
``tool_calls`` field, no tool name, no result — the exporter reads Cursor's chat
bubbles, not its tool log. So a cursor session cannot be turned into an event
stream at all, and the honest output is zero traces plus a counted reason.

This adapter therefore returns nothing and reports the shortfall, so the atlas
can say "cursor: 0 traces, exporter drops tool calls" instead of silently
showing cursor as a runtime with no procedures.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from ..normalize import Trace

CURSOR_EXPORT = Path(os.path.expanduser("~/.ocbrain/exports/cursor"))


def cursor_shortfall(root: Path | None = None) -> dict[str, int]:
    """Count what the cursor export *does* contain, to size what is missing."""
    base = root or CURSOR_EXPORT
    stats = {"files": 0, "messages": 0, "records_with_tool_field": 0}
    if not base.exists():
        return stats
    for path in sorted(base.glob("*.jsonl")):
        stats["files"] += 1
        try:
            handle = path.open("r", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict) or "_meta" in record:
                    continue
                stats["messages"] += 1
                if record.get("tool") or record.get("tool_calls") or record.get("tool_name"):
                    stats["records_with_tool_field"] += 1
    return stats


def iter_cursor_traces(root: Path | None = None) -> Iterator[Trace]:
    """No traces: the export has no tool calls to normalize."""
    return iter(())
