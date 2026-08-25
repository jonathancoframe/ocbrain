"""Pins, and the recompilation that used to drop them."""

from __future__ import annotations

from pathlib import Path

from ocbrain.core_v1 import (
    append_core_event,
    get_core_v1_belief,
    init_core_v1,
    project_core_v1,
    record_core_v1_evidence,
)
from ocbrain.db import connect
from ocbrain.mcp_v1 import correct_v1
from ocbrain.scope import ScopeContext, ScopeTag

SCOPE = ScopeTag(
    "project",
    "project:bountiful",
    visibility="internal",
    egress_policy="local_only",
    provenance="test",
)
CONTEXT = ScopeContext(project="bountiful")


def _core(tmp_path: Path):
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    return conn


def _seed(
    conn,
    *,
    belief_id: str,
    body: str,
    confidence: float = 0.9,
    attributes: dict | None = None,
    scope: ScopeTag = SCOPE,
) -> str:
    evidence_id, _event = record_core_v1_evidence(
        conn,
        body=f"evidence for {belief_id}",
        kind="observation",
        scope=scope,
        writer="test",
    )
    proposal = append_core_event(
        conn,
        "compilation_proposed",
        {
            "belief_id": belief_id,
            "belief_type": "wiki_fact",
            "body": body,
            "evidence_ids": [evidence_id],
            "scope": scope.to_dict(),
            "confidence": confidence,
            "attributes": attributes or {},
        },
        writer="test",
    )
    append_core_event(
        conn,
        "compilation_decided",
        {"proposal_event_id": proposal, "decision": "approve", "actor": "test"},
        writer="test",
        project=True,
    )
    conn.commit()
    return belief_id


# --------------------------------------------------------------------------- #
# The pin regression
# --------------------------------------------------------------------------- #
def test_compilation_decision_preserves_pinned(tmp_path: Path) -> None:
    """A pin survives recompilation.

    ``_project_compilation_decision`` hardcoded ``pinned=False``, so every
    approved proposal silently unpinned its belief -- and a scheduled curator
    recompiles constantly. That is why one real corpus held exactly one pinned
    belief: a pin only lasted until the next run touched the same fact.
    """
    conn = _core(tmp_path)
    target = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    correct_v1(
        conn, layer="belief", target=target, op="pin", body=None, actor="human", hard=False
    )
    conn.commit()
    assert get_core_v1_belief(conn, target)["pinned"] == 1

    # Recompile the same belief id, as a curator run would.
    evidence_id, _event = record_core_v1_evidence(
        conn, body="a later observation", kind="observation", scope=SCOPE, writer="wiki-curator"
    )
    proposal = append_core_event(
        conn,
        "compilation_proposed",
        {
            "belief_id": target,
            "belief_type": "wiki_fact",
            "body": "The research VM is reached with ssh asa1 (reworded).",
            "evidence_ids": [evidence_id],
            "scope": SCOPE.to_dict(),
            "confidence": 0.9,
            "attributes": {},
        },
        writer="wiki-curator",
    )
    append_core_event(
        conn,
        "compilation_decided",
        {"proposal_event_id": proposal, "decision": "approve", "actor": "wiki-curator"},
        writer="wiki-curator",
        project=True,
    )
    conn.commit()

    assert get_core_v1_belief(conn, target)["pinned"] == 1
    project_core_v1(conn, full=True)
    assert get_core_v1_belief(conn, target)["pinned"] == 1


def test_a_first_compilation_is_not_pinned(tmp_path: Path) -> None:
    """Preserving a pin must not invent one for a belief that never had it."""
    conn = _core(tmp_path)
    target = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    assert get_core_v1_belief(conn, target)["pinned"] == 0
