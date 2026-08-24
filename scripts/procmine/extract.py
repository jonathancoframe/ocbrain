"""Stage 1: walk every runtime's history and cache normalized traces.

Extraction is separated from mining because it is the slow half — codex rollouts
alone are ~8 GB — and because caching the normalized stream means every later
experiment reruns in seconds against a file that contains no raw arguments.

The cache is JSONL, one trace per line, written to the scratchpad or the
worktree. Reading is strictly read-only against the live stores.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .adapters import (
    ADAPTER_STATUS,
    iter_claude_traces,
    iter_codex_traces,
    iter_hermes_legacy_traces,
    iter_hermes_profile_traces,
)
from .adapters.cursor import cursor_shortfall
from .normalize import Trace

SOURCES: dict[str, Any] = {
    "claude-code": iter_claude_traces,
    "codex": iter_codex_traces,
    "hermes": iter_hermes_profile_traces,
    "hermes-legacy": iter_hermes_legacy_traces,
}


def extract_all(sources: list[str] | None = None) -> Iterator[Trace]:
    for name in sources or list(SOURCES):
        producer = SOURCES.get(name)
        if producer is None:
            continue
        yield from producer()


def write_cache(out_path: Path, sources: list[str] | None = None) -> dict[str, Any]:
    """Extract every trace to ``out_path`` and return a per-source summary."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "adapter_status": ADAPTER_STATUS,
        "cursor_shortfall": cursor_shortfall(),
        "sources": {},
    }
    started = time.time()
    with out_path.open("w") as handle:
        for name in sources or list(SOURCES):
            producer = SOURCES.get(name)
            if producer is None:
                continue
            traces = 0
            events = 0
            truncated = 0
            source_started = time.time()
            for trace in producer():
                handle.write(json.dumps(trace.as_dict()) + "\n")
                traces += 1
                events += len(trace.events)
                truncated += 1 if trace.truncated else 0
                if traces % 50 == 0:
                    print(
                        f"  {name}: {traces} traces, {events} events",
                        file=sys.stderr,
                        flush=True,
                    )
            summary["sources"][name] = {
                "traces": traces,
                "events": events,
                "truncated_traces": truncated,
                "seconds": round(time.time() - source_started, 1),
            }
            print(f"{name}: {traces} traces / {events} events", file=sys.stderr, flush=True)
    summary["total_seconds"] = round(time.time() - started, 1)
    summary["cache_path"] = str(out_path)
    return summary


def read_cache(path: Path) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                traces.append(json.loads(line))
    return traces
