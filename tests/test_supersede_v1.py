"""The supersession primitive: replacing a belief instead of deleting it.

Every correction an agent had ever issued against a real corpus took the same
shape, because it was the only shape available: soft-retract the wrong belief,
then type the replacement into the correction's ``body`` -- a field nothing
indexes and nothing serves. Correcting the brain therefore *destroyed*
knowledge. These tests pin the primitive that replaces that pattern, and the
three guards that keep it from being a worse footgun than the thing it fixes:
the era is stamped on both sides, the replacement cannot restate the original,
and it cannot gain authority by being newer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocbrain.core_v1 import (
    _restore_blocked,
    append_core_event,
    get_core_v1_belief,
    init_core_v1,
    project_core_v1,
    record_core_v1_evidence,
)
from ocbrain.db import connect
from ocbrain.hygiene import restore
from ocbrain.mcp_v1 import correct_v1, supersede_v1
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


def _serving(conn) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT belief_id FROM current_beliefs WHERE serve=1 AND status='current'"
        )
    }


def _supersede(conn, target: str, body: str, **kwargs):
    result = supersede_v1(
        conn,
        target=target,
        body=body,
        reason=kwargs.pop("reason", "the stored fact names a host that no longer exists"),
        context=kwargs.pop("context", CONTEXT),
        actor=kwargs.pop("actor", "agent:test"),
        **kwargs,
    )
    conn.commit()
    return result


# --------------------------------------------------------------------------- #
# The atomic replacement
# --------------------------------------------------------------------------- #
def test_supersede_retires_and_replaces_in_one_transaction(tmp_path: Path) -> None:
    """Both halves land together or neither does.

    A rollback is the only honest test of atomicity: if the retirement and the
    replacement were separate transactions, discarding the second would leave
    the corpus with the old fact deleted and nothing serving in its place --
    exactly the state the retract-then-describe pattern produces today.
    """
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")

    result = supersede_v1(
        conn,
        target=old,
        body="The research VM is reached with ssh asa2; asa1 was terminated.",
        reason="asa1 was terminated on 2026-08-20",
        context=CONTEXT,
        actor="agent:test",
    )
    successor = result["successor_id"]
    assert result["mode"] == "direct"
    # Uncommitted, both halves are already visible to this connection.
    assert _serving(conn) == {successor}

    conn.rollback()
    assert _serving(conn) == {old}
    assert get_core_v1_belief(conn, successor) is None


def test_the_retired_belief_leaves_the_search_index(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    assert {str(row[0]) for row in conn.execute("SELECT doc_id FROM search_documents")} == {old}

    result = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    indexed = {str(row[0]) for row in conn.execute("SELECT doc_id FROM search_documents")}
    assert indexed == {result["successor_id"]}


def test_supersession_stamps_the_era_on_both_sides(tmp_path: Path) -> None:
    """``valid_until`` closes the old era; ``valid_from`` opens the new one."""
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")

    result = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    retired = get_core_v1_belief(conn, old)
    successor = get_core_v1_belief(conn, result["successor_id"])
    assert retired["status"] == "retracted"
    assert retired["serve"] == 0
    assert retired["attributes"]["superseded_by"] == result["successor_id"]
    assert retired["attributes"]["valid_until"]
    assert successor["attributes"]["supersedes"] == old
    assert successor["attributes"]["valid_from"]
    assert successor["attributes"]["correction_evidence_id"] == result["correction_evidence_id"]


def test_a_supersession_copies_the_scope_verbatim(tmp_path: Path) -> None:
    """Replacing a fact can never widen where it reaches."""
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")

    result = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    assert result["scope"] == SCOPE.to_dict()
    assert get_core_v1_belief(conn, result["successor_id"])["scope"] == SCOPE.to_dict()


def test_the_successor_inherits_identity_but_not_the_old_evidence(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    old = _seed(
        conn,
        belief_id="belief:vm",
        body="The research VM is reached with ssh asa1.",
        attributes={
            "key": "vm-access",
            "title": "Research VM access",
            "category": "operational",
            "lifecycle": "durable",
            "curator": "wiki-curator-v9",
        },
    )
    original_evidence = get_core_v1_belief(conn, old)["evidence_ids"]

    result = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    successor = get_core_v1_belief(conn, result["successor_id"])
    assert successor["attributes"]["key"] == "vm-access"
    assert successor["attributes"]["title"] == "Research VM access"
    assert successor["attributes"]["category"] == "operational"
    assert successor["attributes"]["lifecycle"] == "durable"
    # Not inherited: the successor is not a curator product and never was.
    assert "curator" not in successor["attributes"]
    # The replacement stands on the correction that produced it, not on the
    # evidence that supported the claim it is replacing.
    assert successor["evidence_ids"] == [result["correction_evidence_id"]]
    assert successor["evidence_ids"] != original_evidence


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def test_self_supersede_is_rejected(tmp_path: Path) -> None:
    """Restating the stored belief would retire a good fact and replace it with itself."""
    conn = _core(tmp_path)
    body = "The research VM is reached with ssh asa1."
    old = _seed(conn, belief_id="belief:vm", body=body)

    for restatement in (body, f"  {body.upper()}  ", body.replace(" ", "\n")):
        with pytest.raises(ValueError, match="restates the stored belief"):
            supersede_v1(
                conn,
                target=old,
                body=restatement,
                reason="no real change",
                context=CONTEXT,
                actor="agent:test",
            )
    assert _serving(conn) == {old}


def test_the_replacement_confidence_is_capped_at_the_margin(tmp_path: Path) -> None:
    """Recency is not authority: a replacement never gains confidence by replacing."""
    conn = _core(tmp_path)
    confident = _seed(
        conn, belief_id="belief:high", body="Assignments are sticky per session.", confidence=0.97
    )
    tentative = _seed(
        conn, belief_id="belief:low", body="The hourly job runs at :05.", confidence=0.30
    )

    high = _supersede(conn, confident, "Assignments are sticky per visitor, not per session.")
    low = _supersede(conn, tentative, "The hourly job runs at :20 since the 2026-07-24 deploy.")

    assert get_core_v1_belief(conn, high["successor_id"])["confidence"] == 0.7
    # A low-confidence original does not get rounded *up* to the cap either.
    assert get_core_v1_belief(conn, low["successor_id"])["confidence"] == 0.30


def test_only_a_serving_belief_can_be_superseded(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    correct_v1(
        conn, layer="belief", target=old, op="retract", body="wrong", actor="human", hard=False
    )
    conn.commit()

    with pytest.raises(ValueError, match="only a serving belief can be superseded"):
        supersede_v1(
            conn,
            target=old,
            body="The research VM is reached with ssh asa2.",
            reason="asa1 is gone",
            context=CONTEXT,
            actor="agent:test",
        )
    with pytest.raises(ValueError, match="belief not found"):
        supersede_v1(
            conn,
            target="belief:absent",
            body="Something else entirely.",
            reason="asa1 is gone",
            context=CONTEXT,
            actor="agent:test",
        )


def test_previously_retracted_content_is_refused_in_plain_words(tmp_path: Path) -> None:
    """A banned body comes back as a sentence, not a PermissionError mid-write."""
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    replacement = "The research VM is reached with ssh asa2."

    first = _supersede(conn, old, replacement)
    # Tombstone the successor, then try to reintroduce the same content.
    append_core_event(
        conn,
        "tombstone_recorded",
        {"target": first["successor_id"], "mode": "soft", "approved_by": "human"},
        writer="human",
        project=True,
    )
    conn.commit()
    reinstated = _seed(conn, belief_id="belief:vm2", body="The research VM is reached somehow.")

    with pytest.raises(ValueError, match="blocked: this content was previously tombstoned"):
        supersede_v1(
            conn,
            target=reinstated,
            body=replacement,
            reason="trying the banned body again",
            context=CONTEXT,
            actor="agent:test",
        )
    # Nothing partial survived the refusal.
    assert reinstated in _serving(conn)


def test_restore_is_blocked_while_the_successor_serves(tmp_path: Path) -> None:
    """Restoring a superseded belief would serve both halves of a settled conflict."""
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    result = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    assert _restore_blocked(conn, old) == f"superseded by {result['successor_id']}"
    with pytest.raises(PermissionError, match="superseded by"):
        restore(conn, belief_id=old)

    # Once the replacement is itself retired, the original is restorable again:
    # the block is about serving two contradictory facts, not about permanence.
    correct_v1(
        conn,
        layer="belief",
        target=result["successor_id"],
        op="retract",
        body="the replacement was wrong too",
        actor="human",
        hard=False,
    )
    conn.commit()
    assert _restore_blocked(conn, old) is None
    assert restore(conn, belief_id=old)["changed"] is True
    assert _serving(conn) == {old}


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #
def test_a_full_reprojection_reproduces_the_serving_set(tmp_path: Path) -> None:
    """The projection is derived, so a rebuild has to land on the same corpus.

    Supersession touches status, serve, attributes, and the search index at
    once. If any of that were folded in a way a replay could not reproduce, the
    ledger would stop being the authority and the relational tables would start
    being it.
    """
    conn = _core(tmp_path)
    first = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    _seed(conn, belief_id="belief:sticky", body="Assignments are sticky per session.")
    step_one = _supersede(conn, first, "The research VM is reached with ssh asa2.")
    step_two = _supersede(
        conn, step_one["successor_id"], "The research VM is reached with ssh asa3 since the move."
    )
    correct_v1(
        conn, layer="belief", target="belief:sticky", op="pin", body=None, actor="human", hard=False
    )
    conn.commit()

    def snapshot() -> list[tuple]:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT belief_id, status, serve, pinned, confidence, attributes_json "
                "FROM current_beliefs ORDER BY belief_id"
            )
        ]

    def indexed() -> list[str]:
        return sorted(str(row[0]) for row in conn.execute("SELECT doc_id FROM search_documents"))

    before = snapshot()
    before_index = indexed()
    project_core_v1(conn, full=True)
    conn.commit()

    assert snapshot() == before
    assert indexed() == before_index
    assert _serving(conn) == {step_two["successor_id"], "belief:sticky"}


# --------------------------------------------------------------------------- #
# annotate
# --------------------------------------------------------------------------- #
def test_annotate_merges_attributes_without_touching_service(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    target = _seed(
        conn,
        belief_id="belief:vm",
        body="The research VM is reached with ssh asa1.",
        attributes={"key": "vm-access", "hits": 3},
    )
    before = get_core_v1_belief(conn, target)

    correct_v1(
        conn,
        layer="belief",
        target=target,
        op="annotate",
        body="mined contradiction and a recomputed statistic",
        actor="maintenance:procmine",
        hard=False,
        attributes_patch={"contradicts": ["belief:other"], "hits": 11, "key": None},
    )
    conn.commit()

    after = get_core_v1_belief(conn, target)
    assert after["attributes"]["contradicts"] == ["belief:other"]
    # Recompute-and-replace, never increment: replaying the same event twice
    # cannot drift the number.
    assert after["attributes"]["hits"] == 11
    # A null value deletes its key rather than storing a null.
    assert "key" not in after["attributes"]
    for field in ("status", "serve", "body", "confidence"):
        assert after[field] == before[field]
    assert {str(row[0]) for row in conn.execute("SELECT doc_id FROM search_documents")} == {target}


def test_annotate_is_replay_stable(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    target = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    correct_v1(
        conn,
        layer="belief",
        target=target,
        op="annotate",
        body=None,
        actor="maintenance:procmine",
        hard=False,
        attributes_patch={"contradicts": ["belief:other"], "hits": 11},
    )
    conn.commit()
    before = json.loads(
        conn.execute(
            "SELECT attributes_json FROM current_beliefs WHERE belief_id=?", (target,)
        ).fetchone()[0]
    )

    project_core_v1(conn, full=True)
    conn.commit()

    after = json.loads(
        conn.execute(
            "SELECT attributes_json FROM current_beliefs WHERE belief_id=?", (target,)
        ).fetchone()[0]
    )
    assert after == before


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
