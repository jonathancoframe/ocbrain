"""What feedback is allowed to say, and how long a belief's record survives.

Two defects, one column. ``retrieval_uses.outcome`` carries both "the corpus had
nothing for this query" and "the corpus served the wrong thing", and feedback is
the only ranking signal the brain has. On the live core, 1,086 of 2,044
retrievals served zero items and 183 of those carry a relevance verdict anyway,
174 of them ``irrelevant`` -- filed against a written instruction not to file
them. An instruction that 183 rows ignore is not a rule, so the server enforces
it and records the zero-item case itself.

The second defect runs the other way: every curator pass mints a new belief_id,
so the retrieval history stayed behind on an id nothing serves. 373 of 575
ever-retrieved ids are retracted. These tests pin the successor inheriting its
ancestors' record, once, across a three-generation chain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocbrain.cli import main as cli_main
from ocbrain.core_v1 import (
    NO_COVERAGE_OUTCOME,
    SERVED_OUTCOME,
    _retrieval_feedback_scores,
    append_core_event,
    init_core_v1,
    reclassify_no_coverage_receipts,
    record_core_v1_evidence,
    record_core_v1_retrieval,
    retrieval_history_by_lineage,
)
from ocbrain.db import connect
from ocbrain.mcp_v1 import feedback_v1, supersede_v1
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


def _seed(conn, *, belief_id: str, body: str) -> str:
    evidence_id, _event = record_core_v1_evidence(
        conn,
        body=f"evidence for {belief_id}",
        kind="observation",
        scope=SCOPE,
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
            "scope": SCOPE.to_dict(),
            "confidence": 0.9,
            "attributes": {},
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


def _serve(conn, *belief_ids: str, query: str) -> str:
    """Record one retrieval receipt naming these beliefs, or none of them."""
    retrieval_id = record_core_v1_retrieval(
        conn,
        query=query,
        context=CONTEXT.to_dict(),
        items=[{"belief_id": belief_id, "score": 1.0} for belief_id in belief_ids],
        runtime="test",
        task_ref="test-task",
        session_id="session-1",
    )
    conn.commit()
    return retrieval_id


def _outcome(conn, retrieval_id: str) -> str:
    return str(
        conn.execute(
            "SELECT outcome FROM retrieval_uses WHERE id=?", (retrieval_id,)
        ).fetchone()[0]
    )


# --------------------------------------------------------------------------- #
# Defect 1 -- an empty retrieval is not a bad retrieval
# --------------------------------------------------------------------------- #
def test_the_server_records_a_zero_item_read_as_no_coverage(tmp_path: Path) -> None:
    """The item count is observed where the row is written, not reported later."""
    conn = _core(tmp_path)
    belief = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa2.")

    served = _serve(conn, belief, query="how do I reach the research vm")
    empty = _serve(conn, query="what is the pager rotation")

    assert _outcome(conn, served) == SERVED_OUTCOME
    assert _outcome(conn, empty) == NO_COVERAGE_OUTCOME


def test_a_relevance_verdict_on_a_zero_item_retrieval_is_refused(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    empty = _serve(conn, query="what is the pager rotation")

    with pytest.raises(ValueError) as raised:
        feedback_v1(conn, empty, outcome="irrelevant", note="nothing came back")

    message = str(raised.value)
    assert "served zero items" in message
    # The error has to say what to do instead, or the caller files it anyway.
    assert "brain.ingest" in message
    assert NO_COVERAGE_OUTCOME in message
    # And the refusal leaves the receipt exactly as the server wrote it.
    assert _outcome(conn, empty) == NO_COVERAGE_OUTCOME
    row = conn.execute(
        "SELECT note, feedback_at FROM retrieval_uses WHERE id=?", (empty,)
    ).fetchone()
    assert row["note"] is None
    assert row["feedback_at"] is None


def test_no_coverage_cannot_be_filed_by_the_caller(tmp_path: Path) -> None:
    """Server-derived, because a caller-supplied count can disagree with the row."""
    conn = _core(tmp_path)
    belief = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa2.")
    served = _serve(conn, belief, query="how do I reach the research vm")

    for claimed in (NO_COVERAGE_OUTCOME, SERVED_OUTCOME):
        with pytest.raises(ValueError) as raised:
            feedback_v1(conn, served, outcome=claimed, note=None)
        assert "recorded by the server" in str(raised.value)

    assert _outcome(conn, served) == SERVED_OUTCOME


def test_feedback_on_a_served_retrieval_still_records_the_verdict(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    belief = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa2.")
    served = _serve(conn, belief, query="how do I reach the research vm")

    result = feedback_v1(conn, served, outcome="used", note="used the host name")

    assert result == {"retrieval_use_id": served, "outcome": "used", "served_items": 1}
    assert _outcome(conn, served) == "used"


def test_an_unknown_retrieval_id_is_still_a_not_found_error(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    with pytest.raises(ValueError, match="retrieval use not found"):
        feedback_v1(conn, "ret:nope", outcome="used", note=None)


# --------------------------------------------------------------------------- #
# Defect 1 -- the rows already written
# --------------------------------------------------------------------------- #
def _force_verdict(conn, retrieval_id: str, outcome: str) -> None:
    """Write a verdict the way the old server would have: no zero-item check."""
    conn.execute(
        "UPDATE retrieval_uses SET outcome=?, feedback_source='runtime_explicit' WHERE id=?",
        (outcome, retrieval_id),
    )
    conn.commit()


def test_reclassification_reports_by_default_and_spares_judged_packets(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    belief = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa2.")
    empty_one = _serve(conn, query="what is the pager rotation")
    empty_two = _serve(conn, query="who owns the staging cluster")
    judged = _serve(conn, belief, query="how do I reach the research vm")
    _force_verdict(conn, empty_one, "irrelevant")
    _force_verdict(conn, empty_two, "ignored")
    _force_verdict(conn, judged, "irrelevant")

    plan = reclassify_no_coverage_receipts(conn, apply=False)
    assert plan["candidates"] == 2
    assert plan["by_outcome"] == {"ignored": 1, "irrelevant": 1}
    assert plan["applied"] == 0
    assert plan["dry_run"] is True
    # A report writes nothing.
    assert _outcome(conn, empty_one) == "irrelevant"

    applied = reclassify_no_coverage_receipts(conn, apply=True)
    conn.commit()
    assert applied["applied"] == 2
    assert _outcome(conn, empty_one) == NO_COVERAGE_OUTCOME
    assert _outcome(conn, empty_two) == NO_COVERAGE_OUTCOME
    # The verdict on the packet that actually served an item is untouched: this
    # command repairs a category error, it does not launder bad reviews.
    assert _outcome(conn, judged) == "irrelevant"
    note = conn.execute(
        "SELECT note FROM retrieval_uses WHERE id=?", (empty_one,)
    ).fetchone()[0]
    assert "reclassified from irrelevant" in str(note)

    # Idempotent: nothing is left to reclassify.
    assert reclassify_no_coverage_receipts(conn, apply=False)["candidates"] == 0


def test_cli_feedback_repair_reports_then_applies(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    empty = _serve(conn, query="what is the pager rotation")
    _force_verdict(conn, empty, "irrelevant")
    conn.close()

    assert cli_main(["--db", str(db_path), "feedback-repair"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["action"] == "feedback-repair"
    assert report["candidates"] == 1
    assert report["applied"] == 0

    assert cli_main(["--db", str(db_path), "feedback-repair", "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] == 1
    assert applied["dry_run"] is False

    conn = connect(db_path)
    assert _outcome(conn, empty) == NO_COVERAGE_OUTCOME
    conn.close()


# --------------------------------------------------------------------------- #
# Defect 2 -- history survives the recompile that renames the belief
# --------------------------------------------------------------------------- #
def _chain(conn) -> tuple[str, str, str]:
    """Three generations of one fact, each supersession retiring the last."""
    gen1 = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa0.")
    second = supersede_v1(
        conn,
        target=gen1,
        body="The research VM is reached with ssh asa1.",
        reason="asa0 was retired in June",
        context=CONTEXT,
        actor="agent:test",
    )
    conn.commit()
    third = supersede_v1(
        conn,
        target=second["successor_id"],
        body="The research VM is reached with ssh asa2; asa1 was terminated.",
        reason="asa1 was terminated on 2026-08-20",
        context=CONTEXT,
        actor="agent:test",
    )
    conn.commit()
    return gen1, str(second["successor_id"]), str(third["successor_id"])


def test_a_three_generation_chain_inherits_its_ancestors_verdicts_once(tmp_path: Path) -> None:
    """Generation three carries generations one and two, counted exactly once.

    The count is the assertion that matters. ``prior_observations`` damping
    means a belief with one verdict barely moves and a belief with four moves
    four times as far, so an inheritance that silently doubled would be a
    ranking change disguised as a bugfix.
    """
    conn = _core(tmp_path)
    gen1, gen2, gen3 = _chain(conn)

    feedback_v1(conn, _serve(conn, gen1, query="reach the vm one"), outcome="used", note=None)
    feedback_v1(conn, _serve(conn, gen1, query="reach the vm two"), outcome="used", note=None)
    feedback_v1(conn, _serve(conn, gen2, query="reach the vm three"), outcome="helpful", note=None)
    conn.commit()

    history = retrieval_history_by_lineage(conn, {gen1, gen2, gen3})

    # gen1 is the oldest id: it owns two verdicts and inherits nothing.
    assert history[gen1] == {"n": 2, "signal": 2.0, "inherited_n": 0}
    # gen2 owns its own verdict and inherits gen1's two.
    assert history[gen2] == {"n": 3, "signal": 4.0, "inherited_n": 2}
    # gen3 has never been retrieved under its own id and inherits all three.
    assert history[gen3] == {"n": 3, "signal": 4.0, "inherited_n": 3}


def test_one_retrieval_serving_two_generations_counts_once(tmp_path: Path) -> None:
    """The lineage is a set of ids, not a sum over hops."""
    conn = _core(tmp_path)
    gen1, gen2, gen3 = _chain(conn)

    both = _serve(conn, gen1, gen2, query="reach the vm")
    feedback_v1(conn, both, outcome="used", note=None)
    conn.commit()

    history = retrieval_history_by_lineage(conn, {gen3})
    assert history[gen3] == {"n": 1, "signal": 1.0, "inherited_n": 1}


def test_a_verdict_the_belief_earned_itself_is_never_called_inherited(tmp_path: Path) -> None:
    """One retrieval serving the successor and an ancestor is the successor's."""
    conn = _core(tmp_path)
    gen1, _gen2, gen3 = _chain(conn)

    feedback_v1(conn, _serve(conn, gen3, gen1, query="reach the vm"), outcome="used", note=None)
    conn.commit()

    assert retrieval_history_by_lineage(conn, {gen3})[gen3] == {
        "n": 1,
        "signal": 1.0,
        "inherited_n": 0,
    }


def test_the_feedback_boost_moves_with_the_inherited_count(tmp_path: Path) -> None:
    """Same weights as ranking uses, asserted as numbers.

    Three ``used`` verdicts on retired ancestors: average 1.0, weight 0.125,
    damping 3/(3+3) = 0.5, so the successor's boost is 0.0625. Before this
    change the successor had no history of its own and scored 0.0 -- ranked as
    if the fact had never been served, on the day it was recompiled.
    """
    conn = _core(tmp_path)
    gen1, gen2, gen3 = _chain(conn)
    for index in range(3):
        feedback_v1(
            conn, _serve(conn, gen1, query=f"reach the vm {index}"), outcome="used", note=None
        )
    conn.commit()

    scores = _retrieval_feedback_scores(
        conn, {gen3}, weight=0.125, clamp=0.25, prior_observations=3.0
    )
    assert scores == {gen3: pytest.approx(0.0625)}


def test_an_inherited_record_of_harm_still_hits_the_clamp(tmp_path: Path) -> None:
    """Inherited history is bounded by the same clamp as first-hand history.

    Six ``harmful`` verdicts on the ancestors: average -4.0, weight 0.125,
    damping 6/(6+3), which is -0.333 before the clamp and -0.25 after it. A
    successor cannot be pushed further down by an ancestor's record than it
    could be by its own.
    """
    conn = _core(tmp_path)
    gen1, _gen2, gen3 = _chain(conn)
    for index in range(6):
        feedback_v1(
            conn, _serve(conn, gen1, query=f"harmful vm {index}"), outcome="harmful", note=None
        )
    conn.commit()

    clamped = _retrieval_feedback_scores(
        conn, {gen3}, weight=0.125, clamp=0.25, prior_observations=3.0
    )
    assert clamped[gen3] == pytest.approx(-0.25)


def test_a_belief_with_no_lineage_scores_exactly_as_before(tmp_path: Path) -> None:
    """The inheritance is additive: an only-generation belief is unchanged."""
    conn = _core(tmp_path)
    belief = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa2.")
    feedback_v1(conn, _serve(conn, belief, query="reach the vm"), outcome="used", note=None)
    conn.commit()

    history = retrieval_history_by_lineage(conn, {belief})
    assert history[belief] == {"n": 1, "signal": 1.0, "inherited_n": 0}
    scores = _retrieval_feedback_scores(
        conn, {belief}, weight=0.125, clamp=0.25, prior_observations=3.0
    )
    # average 1.0 * 0.125 * 1/(1+3)
    assert scores == {belief: pytest.approx(0.03125)}


def test_an_unjudged_retrieval_is_not_history(tmp_path: Path) -> None:
    """``served`` and ``no_coverage`` are not verdicts and must not damp one."""
    conn = _core(tmp_path)
    gen1, _gen2, gen3 = _chain(conn)
    _serve(conn, gen1, query="reach the vm")
    _serve(conn, query="unrelated question")
    conn.commit()

    assert retrieval_history_by_lineage(conn, {gen1, gen3}) == {}
    assert _retrieval_feedback_scores(conn, {gen3}) == {}
