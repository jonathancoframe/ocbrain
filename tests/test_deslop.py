"""Deslop: mechanical rules, repair safety, write-time prevention, volume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocbrain.cli import main as cli_main
from ocbrain.core_v1 import (
    append_core_event,
    get_core_v1_belief,
    get_core_v1_evidence,
    init_core_v1,
    record_core_v1_evidence,
)
from ocbrain.db import connect
from ocbrain.deslop import (
    ENFORCED_RULE_IDS,
    REWINDOW_HEAD_CHARS,
    RULE_IDS,
    apply_repair,
    apply_volume_eviction,
    find_slop,
    plan_volume_eviction,
    repair_is_subtractive,
    rewindowed_evidence_id,
    scan_beliefs,
    validate_repair,
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


def test_scan_is_deterministic_and_skips_pinned_beliefs(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    _seed(conn, belief_id="belief_clean", body=CLEAN, attributes={"lifecycle": "durable"})
    _seed(conn, belief_id="belief_fused", body=FUSED, attributes={"lifecycle": "durable"})
    conn.commit()
    first = scan_beliefs(conn)
    assert [item["belief_id"] for item in first] == ["belief_fused"]
    assert scan_beliefs(conn) == first
    conn.close()


# --- Repair safety ---------------------------------------------------------


def test_a_repair_may_not_invent_content() -> None:
    subtractive, invented = repair_is_subtractive(
        "The gateway runs as ai.hermes.gateway.",
        "The gateway runs as ai.hermes.gateway on port 8080.",
    )
    assert not subtractive
    assert "8080" in invented


def test_a_split_is_accepted_when_its_union_subsets_the_original() -> None:
    action, bodies, rejection = validate_repair(
        FUSED,
        {
            "action": "split",
            "reason": "three independent facts",
            "bodies": [
                "The gateway runs as ai.hermes.gateway.",
                "Control the gateway with launchctl.",
                "The gateway logs to /tmp/hermes.log.",
            ],
        },
    )
    assert (action, rejection) == ("split", None)
    assert len(bodies) == 3


def test_an_invented_token_is_rejected_before_anything_is_written() -> None:
    _, _, rejection = validate_repair(
        FUSED,
        {
            "action": "rewrite",
            "reason": "tightened",
            "bodies": ["The gateway runs as ai.hermes.gateway on port 8080 always."],
        },
    )
    assert rejection is not None
    assert rejection.startswith("repair_invented_content")


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"action": "rewrite", "bodies": ["a", "b"]}, "rewrite_must_return_one_body"),
        ({"action": "split", "bodies": ["x"]}, "split_must_return_two_to_five_bodies"),
        ({"action": "rewrite", "bodies": ["too short"]}, "body_length_out_of_range"),
        ({"action": "delete", "bodies": []}, "invalid_action"),
    ],
)
def test_malformed_repairs_are_rejected(response: dict, expected: str) -> None:
    _, _, rejection = validate_repair(FUSED, response)
    assert rejection == expected


def test_a_split_inherits_evidence_and_supersedes_the_original(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    evidence_id, _ = record_core_v1_evidence(
        conn, body=FUSED, kind="audit_finding", scope=SCOPE, writer="test"
    )
    _seed(
        conn,
        belief_id="belief_fused",
        body=FUSED,
        attributes={"lifecycle": "durable", "key": "hermes-gateway"},
        evidence_ids=[evidence_id],
    )
    conn.commit()

    outcome = apply_repair(
        conn,
        belief_id="belief_fused",
        action="split",
        bodies=[
            "The gateway runs as ai.hermes.gateway.",
            "The gateway logs to /tmp/hermes.log.",
        ],
        reason="three independent facts",
    )
    assert len(outcome["created"]) == 2
    for created_id in outcome["created"]:
        created = get_core_v1_belief(conn, created_id)
        assert created is not None
        # Provenance survives the repair: a split belief still cites the evidence
        # its source cited, so `brain.source` keeps working on it.
        assert created["evidence_ids"] == [evidence_id]
    original = get_core_v1_belief(conn, "belief_fused")
    assert original is not None
    assert original["attributes"]["superseded_by"] == outcome["created"][0]
    conn.close()


def test_a_rewrite_updates_the_belief_in_place(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    _seed(
        conn,
        belief_id="belief_x",
        body="The migration is now complete on ai.hermes.gateway.",
        attributes={"lifecycle": "durable", "key": "hermes-migration"},
    )
    conn.commit()
    outcome = apply_repair(
        conn,
        belief_id="belief_x",
        action="rewrite",
        bodies=["The migration on ai.hermes.gateway is complete."],
        reason="present tense in a durable belief",
    )
    rewritten = get_core_v1_belief(conn, outcome["created"][0])
    assert rewritten is not None
    assert rewritten["body"] == "The migration on ai.hermes.gateway is complete."
    assert rewritten["attributes"]["key"] == "hermes-migration"
    conn.close()


def test_a_drop_is_reversible(tmp_path: Path) -> None:
    from ocbrain.hygiene import restore

    conn = _core(tmp_path)
    _seed(conn, belief_id="belief_junk", body=CLEAN, attributes={"lifecycle": "durable"})
    conn.commit()
    apply_repair(
        conn, belief_id="belief_junk", action="drop", bodies=[], reason="nothing to act on"
    )
    dropped = get_core_v1_belief(conn, "belief_junk")
    assert dropped is not None
    assert dropped["status"] == "retracted"

    restore(conn, belief_id="belief_junk")
    restored = get_core_v1_belief(conn, "belief_junk")
    assert restored is not None
    assert restored["status"] == "current"
    assert restored["serve"]
    conn.close()


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


def test_eviction_spares_evidence_a_belief_still_cites(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    older, _ = record_core_v1_evidence(
        conn,
        body="older window",
        kind="codex_history_file",
        scope=SCOPE,
        writer="test",
        artifact_ref="/tmp/rollout.jsonl",
    )
    newer, _ = record_core_v1_evidence(
        conn,
        body="newer window",
        kind="codex_history_file",
        scope=SCOPE,
        writer="test",
        artifact_ref="/tmp/rollout.jsonl",
    )
    _seed(conn, belief_id="belief_cites_older", body=CLEAN, evidence_ids=[older])
    conn.commit()

    plan = plan_volume_eviction(conn)
    evicted = {target["evidence_id"] for target in plan["targets"]}
    assert older not in evicted
    assert newer not in evicted  # newest for its source
    conn.close()


def test_eviction_is_a_cache_drop_that_a_full_sync_restores(tmp_path: Path) -> None:
    from ocbrain.core_ops import sync_core

    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    for index in range(4):
        record_core_v1_evidence(
            conn,
            body=f"window {index}",
            kind="codex_history_file",
            scope=SCOPE,
            writer="test",
            artifact_ref="/tmp/rollout.jsonl",
        )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0]

    plan = apply_volume_eviction(conn, plan_volume_eviction(conn))
    assert plan["evicted"] == 3
    assert conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == before - 3
    conn.close()

    result = sync_core(db_path, full=True, max_events=1, time_budget_seconds=60.0)
    assert result["status"] == "ok"
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == before
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


# --- CLI -------------------------------------------------------------------


def test_the_cli_reports_mechanically_without_a_hosted_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    _seed(conn, belief_id="belief_clean", body=CLEAN, attributes={"lifecycle": "durable"})
    _seed(conn, belief_id="belief_fused", body=FUSED, attributes={"lifecycle": "durable"})
    conn.commit()
    conn.close()

    assert cli_main(["--db", str(db_path), "deslop", "--mechanical-only"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scanned"] == 2
    assert payload["flagged"] == 1
    assert payload["census"] == {"fused-claims": 1}
    assert payload["judged"] is False
    assert payload["repairs"] == []


def test_the_cli_reports_volume_without_evicting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    for index in range(3):
        record_core_v1_evidence(
            conn,
            body=f"window {index}",
            kind="codex_history_file",
            scope=SCOPE,
            writer="test",
            artifact_ref="/tmp/rollout.jsonl",
        )
    conn.commit()
    conn.close()

    assert cli_main(["--db", str(db_path), "deslop", "--volume"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == 2
    assert payload["reversible_by"] == "ocbrain sync --full"

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == 3
    conn.close()


def test_an_advisory_finding_alone_is_never_applied_unattended(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    _seed(
        conn,
        belief_id="belief_vague",
        body="the work was completed and everything looks fine",
        attributes={"lifecycle": "durable"},
    )
    conn.commit()
    conn.close()

    # --apply with only an advisory finding must not reach a hosted call, which
    # is what an unresolved credential would surface as.
    assert cli_main(["--db", str(db_path), "deslop", "--mechanical-only", "--apply"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["flagged"] == 1
    assert payload["actionable"] == 0
    assert payload["repairs"] == []


# --- Doctrine --------------------------------------------------------------


def test_the_doctrine_passes_the_rules_it_states() -> None:
    """A writing standard that trips its own linter is not a standard."""
    from ocbrain.deslop import DOCTRINE_BODY

    assert find_slop(DOCTRINE_BODY, {"lifecycle": "durable"}) == []


def test_installing_the_doctrine_is_idempotent_and_pins_it(tmp_path: Path) -> None:
    from ocbrain.deslop import DOCTRINE_BODY, install_doctrine

    conn = _core(tmp_path)
    first = install_doctrine(conn, project="bountiful")
    assert first["changed"]
    assert install_doctrine(conn, project="bountiful")["changed"] is False

    belief = get_core_v1_belief(conn, first["belief_id"])
    assert belief is not None
    assert belief["body"] == DOCTRINE_BODY
    # Pinned, so the sweep it describes can never retire it.
    assert bool(belief["pinned"])
    assert belief["serve"]
    conn.close()


def test_the_doctrine_is_retrievable_by_a_client_before_it_writes(tmp_path: Path) -> None:
    from ocbrain.core_v1 import search_core_v1
    from ocbrain.deslop import install_doctrine
    from ocbrain.scope import ScopeContext

    conn = _core(tmp_path)
    install_doctrine(conn, project="bountiful")
    result = search_core_v1(
        conn,
        query="how should I write a belief",
        context=ScopeContext(project="bountiful"),
        limit=3,
    )
    items = result["items"] if isinstance(result, dict) else result
    assert any("one fact per belief" in item["body"] for item in items)
    conn.close()


def test_the_scan_never_flags_the_doctrine(tmp_path: Path) -> None:
    """Pinned beliefs are outside the population, so doctrine is not recursive."""
    from ocbrain.deslop import install_doctrine

    conn = _core(tmp_path)
    install_doctrine(conn, project="bountiful")
    assert scan_beliefs(conn) == []
    conn.close()


def test_splitting_a_current_belief_gives_each_child_an_expiry(tmp_path: Path) -> None:
    """Otherwise repairing one finding multiplies another.

    Measured on a live corpus: nine splits turned nine `current-without-expiry`
    findings into thirteen, because each child inherited the parent's missing
    expiry.
    """
    conn = _core(tmp_path)
    body = "The runner holds 42 leases; the queue holds 7 tasks in /tmp/queue."
    _seed(
        conn,
        belief_id="belief_current",
        body=body,
        attributes={"lifecycle": "current", "key": "runner-state"},
    )
    conn.commit()
    outcome = apply_repair(
        conn,
        belief_id="belief_current",
        action="split",
        bodies=["The runner holds 42 leases.", "The queue holds 7 tasks in /tmp/queue."],
        reason="two independent facts",
        current_ttl_days=90,
    )
    for created_id in outcome["created"]:
        created = get_core_v1_belief(conn, created_id)
        assert created is not None
        assert created["attributes"]["valid_until"]
        assert find_slop(created["body"], created["attributes"]) == []
    conn.close()


def test_a_local_repair_runs_with_no_credentials_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stamp is applied locally, so an absent API key must not stop it.

    The sibling test below asserts the same behaviour but passes on any machine
    that happens to have a key configured, which is how a run that resolved
    credentials eagerly still looked green locally and failed in CI. Here the
    key is guaranteed missing, so the assertion can only pass if the credential
    path is never reached.
    """
    monkeypatch.setattr("ocbrain.curator.load_env_value", lambda *_args, **_kwargs: "")

    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    _seed(
        conn,
        belief_id="belief_current",
        body="The runner ai.hermes.gateway holds 42 leases.",
        attributes={"lifecycle": "current", "key": "runner-state"},
    )
    conn.commit()
    conn.close()

    assert cli_main(["--db", str(db_path), "deslop", "--mechanical-only", "--apply"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["action"] for r in payload["repairs"]] == ["stamp"]


def test_a_judged_run_still_demands_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferring resolution must not silently disable the guard.

    The judged pass genuinely calls a model, so a missing key there is still a
    hard, loud failure rather than a quietly skipped stage.
    """
    monkeypatch.setattr("ocbrain.curator.load_env_value", lambda *_args, **_kwargs: "")

    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    _seed(
        conn,
        belief_id="belief_current",
        body="The runner ai.hermes.gateway holds 42 leases.",
        attributes={"lifecycle": "current", "key": "runner-state"},
    )
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        cli_main(["--db", str(db_path), "deslop", "--apply"])


def test_a_missing_expiry_is_stamped_without_a_hosted_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The body is fine; only the lifecycle bookkeeping is missing.

    Routing this through a model would spend a hosted call to change the one
    thing that is not wrong, so `--apply` must fix it locally. An unresolvable
    API key would surface as a SystemExit if it reached the credential path.
    """
    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    body = "The runner ai.hermes.gateway holds 42 leases."
    _seed(
        conn,
        belief_id="belief_current",
        body=body,
        attributes={"lifecycle": "current", "key": "runner-state"},
    )
    conn.commit()
    conn.close()

    assert cli_main(["--db", str(db_path), "deslop", "--mechanical-only", "--apply"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["action"] for r in payload["repairs"]] == ["stamp"]

    conn = connect(db_path)
    stamped = get_core_v1_belief(conn, payload["repairs"][0]["created"][0])
    assert stamped is not None
    # The body is untouched and the finding is gone.
    assert stamped["body"] == body
    assert find_slop(stamped["body"], stamped["attributes"]) == []
    conn.close()


def test_an_in_place_repair_updates_a_belief_whose_id_is_not_key_derived(
    tmp_path: Path,
) -> None:
    """Re-deriving the id from the key forks a duplicate and leaves the original.

    Only the wiki curator mints ids as `stable_id("belief", "wiki", key, scope)`.
    A belief compiled by any other path has an id that formula does not reproduce,
    and on a live corpus five of thirteen stamps forked instead of updating.
    """
    conn = _core(tmp_path)
    _seed(
        conn,
        belief_id="belief_from_another_pipeline",
        body="The runner ai.hermes.gateway holds 42 leases.",
        attributes={"lifecycle": "current", "key": "runner-state"},
    )
    conn.commit()
    before = conn.execute(
        "SELECT COUNT(*) FROM current_beliefs WHERE status='current' AND serve=1"
    ).fetchone()[0]

    outcome = apply_repair(
        conn,
        belief_id="belief_from_another_pipeline",
        action="stamp",
        bodies=[],
        reason="lifecycle is current but no valid_until is set",
        current_ttl_days=90,
    )
    assert outcome["created"] == ["belief_from_another_pipeline"]
    after = conn.execute(
        "SELECT COUNT(*) FROM current_beliefs WHERE status='current' AND serve=1"
    ).fetchone()[0]
    assert after == before
    conn.close()
