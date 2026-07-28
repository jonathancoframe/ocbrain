"""Exact-locator retrieval: brain.search short-circuits semantic ranking."""

from __future__ import annotations

from ocbrain.closeout import record_closeout
from ocbrain.core_v1 import (
    init_core_v1,
    record_core_v1_evidence,
    sha256_text,
)
from ocbrain.db import connect
from ocbrain.mcp_v1 import exact_lookup_v1, search_v1
from ocbrain.scope import ScopeContext, ScopeTag

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


def test_semantic_search_is_not_hijacked_by_plain_queries(tmp_path):
    conn, _evidence_id, _event_id, _receipt = _seed(tmp_path)
    payload = _search(conn, "how does exact lookup work")
    assert "match_mode" not in payload
    assert "exact_matches" not in payload


def test_exact_lookup_does_not_match_retrieval_use_task_refs(tmp_path):
    conn, _evidence_id, _event_id, _receipt = _seed(tmp_path)
    # A repeated identical query must not exact-match its own prior retrieval
    # receipt (retrieval_uses.task_ref is auto-derived from query text).
    first = _search(conn, "how does exact lookup work")
    assert "match_mode" not in first
    second = _search(conn, "how does exact lookup work")
    assert "match_mode" not in second


def test_exact_lookup_respects_scope_gating(tmp_path):
    conn, _evidence_id, _event_id, _receipt = _seed(tmp_path)
    # The evidence lives in project:ocbrain; a caller scoped to another
    # project without cross_scope must not receive it.
    matches = exact_lookup_v1(
        conn,
        ARTIFACT_URI,
        context=ScopeContext(project="other-project"),
    )
    assert all(match["kind"] != "evidence" for match in matches)


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
