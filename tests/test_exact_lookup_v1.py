"""Exact-locator retrieval: brain.search short-circuits semantic ranking."""

from __future__ import annotations

from ocbrain.closeout import record_closeout
from ocbrain.core_v1 import (
    append_core_event,
    init_core_v1,
    record_core_v1_evidence,
    sha256_text,
)
from ocbrain.db import connect
from ocbrain.mcp_v1 import exact_lookup_v1, search_v1
from ocbrain.scope import HOSTED_MODEL_TARGET, ScopeContext, ScopeTag

ARTIFACT_URI = "file:///tmp/exact-lookup-proof.txt"
ARTIFACT_SHA256 = sha256_text("exact-lookup-proof")


def _seed(tmp_path):
    conn = connect(tmp_path / "exact-v1.sqlite")
    init_core_v1(conn)
    scope = ScopeTag("project", "project:ocbrain")
    evidence_id, event_id = record_core_v1_evidence(
        conn,
        body="Exact lookup proof evidence body.",
        kind="observation",
        scope=scope,
        writer="test",
        artifact_ref=ARTIFACT_URI,
    )
    receipt = record_closeout(
        conn,
        task_ref="mission-exact-lookup",
        status="completed",
        summary="Exact lookup mission verified.",
        context=ScopeContext(project="ocbrain"),
        decision_impact="informed",
        artifact_refs=[
            {"uri": ARTIFACT_URI, "kind": "file", "sha256": ARTIFACT_SHA256},
        ],
    )
    conn.commit()
    return conn, evidence_id, event_id, receipt


def _search(conn, query, **kwargs):
    context = kwargs.pop("context", ScopeContext(project="ocbrain"))
    payload = search_v1(
        conn,
        query,
        context=context,
        limit=10,
        cross_scope=kwargs.pop("cross_scope", False),
    )
    conn.commit()
    return payload


def test_exact_lookup_by_closeout_task_ref(tmp_path):
    conn, _evidence_id, _event_id, receipt = _seed(tmp_path)
    payload = _search(conn, "mission-exact-lookup")
    assert payload["match_mode"] == "exact"
    assert payload["items"] == []
    match = payload["exact_matches"][0]
    assert match["kind"] == "closeout"
    assert match["id"] == receipt["id"]
    assert match["matched_by"] == "task_ref"
    assert match["status"] == "completed"
    assert payload["retrieval_use_id"].startswith("ret_")


def test_exact_lookup_by_evidence_and_event_id(tmp_path):
    conn, evidence_id, event_id, _receipt = _seed(tmp_path)
    by_evidence = _search(conn, evidence_id)
    assert by_evidence["match_mode"] == "exact"
    kinds = {match["kind"] for match in by_evidence["exact_matches"]}
    assert "evidence" in kinds
    evidence_match = next(
        match for match in by_evidence["exact_matches"] if match["kind"] == "evidence"
    )
    assert evidence_match["artifact_uri"] == ARTIFACT_URI

    by_event = _search(conn, event_id)
    assert by_event["match_mode"] == "exact"
    event_match = next(
        match for match in by_event["exact_matches"] if match["kind"] == "event"
    )
    assert event_match["event_kind"] == "evidence_recorded"


def test_exact_lookup_by_artifact_uri_and_sha256(tmp_path):
    conn, evidence_id, _event_id, receipt = _seed(tmp_path)
    by_uri = _search(conn, ARTIFACT_URI)
    assert by_uri["match_mode"] == "exact"
    by_kind = {match["kind"] for match in by_uri["exact_matches"]}
    assert "evidence" in by_kind
    assert "closeout" in by_kind

    by_hash = _search(conn, ARTIFACT_SHA256)
    assert by_hash["match_mode"] == "exact"
    closeout_matches = [
        match for match in by_hash["exact_matches"] if match["kind"] == "closeout"
    ]
    assert closeout_matches
    assert closeout_matches[0]["id"] == receipt["id"]
    assert closeout_matches[0]["matched_by"] == "artifact_sha256"
    # The evidence content hash also resolves to the evidence object.
    content_hash = sha256_text("Exact lookup proof evidence body.")
    by_content = _search(conn, content_hash)
    assert by_content["match_mode"] == "exact"
    assert any(
        match["kind"] == "evidence" and match["id"] == evidence_id
        for match in by_content["exact_matches"]
    )


def test_exact_lookup_by_stored_opaque_artifact_uri(tmp_path):
    conn, _evidence_id, _event_id, receipt = _seed(tmp_path)
    scope = ScopeTag("project", "project:ocbrain")
    opaque_refs = (
        "ocbrain-bundle:sha256:" + ("a" * 64),
        f"closeout:{receipt['id']}",
    )

    for index, artifact_ref in enumerate(opaque_refs):
        evidence_id, _event_id = record_core_v1_evidence(
            conn,
            body=f"Opaque artifact locator proof {index}.",
            kind="observation",
            scope=scope,
            writer="test",
            artifact_ref=artifact_ref,
        )
        conn.commit()
        payload = _search(conn, artifact_ref)
        assert payload["match_mode"] == "exact"
        assert any(
            match["kind"] == "evidence" and match["id"] == evidence_id
            for match in payload["exact_matches"]
        )


def test_semantic_search_is_not_hijacked_by_plain_queries(tmp_path):
    conn, _evidence_id, _event_id, _receipt = _seed(tmp_path)
    for query in ("how does exact lookup work", "project:ocbrain", "status:ready"):
        payload = _search(conn, query)
        assert "match_mode" not in payload
        assert "exact_matches" not in payload


def test_missing_exact_shaped_locator_never_falls_through_to_semantic_search(
    tmp_path, monkeypatch
):
    conn, _evidence_id, _event_id, _receipt = _seed(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("exact-shaped locator reached semantic retrieval")

    monkeypatch.setattr("ocbrain.mcp_v1.build_context_v1", forbidden)

    for locator in (
        "belief_ffffffffffffffff",
        "f" * 64,
        "file:///tmp/does-not-exist.txt",
        "ocbrain-bundle:sha256:" + ("f" * 64),
        "closeout:close_ffffffffffffffff",
    ):
        payload = _search(conn, locator)
        assert payload["match_mode"] == "exact"
        assert payload["items"] == []
        assert payload["exact_matches"] == []
        assert payload["coverage"]["returned"] == 0
        assert payload["coverage"]["feedback_needed"] is False


def test_exact_lookup_does_not_match_retrieval_use_task_refs(tmp_path):
    conn, _evidence_id, _event_id, _receipt = _seed(tmp_path)
    # A repeated identical query must not exact-match its own prior retrieval
    # receipt (retrieval_uses.task_ref is auto-derived from query text).
    first = _search(conn, "how does exact lookup work")
    assert "match_mode" not in first
    second = _search(conn, "how does exact lookup work")
    assert "match_mode" not in second


def test_exact_locators_resolve_locally_from_any_scope(tmp_path):
    """An id the caller already holds resolves, whatever project they name.

    Requiring the caller's context to match the record's scope meant an id
    copied out of a handoff note resolved to nothing, which reads as "no such
    record" rather than "not yours". Locators are not a discovery channel: the
    caller cannot guess a stable id they were never given.
    """
    conn, evidence_id, event_id, receipt = _seed(tmp_path)
    foreign_context = ScopeContext(project="other-project")

    first = _search(conn, receipt["id"])
    retrieval_id = first["retrieval_use_id"]
    locators = (
        evidence_id,
        event_id,
        receipt["id"],
        receipt["task_ref"],
        retrieval_id,
        ARTIFACT_URI,
        ARTIFACT_SHA256,
    )

    for locator in locators:
        assert exact_lookup_v1(conn, locator, context=foreign_context) != []
        assert exact_lookup_v1(conn, locator, context=ScopeContext()) != []


def test_exact_lookup_still_gates_hosted_delivery_and_confidential_material(tmp_path):
    conn, evidence_id, event_id, receipt = _seed(tmp_path)
    local_context = ScopeContext(project="ocbrain")

    first = _search(conn, receipt["id"])
    locators = (
        evidence_id,
        event_id,
        receipt["id"],
        receipt["task_ref"],
        first["retrieval_use_id"],
        ARTIFACT_URI,
        ARTIFACT_SHA256,
    )

    # Closeout and retrieval receipts never leave the machine. Evidence and
    # event hits still pass their own egress policies, which this fixture
    # deliberately sets to local_only.
    for locator in locators:
        assert (
            exact_lookup_v1(
                conn,
                locator,
                context=local_context,
                delivery_target=HOSTED_MODEL_TARGET,
            )
            == []
        )

    # Widening reach did not widen confidentiality: a confidential record in
    # another project stays invisible even to its exact id.
    confidential_id, confidential_event = record_core_v1_evidence(
        conn,
        body="Confidential evidence in a project the caller did not name.",
        kind="observation",
        scope=ScopeTag(
            "project",
            "project:someone-else",
            visibility="confidential",
            egress_policy="local_only",
        ),
        writer="test",
    )
    conn.commit()
    foreign_context = ScopeContext(project="other-project")
    assert exact_lookup_v1(conn, confidential_id, context=foreign_context) == []
    assert exact_lookup_v1(conn, confidential_event, context=foreign_context) == []
    # Its own project still reaches it.
    owner = ScopeContext(project="someone-else")
    assert exact_lookup_v1(conn, confidential_id, context=owner) != []


def test_hosted_exact_lookup_redacts_local_evidence_metadata(tmp_path):
    conn = connect(tmp_path / "hosted-exact-v1.sqlite")
    init_core_v1(conn)
    evidence_id, event_id = record_core_v1_evidence(
        conn,
        body="Hosted-safe evidence with a local-only source locator.",
        kind="observation",
        scope=ScopeTag(
            "project",
            "project:hosted-project",
            egress_policy="hosted_ok",
        ),
        writer="private-local-writer",
        artifact_ref="file:///Users/example/private/source.txt",
    )
    conn.commit()
    context = ScopeContext(project="hosted-project")

    evidence_matches = exact_lookup_v1(
        conn,
        evidence_id,
        context=context,
        delivery_target=HOSTED_MODEL_TARGET,
    )
    assert len(evidence_matches) == 1
    assert evidence_matches[0]["artifact_uri"] == f"ocbrain://evidence/{evidence_id}"
    assert "file:///Users/" not in str(evidence_matches)

    event_matches = exact_lookup_v1(
        conn,
        event_id,
        context=context,
        delivery_target=HOSTED_MODEL_TARGET,
    )
    assert event_matches[0]["kind"] == "event"
    assert "writer" not in event_matches[0]


def test_exact_lookup_inherits_scope_from_a_proposal_event(tmp_path):
    conn = connect(tmp_path / "proposal-event-v1.sqlite")
    init_core_v1(conn)
    scope = ScopeTag("project", "project:ocbrain")
    proposal_id = append_core_event(
        conn,
        "compilation_proposed",
        {
            "schema_version": "ocbrain.compilation.v1",
            "scope": scope.to_dict(),
            "subject": {"kind": "belief", "id": "belief:proposal-event"},
        },
        writer="test",
    )
    decision_id = append_core_event(
        conn,
        "compilation_decided",
        {
            "schema_version": "ocbrain.compilation-decision.v1",
            "subject": {"kind": "proposal", "id": proposal_id},
            "decision": "reject",
        },
        writer="test",
    )
    conn.commit()

    matches = exact_lookup_v1(
        conn,
        decision_id,
        context=ScopeContext(project="ocbrain"),
    )
    assert matches[0]["id"] == decision_id
    assert matches[0]["kind"] == "event"

    # The decision event carries no scope of its own; it inherits the proposal's.
    # Make that parent confidential and the child must become unreachable too,
    # which is what proves the walk is still consulted rather than skipped.
    confidential_proposal = append_core_event(
        conn,
        "compilation_proposed",
        {
            "schema_version": "ocbrain.compilation.v1",
            "scope": ScopeTag(
                "project",
                "project:someone-else",
                visibility="confidential",
                egress_policy="local_only",
            ).to_dict(),
            "subject": {"kind": "belief", "id": "belief:confidential-proposal"},
        },
        writer="test",
    )
    confidential_decision = append_core_event(
        conn,
        "compilation_decided",
        {
            "schema_version": "ocbrain.compilation-decision.v1",
            "subject": {"kind": "proposal", "id": confidential_proposal},
            "decision": "reject",
        },
        writer="test",
    )
    conn.commit()
    assert (
        exact_lookup_v1(
            conn,
            confidential_decision,
            context=ScopeContext(project="other-project"),
        )
        == []
    )


def test_exact_lookup_records_the_real_object_kind(tmp_path):
    conn, _evidence_id, _event_id, receipt = _seed(tmp_path)
    payload = _search(conn, receipt["id"])
    row = conn.execute(
        "SELECT object_id, object_kind FROM retrieval_items "
        "WHERE retrieval_use_id=? AND rank=0",
        (payload["retrieval_use_id"],),
    ).fetchone()
    assert dict(row) == {"object_id": receipt["id"], "object_kind": "closeout"}


def test_exact_lookup_rejects_oversized_or_blank_queries(tmp_path):
    conn, _evidence_id, _event_id, _receipt = _seed(tmp_path)
    assert exact_lookup_v1(conn, "   ", context=ScopeContext()) == []
    assert exact_lookup_v1(conn, "x" * 600, context=ScopeContext()) == []


def test_exact_lookup_by_closeout_id_and_retrieval_id(tmp_path):
    conn, _evidence_id, _event_id, receipt = _seed(tmp_path)
    payload = _search(conn, receipt["id"])
    assert payload["match_mode"] == "exact"
    assert payload["exact_matches"][0]["kind"] == "closeout"

    retrieval_id = payload["retrieval_use_id"]
    by_retrieval = _search(conn, retrieval_id)
    assert by_retrieval["match_mode"] == "exact"
    assert any(
        match["kind"] == "retrieval_use" and match["id"] == retrieval_id
        for match in by_retrieval["exact_matches"]
    )
