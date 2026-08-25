"""Stage 1: walk every runtime's history and cache normalized traces.

Extraction is separated from mining because it is the slow half — codex rollouts
alone are ~8 GB — and because caching the normalized stream means every later
experiment reruns in seconds against a file that contains no raw arguments.

It is also **incremental**. A source file is fingerprinted by ``(mtime_ns, size)``
and its normalized traces are kept as a per-file JSONL segment; an unchanged file
is concatenated from its segment instead of being reparsed. That mirrors the
``history_file_fingerprints_v1`` gate the v1 importer already uses, and it is
what makes a scheduled miner affordable: the corpus is append-mostly, so almost
every file is unchanged on almost every run.

The state file and segment directory live under ``~/.ocbrain/procmine/`` and are
written only when the miner runs. The output cache is JSONL, one trace per line.
Reading is strictly read-only against the live stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters import (
    ADAPTER_STATUS,
    claude_files,
    codex_files,
    hermes_legacy_files,
    hermes_profile_files,
    parse_claude_traces,
    parse_codex_traces,
    parse_hermes_legacy_traces,
    parse_hermes_profile_db,
)
from .adapters.cursor import cursor_shortfall
from .normalize import NORMALIZER_VERSION, Trace

PROCMINE_STATE_DIR = Path(os.path.expanduser("~/.ocbrain/procmine"))
EXTRACT_STATE_NAME = "extract-state.json"
SEGMENT_DIR_NAME = "cache"
EXTRACT_STATE_SCHEMA = "ocbrain.procmine.extract-state.v1"


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """One runtime's history: which files it is, and how to read one of them."""

    files: Callable[[], list[Path]]
    parse: Callable[[Path], Iterator[Trace]]


SOURCES: dict[str, SourceSpec] = {
    "claude-code": SourceSpec(claude_files, parse_claude_traces),
    "codex": SourceSpec(codex_files, parse_codex_traces),
    "hermes": SourceSpec(hermes_profile_files, parse_hermes_profile_db),
    "hermes-legacy": SourceSpec(hermes_legacy_files, parse_hermes_legacy_traces),
}


def extract_all(sources: list[str] | None = None) -> Iterator[Trace]:
    """Every trace, freshly parsed. No cache, no state — used for one-off runs."""
    for name in sources or list(SOURCES):
        spec = SOURCES.get(name)
        if spec is None:
            continue
        for path in spec.files():
            yield from spec.parse(path)


def file_fingerprint(path: Path) -> str | None:
    """``mtime_ns:size``, or ``None`` if the file has gone away.

    Not a content hash: codex rollouts alone are ~8 GB, and hashing them to
    decide whether to reparse them would cost more than reparsing. The failure
    mode of a stat fingerprint — a file rewritten in place within the same
    nanosecond and to the same length — does not occur in append-only
    transcript stores.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return f"{info.st_mtime_ns}:{info.st_size}"


def _segment_name(source: str, path: Path) -> str:
    """A flat, collision-free filename for one source file's segment.

    The path itself is hashed rather than sanitized: transcript paths are deeply
    nested and contain characters a filename cannot, and the digest keeps the
    segment directory flat without leaking the path into a filename.
    """
    digest = hashlib.sha256(str(path).encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"{source}-{digest}.jsonl"


def load_extract_state(state_path: Path) -> dict[str, dict[str, Any]]:
    """Load the per-file fingerprint gate, failing open to "nothing is cached"."""
    try:
        raw = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("schema_version") != EXTRACT_STATE_SCHEMA:
        return {}
    if raw.get("normalizer_version") != NORMALIZER_VERSION:
        # The signature or redaction rules moved. Every cached segment holds
        # text the current normalizer would not have produced, and no source
        # file changed to say so, so the whole cache is discarded.
        return {}
    files = raw.get("files")
    if not isinstance(files, dict):
        return {}
    return {
        key: value
        for key, value in files.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def store_extract_state(state_path: Path, files: dict[str, dict[str, Any]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": EXTRACT_STATE_SCHEMA,
                "normalizer_version": NORMALIZER_VERSION,
                "files": files,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _state_key(source: str, path: Path) -> str:
    return json.dumps([source, str(path)])


def write_cache(
    out_path: Path,
    sources: list[str] | None = None,
    *,
    state_dir: Path | None = None,
    incremental: bool = True,
) -> dict[str, Any]:
    """Extract every trace to ``out_path`` and return a per-source summary.

    With ``incremental`` on (the default) an unchanged source file is copied
    from its cached segment rather than reparsed, and the summary reports how
    many files were reused. ``state_dir`` exists so tests can point the state
    and segments at a tmp path instead of the operator's home.
    """
    base = state_dir or PROCMINE_STATE_DIR
    state_path = base / EXTRACT_STATE_NAME
    segment_dir = base / SEGMENT_DIR_NAME
    state = load_extract_state(state_path) if incremental else {}
    fresh_state: dict[str, dict[str, Any]] = {}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if incremental:
        segment_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "adapter_status": ADAPTER_STATUS,
        "cursor_shortfall": cursor_shortfall(),
        "incremental": incremental,
        "sources": {},
    }
    started = time.time()
    with out_path.open("w") as handle:
        for name in sources or list(SOURCES):
            spec = SOURCES.get(name)
            if spec is None:
                continue
            summary["sources"][name] = _extract_source(
                name,
                spec,
                handle,
                state=state,
                fresh_state=fresh_state,
                segment_dir=segment_dir,
                incremental=incremental,
            )
    if incremental:
        store_extract_state(state_path, fresh_state)
        _prune_segments(segment_dir, fresh_state)
    summary["total_seconds"] = round(time.time() - started, 1)
    summary["cache_path"] = str(out_path)
    summary["state_path"] = str(state_path) if incremental else None
    return summary


def _extract_source(
    name: str,
    spec: SourceSpec,
    handle: Any,
    *,
    state: dict[str, dict[str, Any]],
    fresh_state: dict[str, dict[str, Any]],
    segment_dir: Path,
    incremental: bool,
) -> dict[str, Any]:
    traces = events = truncated = 0
    reused_files = parsed_files = 0
    source_started = time.time()
    for path in spec.files():
        key = _state_key(name, path)
        fingerprint = file_fingerprint(path)
        cached = state.get(key) if incremental else None
        segment = segment_dir / _segment_name(name, path)
        if (
            cached is not None
            and fingerprint is not None
            and cached.get("fingerprint") == fingerprint
            and segment.exists()
        ):
            counts = _replay_segment(segment, handle)
            if counts is not None:
                reused_files += 1
                traces += counts["traces"]
                events += counts["events"]
                truncated += counts["truncated"]
                fresh_state[key] = {"fingerprint": fingerprint, **counts}
                continue
        counts = _parse_and_cache(
            spec, path, handle, segment=segment if incremental else None
        )
        parsed_files += 1
        traces += counts["traces"]
        events += counts["events"]
        truncated += counts["truncated"]
        if incremental and fingerprint is not None:
            fresh_state[key] = {"fingerprint": fingerprint, **counts}
        if traces and traces % 50 == 0:
            print(f"  {name}: {traces} traces, {events} events", file=sys.stderr, flush=True)
    print(
        f"{name}: {traces} traces / {events} events "
        f"({reused_files} files cached, {parsed_files} parsed)",
        file=sys.stderr,
        flush=True,
    )
    return {
        "traces": traces,
        "events": events,
        "truncated_traces": truncated,
        "files_reused": reused_files,
        "files_parsed": parsed_files,
        "seconds": round(time.time() - source_started, 1),
    }


def _replay_segment(segment: Path, handle: Any) -> dict[str, int] | None:
    """Copy a cached segment into the combined cache, or refuse it.

    A segment that cannot be read or holds a malformed line is treated as a
    miss, not as an error: the source file is still there and reparsing it is
    always correct.
    """
    lines: list[str] = []
    traces = events = truncated = 0
    try:
        with segment.open() as source:
            for line in source:
                stripped = line.strip()
                if not stripped:
                    continue
                record = json.loads(stripped)
                if not isinstance(record, dict):
                    return None
                lines.append(stripped)
                traces += 1
                events += len(record.get("events") or [])
                truncated += 1 if record.get("truncated") else 0
    except (OSError, ValueError):
        return None
    for line in lines:
        handle.write(line + "\n")
    return {"traces": traces, "events": events, "truncated": truncated}


def _parse_and_cache(
    spec: SourceSpec, path: Path, handle: Any, *, segment: Path | None
) -> dict[str, int]:
    traces = events = truncated = 0
    encoded: list[str] = []
    for trace in spec.parse(path):
        line = json.dumps(trace.as_dict())
        handle.write(line + "\n")
        if segment is not None:
            encoded.append(line)
        traces += 1
        events += len(trace.events)
        truncated += 1 if trace.truncated else 0
    if segment is not None:
        _write_atomic(segment, "".join(f"{line}\n" for line in encoded))
    return {"traces": traces, "events": events, "truncated": truncated}


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _prune_segments(segment_dir: Path, fresh_state: dict[str, dict[str, Any]]) -> int:
    """Delete segments for source files that no longer exist.

    Without this the cache directory only ever grows, and a deleted transcript
    would leave a segment behind forever. Only files this run knows the shape of
    are kept; anything else in the directory is ours and stale.
    """
    keep = set()
    for key in fresh_state:
        try:
            source, path = json.loads(key)
        except (TypeError, ValueError):
            continue
        keep.add(_segment_name(str(source), Path(str(path))))
    removed = 0
    try:
        entries = list(segment_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if entry.is_file() and entry.name.endswith(".jsonl") and entry.name not in keep:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def read_cache(path: Path) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                traces.append(json.loads(line))
    return traces
