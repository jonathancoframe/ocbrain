from __future__ import annotations

from pathlib import Path

import pytest

from ocbrain.core_v1 import (
    append_core_event,
    canonical_runtime,
    init_core_v1,
    record_core_v1_retrieval,
    set_automatic_activation,
)
from ocbrain.db import connect
from ocbrain.mcp_v1 import correct_v1, decide_proposal_v1, ingest_v1
from ocbrain.scope import ScopeContext, ScopeTag

SCOPE = ScopeTag(
    "project",
    "project:bountiful",
    visibility="internal",
    egress_policy="local_only",
    provenance="test",
)


def _core(tmp_path: Path):
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    return conn


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("codex-desktop", "codex"),
        ("Codex desktop", "codex"),
        ("Codex Desktop local macOS", "codex"),
        ("codex-desktop-heartbeat", "codex"),
        ("hermes-cron-mac-planner", "hermes"),
        ("Hermes Agent", "hermes"),
        ("macOS user Hermes gateway", "hermes"),
        ("cursor-subagent", "cursor"),
        ("claude-code", "claude-code"),
        ("macOS Telegram gateway", "telegram"),
        # No known client in the string: keep it legible rather than bucketing
        # every unrecognized runtime into one opaque label.
        ("local macOS + readonlyprod ClickHouse", "local-macos-readonlyprod-clickhouse"),
        ("mcp", "mcp"),
        ("  ", None),
        (None, None),
    ],
)
def test_canonical_runtime_collapses_client_spellings(reported, expected) -> None:
    assert canonical_runtime(reported) == expected


def test_recorded_retrieval_stores_canonical_runtime_and_keeps_the_raw_string(
    tmp_path: Path,
) -> None:
    conn = _core(tmp_path)
    retrieval_id = record_core_v1_retrieval(
        conn,
        query="probe",
        context={"project": "bountiful"},
        items=[],
        runtime="Codex desktop local macOS",
        task_ref=None,
        session_id=None,
    )
    conn.commit()

    row = conn.execute(
        "SELECT served_to_runtime, context_json FROM retrieval_uses WHERE id=?",
        (retrieval_id,),
    ).fetchone()
    assert row["served_to_runtime"] == "codex"
    assert "Codex desktop local macOS" in row["context_json"]


def test_already_canonical_runtime_does_not_add_a_raw_copy(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    retrieval_id = record_core_v1_retrieval(
        conn,
        query="probe",
        context={"project": "bountiful"},
        items=[],
        runtime="codex",
        task_ref=None,
        session_id=None,
    )
    conn.commit()
    row = conn.execute(
        "SELECT context_json FROM retrieval_uses WHERE id=?", (retrieval_id,)
    ).fetchone()
    assert "runtime_raw" not in row["context_json"]


def test_ingest_survives_a_blocked_auto_recompile(tmp_path: Path) -> None:
    """A hard-retracted belief must not make re-ingesting its text fail.

    Auto-belief ids are content-addressed, so identical text recompiles to the
    same id; with automatic activation on, that hit compilation_block_reason and
    raised PermissionError out of brain.ingest, losing the evidence write too.
    """
    conn = _core(tmp_path)
    set_automatic_activation(conn, True)
    context = ScopeContext(project="bountiful")
    body = "The nightly export finishes before the morning report runs."

    first = ingest_v1(
        conn,
        body=body,
        kind="observation",
        context=context,
        writer="test",
        session_id=None,
        artifact_ref=None,
    )
    belief_id = first["auto_compiled_belief_id"]
    assert first["kind"] == "evidence_recorded_and_compiled"

    correct_v1(
        conn,
        layer="belief",
        target=belief_id,
        op="retract",
        body="retired by test",
        actor="test",
        hard=True,
    )
    conn.commit()

    second = ingest_v1(
        conn,
        body=body,
        kind="observation",
        context=context,
        writer="test",
        session_id=None,
        artifact_ref=None,
    )
    conn.commit()

    # The write succeeds instead of raising; only the recompile is skipped, and
    # the response says so rather than failing silently.
    assert second["kind"] == "evidence_recorded"
    assert "hard-corrected" in second["auto_compile_blocked"]
    assert "auto_compiled_belief_id" not in second
    # Evidence is content-addressed, so the identical body is the same record.
    assert second["evidence_id"] == first["evidence_id"]
    assert (
        conn.execute("SELECT COUNT(*) FROM evidence_objects WHERE body=?", (body,)).fetchone()[0]
        == 1
    )
    # And the belief stays retracted rather than being quietly revived.
    assert (
        conn.execute(
            "SELECT status FROM current_beliefs WHERE belief_id=?", (belief_id,)
        ).fetchone()["status"]
        == "retracted"
    )


def _seed_servable_belief(conn, *, belief_id: str, body: str) -> None:
    proposal = append_core_event(
        conn,
        "compilation_proposed",
        {
            "belief_id": belief_id,
            "belief_type": "curated_fact",
            "body": body,
            "evidence_ids": [],
            "scope": SCOPE.to_dict(),
            "confidence": 0.9,
            "attributes": {"source_quality": 0.95},
        },
        writer="test",
    )
    decide_proposal_v1(
        conn,
        proposal_event_id=proposal,
        decision="approve",
        actor="test",
        edited_body=None,
        reason="test seed",
    )


def test_source_expansion_bounds_the_issue_history(tmp_path: Path) -> None:
    """brain.source must not return an unbounded issuance list.

    context_source_handle_issues grows one row per (handle, retrieval) forever.
    The v1 path already windowed its inline list but nothing pinned it, so the
    bound was one refactor away from silently going missing. (The legacy v0
    expansion path was unbounded and is fixed alongside; it cannot load a v1
    handle, so it is not exercised here.)
    """
    from ocbrain.mcp_v1 import build_context_v1, expand_source_v1, record_context_v1
    from ocbrain.shared_context import ISSUED_BY_WINDOW

    conn = _core(tmp_path)
    belief_id = "curated:bountiful:hot-handle"
    body = "Exports finish before the morning report."
    _seed_servable_belief(conn, belief_id=belief_id, body=body)
    conn.commit()

    issued_total = ISSUED_BY_WINDOW + 5
    source_id = ""
    for _index in range(issued_total):
        packet, handles = build_context_v1(
            conn,
            "morning report exports",
            context=ScopeContext(project="bountiful"),
            limit=5,
            cross_scope=False,
            delivery_target="local_model",
        )
        assert handles
        record_context_v1(
            conn,
            packet,
            handles,
            context=ScopeContext(project="bountiful"),
            delivery_target="local_model",
        )
        source_id = handles[0]["id"]
    conn.commit()

    expanded = expand_source_v1(
        conn,
        source_id=source_id,
        context=ScopeContext(project="bountiful"),
        max_chars=2_000,
    )
    # Windowed, not truncated-to-nothing, and the total is still reported so a
    # caller can tell how much history it is not seeing.
    assert len(expanded["issued_by_retrieval_use_ids"]) == ISSUED_BY_WINDOW
    assert expanded["issued_by_count"] == issued_total
    assert issued_total > ISSUED_BY_WINDOW


def test_projection_does_not_duplicate_the_evidence_body(tmp_path: Path) -> None:
    """The body text belongs in one column, not three.

    evidence_objects.metadata_json carried a full copy of the event body, which
    already includes the text that sits in the same row's `body` column and in
    brain_events.body_json. On a real core that third copy was ~23% of the file.
    """
    from ocbrain.core_v1 import get_core_v1_evidence, record_core_v1_evidence

    conn = _core(tmp_path)
    body = "A distinctive evidence body that must be stored exactly once here."
    evidence_id, _event_id = record_core_v1_evidence(
        conn, body=body, kind="analysis_result", scope=SCOPE, writer="test"
    )
    conn.commit()

    stored = get_core_v1_evidence(conn, evidence_id)
    assert stored["body"] == body
    event_body = stored["metadata"]["event_body"]
    # Metadata that is not the text survives; the text itself does not.
    assert event_body["kind"] == "analysis_result"
    assert "body" not in event_body
    assert "body_omitted" in event_body
    row = conn.execute(
        "SELECT metadata_json FROM evidence_objects WHERE evidence_id=?", (evidence_id,)
    ).fetchone()
    assert body not in row["metadata_json"]
