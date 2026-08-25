"""Deslop: the mechanical rules and the write-time gates that enforce them."""

from __future__ import annotations

from pathlib import Path

import pytest

from ocbrain.core_v1 import (
    append_core_event,
    get_core_v1_evidence,
    init_core_v1,
    record_core_v1_evidence,
)
from ocbrain.db import connect
from ocbrain.deslop import (
    ENFORCED_RULE_IDS,
    REWINDOW_HEAD_CHARS,
    RULE_IDS,
    find_slop,
    rewindowed_evidence_id,
)
from ocbrain.mcp_v1 import decide_proposal_v1
from ocbrain.scope import ScopeTag

SCOPE = ScopeTag(
    "project",
    "project:bountiful",
    visibility="internal",
    egress_policy="local_only",
    provenance="test",
)

# One clean fact, and one of each defect the mechanical rules exist to catch.
CLEAN = "The gateway runs as the launchd service ai.hermes.gateway on this Mac."
FUSED = (
    "The gateway runs as ai.hermes.gateway; control it with launchctl; "
    "it logs to /tmp/hermes.log."
)


def _core(tmp_path: Path):
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    return conn


def _seed(
    conn,
    *,
    belief_id: str,
    body: str,
    attributes: dict | None = None,
    evidence_ids: list[str] | None = None,
) -> None:
    proposal = append_core_event(
        conn,
        "compilation_proposed",
        {
            "belief_id": belief_id,
            "belief_type": "wiki_fact",
            "body": body,
            "evidence_ids": evidence_ids or [],
            "scope": SCOPE.to_dict(),
            "confidence": 0.9,
            "attributes": attributes or {},
        },
        writer="test",
    )
    decide_proposal_v1(
        conn,
        proposal_event_id=proposal,
        decision="approve",
        actor="test",
        edited_body=None,
        reason="seed",
    )


# --- Mechanical rules ------------------------------------------------------


def test_a_clean_fact_trips_no_rule() -> None:
    assert find_slop(CLEAN, {"lifecycle": "durable"}) == []


@pytest.mark.parametrize(
    ("body", "attributes", "rule"),
    [
        (FUSED, {"lifecycle": "durable"}, "fused-claims"),
        (
            "The migration is now complete on ai.hermes.gateway.",
            {"lifecycle": "durable"},
            "temporal-in-durable",
        ),
        (
            "The service ai.hermes.gateway holds 42 leases.",
            {"lifecycle": "current"},
            "current-without-expiry",
        ),
        (
            "the work was completed and everything looks fine",
            {"lifecycle": "durable"},
            "no-checkable-content",
        ),
    ],
)
def test_each_rule_fires_on_its_own_defect(
    body: str, attributes: dict, rule: str
) -> None:
    assert rule in {finding.rule for finding in find_slop(body, attributes)}


def test_three_sentences_are_allowed_because_the_curator_asks_for_them() -> None:
    """The curator prompt requests "1-3 short sentences"; three must be compliant.

    A stricter bar here would flag beliefs for meeting the contract they were
    written to, which is how a linter loses its readers.
    """
    body = (
        "The gateway runs as ai.hermes.gateway. It restarts on failure. "
        "Logs land in /tmp/hermes.log."
    )
    assert find_slop(body, {"lifecycle": "durable"}) == []


def test_a_current_belief_with_an_expiry_is_clean() -> None:
    attributes = {"lifecycle": "current", "valid_until": "2027-01-01T00:00:00+00:00"}
    assert find_slop("The service ai.hermes.gateway holds 42 leases.", attributes) == []


def test_a_named_entity_counts_as_checkable_content() -> None:
    """Preference and doctrine beliefs name no path or figure and are still real."""
    assert find_slop("Prefer the gcloud CLI over kubectl for prod infra.", {}) == []


def test_no_checkable_content_is_advisory_not_enforced() -> None:
    """It cannot tell a sentence-initial proper noun from a common word.

    "Jonathan wants short answers" is actionable and fires anyway, so the rule
    reports to a human and never gates a write or retires a belief unattended.
    """
    assert "no-checkable-content" in RULE_IDS
    assert "no-checkable-content" not in ENFORCED_RULE_IDS


# --- Write-time prevention -------------------------------------------------


def test_the_curator_rejects_a_fused_claim_before_it_becomes_a_belief() -> None:
    from ocbrain.curator import validate_claims

    evidence = [{"evidence_id": "evd1", "body": FUSED}]
    claims, rejected = validate_claims(
        {
            "beliefs": [
                {
                    "key": "hermes-gateway",
                    "title": "Hermes gateway",
                    "body": FUSED,
                    "category": "system",
                    "lifecycle": "durable",
                    "confidence": 0.9,
                    "supports": [{"evidence_id": "evd1", "quote": "ai.hermes.gateway"}],
                }
            ]
        },
        evidence=evidence,
        max_beliefs=5,
    )
    assert claims == []
    assert rejected == [{"item": "0", "reason": "slop:fused-claims"}]


def test_the_curator_gate_skips_the_expiry_rule_it_cannot_yet_check() -> None:
    """`valid_until` is assigned in `apply_claims`, after validation."""
    from ocbrain.curator import CLAIM_SLOP_RULES

    assert "current-without-expiry" not in CLAIM_SLOP_RULES


def test_a_slopped_closeout_is_reported_not_refused(tmp_path: Path) -> None:
    from ocbrain.mcp_v1 import closeout_v1
    from ocbrain.scope import ScopeContext

    conn = _core(tmp_path)
    receipt = closeout_v1(
        conn,
        task_ref="task:demo",
        status="completed",
        summary=FUSED,
        context=ScopeContext(project="bountiful"),
        retrieval_use_ids=[],
        decision_impact="none",
        decision_note=None,
        artifact_refs=[],
        verifier_refs=[],
        actions=[],
        outcomes=[],
        awaiting=None,
        actor="test",
    )
    assert [f["rule"] for f in receipt["slop_findings"]] == ["fused-claims"]
    # The work is not lost: the summary is still recorded as evidence.
    assert get_core_v1_evidence(conn, receipt["evidence_id"]) is not None
    conn.close()


def test_hardening_the_closeout_gate_refuses_before_anything_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ocbrain import mcp_v1
    from ocbrain.config import DeslopConfig, OcbrainConfig
    from ocbrain.mcp_v1 import closeout_v1
    from ocbrain.scope import ScopeContext

    monkeypatch.setattr(
        mcp_v1,
        "load_config",
        lambda: OcbrainConfig(deslop=DeslopConfig(reject_closeout_slop=True)),
    )
    conn = _core(tmp_path)
    with pytest.raises(ValueError, match="fused-claims"):
        closeout_v1(
            conn,
            task_ref="task:demo",
            status="completed",
            summary=FUSED,
            context=ScopeContext(project="bountiful"),
            retrieval_use_ids=[],
            decision_impact="none",
            decision_note=None,
            artifact_refs=[],
            verifier_refs=[],
            actions=[],
            outcomes=[],
            awaiting=None,
            actor="test",
        )
    assert conn.execute("SELECT COUNT(*) FROM task_closeouts").fetchone()[0] == 0
    conn.close()


# --- Volume slop -----------------------------------------------------------


def _window(head: str, tail: str) -> str:
    """A body shaped like the importer's output: a fixed head plus a tail."""
    return head.ljust(REWINDOW_HEAD_CHARS, ".") + tail


def test_a_rewindowed_transcript_reuses_the_evidence_it_already_has(
    tmp_path: Path,
) -> None:
    conn = _core(tmp_path)
    first = _window("session-abc ", "turn 1")
    evidence_id, _ = record_core_v1_evidence(
        conn,
        body=first,
        kind="codex_history_file",
        scope=SCOPE,
        writer="test",
        artifact_ref="/tmp/rollout.jsonl",
    )
    conn.commit()

    grown = _window("session-abc ", "turn 1 turn 2 turn 3")
    assert (
        rewindowed_evidence_id(
            conn,
            source_uri="/tmp/rollout.jsonl",
            kind="codex_history_file",
            text=grown,
        )
        == evidence_id
    )
    # A rotated file has a different head, so its content really is new.
    rotated = _window("session-xyz ", "turn 1")
    assert (
        rewindowed_evidence_id(
            conn,
            source_uri="/tmp/rollout.jsonl",
            kind="codex_history_file",
            text=rotated,
        )
        is None
    )
    conn.close()


def test_a_short_body_is_never_treated_as_rewindowed(tmp_path: Path) -> None:
    """Below the window size the body *is* the file, so any change is real."""
    conn = _core(tmp_path)
    record_core_v1_evidence(
        conn,
        body="short body",
        kind="memory_file",
        scope=SCOPE,
        writer="test",
        artifact_ref="/tmp/notes.md",
    )
    conn.commit()
    assert (
        rewindowed_evidence_id(
            conn, source_uri="/tmp/notes.md", kind="memory_file", text="short body plus more"
        )
        is None
    )
    conn.close()


def test_reharvesting_an_appended_transcript_records_no_new_evidence(
    tmp_path: Path,
) -> None:
    from ocbrain.cli import import_source_v1

    conn = _core(tmp_path)
    path = tmp_path / "rollout.jsonl"

    def _import(text: str) -> dict:
        return import_source_v1(
            conn,
            path=path,
            text=text,
            title="Codex rollout",
            source_type="codex_history_file",
            runtime="codex",
            project="bountiful",
            privacy_scope="workspace",
            confidence=0.55,
        )

    path.write_text("seed", encoding="utf-8")
    first = _import(_window("session-abc ", "turn 1"))
    assert first["changed"]
    baseline = conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0]

    second = _import(_window("session-abc ", "turn 1 turn 2"))
    assert second["evidence_id"] == first["evidence_id"]
    # Nothing new in either table: reusing only the id would still re-propose the
    # belief every harvest, appending the transcript to the ledger again.
    assert not second["changed"]
    assert conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == baseline

    third = _import(_window("session-xyz ", "turn 1"))
    assert third["evidence_id"] != first["evidence_id"]
    assert conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == baseline + 1
    conn.close()


def test_a_full_sync_is_not_refused_for_exceeding_the_event_bound(
    tmp_path: Path,
) -> None:
    """Refusing it would leave the projection in the state the operator asked to fix."""
    from ocbrain.core_ops import sync_core

    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    # Leave events unprojected so the incremental bound has something to refuse.
    for index in range(6):
        append_core_event(
            conn,
            "evidence_recorded",
            {
                "subject": {"kind": "evidence", "id": f"evd_{index}"},
                "evidence_id": f"evd_{index}",
                "body": f"body {index}",
                "kind": "audit_finding",
                "scope": SCOPE.to_dict(),
            },
            writer="test",
            project=False,
        )
    conn.commit()
    conn.close()

    bounded = sync_core(db_path, max_events=1, time_budget_seconds=60.0)
    assert bounded["status"] == "bounded_refusal"
    forced = sync_core(db_path, full=True, max_events=1, time_budget_seconds=60.0)
    assert forced["status"] == "ok"
