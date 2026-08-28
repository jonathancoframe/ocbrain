"""The legacy blend retriever must stay unreachable from a v1 core.

The repo carries two independently tuned rankers. ``core_v1`` serves the live
path (FTS5 bm25 + a dense sidecar fused with weighted RRF and a multiplicative
scope/confidence/quality/recency/feedback prior). ``retrieve.py`` is the retired
legacy ranker: a flat ``relevance * scope_weight * confidence * pinned *
catalog_stub`` product with a repo-FTS fallback. Both are still imported by
``mcp.py``, ``cli.py`` and ``shared_context.py``, each behind an ``is_core_v1``
early return.

Nothing enforced that separation. A single ``retrieve(...)`` call added outside
one of those guards would silently mix the two formulas on the live core, and
every existing test would still pass. This gate measures the boundary instead of
assuming it: it drives the complete advertised tool surface and counts how many
``retrieve.py`` functions actually execute.

The second test is the gate's own mutation proof. It runs the identical driver
against a legacy core, where the count MUST be non-zero. If the tracer stops
firing, the driver stops dispatching, or the tool table goes stale, that test
fails and the zero in the first test is exposed as an empty measurement rather
than a passing one.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

from test_mcp_v1 import _seed_v1

from ocbrain.db import connect, init_db
from ocbrain.mcp import (
    ADMIN_PROFILE,
    RUNTIME_TOOLS,
    call_tool,
    tools_for_profile,
)

_RETRIEVE_SOURCE = str((Path(__file__).resolve().parents[1] / "src/ocbrain/retrieve.py").resolve())

# A bare call to the legacy blend retriever, not the import that binds the name.
LEGACY_CALL = re.compile(r"(?<![\w.])retrieve\s*\(")

# Every call site of the legacy ranker in the package, per module. Measured on
# this tree; each one sits in a branch dominated by an ``is_core_v1`` early
# return. Provenance recorded in docs/THRESHOLDS.md.
LEGACY_RETRIEVE_CALL_SITES = {"mcp.py": 2, "cli.py": 1, "shared_context.py": 1}

# One representative argument set per advertised tool. Write tools are included
# deliberately: a refusal still runs the dispatch branch that would host a stray
# legacy call. ``test_tool_coverage_is_complete`` fails if a tool is added to the
# server without being added here, so this table cannot silently go stale.
TOOL_ARGUMENTS: dict[str, dict[str, object]] = {
    "brain.briefing": {"context": {"project": "ocbrain"}},
    "brain.closeout": {"status": "completed", "summary": "gate probe"},
    "brain.context": {"query": "shared context", "limit": 5},
    "brain.correct": {"target": "belief:shared-context", "layer": "body", "op": "replace"},
    "brain.digest": {},
    "brain.egress_preview": {},
    "brain.feedback": {"retrieval_use_id": "missing", "outcome": "used"},
    "brain.forget": {"target": "belief:shared-context"},
    "brain.get": {"id": "belief:shared-context"},
    "brain.goal_close": {
        "goal_id": "missing",
        "status": "completed",
        "verifier_uri": "repo://ocbrain/pytest",
        "verifier_status": "pass",
    },
    "brain.goal_open": {
        "objective": "gate probe",
        "finish_line": "pytest -q",
        "source_path": "docs/THRESHOLDS.md",
    },
    "brain.ingest": {"body": "gate probe evidence"},
    "brain.ledger": {},
    "brain.preview": {"query": "shared context", "limit": 5},
    "brain.proposal_decide": {"proposal_event_id": "missing", "decision": "approve"},
    "brain.proposals": {},
    "brain.search": {"query": "shared context", "limit": 5},
    "brain.source": {"id": "missing"},
    "brain.supersede": {"target": "belief:shared-context", "body": "x", "reason": "gate probe"},
}

# Scoped and cross-scope variants: the legacy blend is reached through the
# scoped branch of brain.search, so an unscoped-only driver would miss it.
SCOPED_VARIANTS: tuple[tuple[str, dict[str, object]], ...] = (
    ("brain.context", {"query": "shared context", "limit": 5, "context": {"project": "ocbrain"}}),
    ("brain.search", {"query": "shared context", "limit": 5, "context": {"project": "ocbrain"}}),
    ("brain.search", {"query": "shared context", "limit": 5, "cross_scope": True}),
    ("brain.preview", {"query": "shared context", "limit": 5, "context": {"project": "ocbrain"}}),
)


def _drive_every_tool(conn: sqlite3.Connection) -> tuple[set[str], int]:
    """Dispatch the whole advertised surface, tracing retrieve.py.

    Returns the set of ``retrieve.py`` function names that executed and the
    number of tool dispatches attempted.
    """
    executed: set[str] = set()

    def _tracer(frame, event, _arg):
        # Returning nothing declines local tracing: one record per call event is
        # all this needs, and per-line tracing over a 20 MB dispatch is not.
        if event == "call" and frame.f_code.co_filename == _RETRIEVE_SOURCE:
            executed.add(frame.f_code.co_name)

    calls = [(name, dict(args)) for name, args in sorted(TOOL_ARGUMENTS.items())]
    calls.extend((name, dict(args)) for name, args in SCOPED_VARIANTS)

    dispatched = 0
    previous = sys.gettrace()
    sys.settrace(_tracer)
    try:
        for name, arguments in calls:
            try:
                call_tool(conn, {"name": name, "arguments": arguments}, profile=ADMIN_PROFILE)
            except Exception:  # noqa: BLE001 - a refusal still exercised the branch
                pass
            dispatched += 1
    finally:
        sys.settrace(previous)
    return executed, dispatched


def _seed_legacy(tmp_path: Path) -> sqlite3.Connection:
    """A legacy core with one current belief, so the legacy ranker has input."""
    conn = connect(tmp_path / "legacy.sqlite")
    init_db(conn)
    conn.execute(
        """
        INSERT INTO current_beliefs (
          belief_id, body, scope_type, scope_id, visibility, egress_policy,
          confidence, confidence_band, evidence_ids, status, pinned,
          approved_event_id, last_event_id, last_compiled_at
        ) VALUES (
          'belief:shared-context',
          'Shared context is the stable bridge across every runtime.',
          'project', 'project:ocbrain', 'internal', 'local_only',
          0.9, 'high', '[]', 'current', 0,
          'evt_a', 'evt_a', '2026-07-10T00:00:00+00:00'
        )
        """
    )
    conn.commit()
    return conn


def test_tool_coverage_is_complete() -> None:
    """The driver must cover every tool the server advertises."""
    advertised = tools_for_profile(ADMIN_PROFILE)
    assert advertised == set(TOOL_ARGUMENTS), (
        "TOOL_ARGUMENTS drifted from the advertised tool surface; "
        f"missing={sorted(advertised - set(TOOL_ARGUMENTS))} "
        f"extra={sorted(set(TOOL_ARGUMENTS) - advertised)}"
    )
    assert RUNTIME_TOOLS <= advertised


def test_legacy_retriever_never_runs_on_a_v1_core(tmp_path: Path) -> None:
    conn = _seed_v1(tmp_path)
    try:
        executed, dispatched = _drive_every_tool(conn)
    finally:
        conn.close()

    # A driver that dispatched nothing would report an empty set for the wrong
    # reason. Pin the count to the table so an emptied table cannot pass.
    assert dispatched == len(TOOL_ARGUMENTS) + len(SCOPED_VARIANTS) == 23
    assert executed == set(), (
        "a v1 core reached the retired legacy blend retriever; "
        f"retrieve.py functions executed: {sorted(executed)}"
    )


def test_the_same_driver_does_reach_the_legacy_retriever(tmp_path: Path) -> None:
    """Mutation proof for the gate above: the instrument must report dirty."""
    conn = _seed_legacy(tmp_path)
    try:
        executed, dispatched = _drive_every_tool(conn)
    finally:
        conn.close()

    assert dispatched == len(TOOL_ARGUMENTS) + len(SCOPED_VARIANTS) == 23
    assert "retrieve" in executed, (
        "the driver no longer reaches retrieve.py on a legacy core, so the "
        "zero measured on a v1 core is an empty check rather than a passing one"
    )
    # Measured on this fixture: the legacy path runs 9 distinct retrieve.py
    # functions. Documented in docs/THRESHOLDS.md.
    assert len(executed) >= 5, sorted(executed)


def test_the_legacy_retriever_has_exactly_the_known_call_sites() -> None:
    """Freeze the legacy ranker's blast radius across the whole package.

    The dynamic gate above can only see call sites the tool driver reaches. A
    fourth ``retrieve(...)`` added in a module the driver never dispatches would
    slip past it. This counts the call sites directly, so any new one has to be
    added here and its guard re-proved by hand.
    """
    src = Path(__file__).resolve().parents[1] / "src/ocbrain"
    call_sites: dict[str, int] = {}
    for path in sorted(src.glob("*.py")):
        if path.name == "retrieve.py":
            continue
        lines = path.read_text().splitlines()
        if not any(line.startswith("from ocbrain.retrieve import retrieve") for line in lines):
            continue
        call_sites[path.name] = sum(
            1
            for line in lines
            if LEGACY_CALL.search(line) and not line.lstrip().startswith("from ")
        )

    assert call_sites == LEGACY_RETRIEVE_CALL_SITES, (
        "the legacy blend retriever gained or lost a call site; each one must sit "
        "behind an is_core_v1 early return, and the dynamic gate above must be "
        f"re-proved before this table is updated. found={call_sites}"
    )
