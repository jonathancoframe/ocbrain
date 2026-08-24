"""Empty-result cross-scope fallback.

``cross_scope`` is an opt-in almost no caller sends, so a question the brain
could answer from a neighbouring project abstained on a technicality. When the
scoped pass returns nothing, retrieval retries once across scopes.

What these tests exist to hold down is the difference between widening reach and
weakening judgement. The retry runs the same primitive with the same dense
floors, the same multi-term lexical bar, the same redundancy filter and dedup; a
query the brain genuinely cannot answer must still come back empty, and client
and confidential inventory must stay invisible in both passes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocbrain.core_v1 import append_core_event, init_core_v1, search_core_v1
from ocbrain.db import connect
from ocbrain.mcp_v1 import build_context_v1, decide_proposal_v1
from ocbrain.scope import ScopeContext, ScopeTag

TRINO_FACT = "Trino sandbox schemas are the scratch tier for research queries."


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCBRAIN_CONFIG", str(tmp_path / "absent-config.json"))
    monkeypatch.delenv("OCBRAIN_RETRIEVAL_SCOPE_FALLBACK_ENABLED", raising=False)
    monkeypatch.delenv("OCBRAIN_VECTOR_DB", raising=False)


def _seed_belief(conn, *, belief_id: str, body: str, scope: ScopeTag) -> None:
    proposal = append_core_event(
        conn,
        "compilation_proposed",
        {
            "belief_id": belief_id,
            "belief_type": "curated_fact",
            "body": body,
            "evidence_ids": [],
            "scope": scope.to_dict(),
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
        reason="scope fallback fixture",
    )


def _seed_core(tmp_path: Path):
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    _seed_belief(
        conn,
        belief_id="curated:neighbour:trino",
        body=TRINO_FACT,
        scope=ScopeTag("project", "project:neighbour", provenance="test"),
    )
    _seed_belief(
        conn,
        belief_id="curated:bihua:client-note",
        body="PRIVATE_CLIENT_SENTINEL trino sandbox schemas for the client engagement.",
        scope=ScopeTag(
            "client",
            "client:bihua",
            visibility="confidential",
            egress_policy="local_only",
            provenance="test",
        ),
    )
    _seed_belief(
        conn,
        belief_id="curated:secret:programme",
        body="PRIVATE_CONFIDENTIAL_SENTINEL trino sandbox schemas under embargo.",
        scope=ScopeTag(
            "project",
            "project:embargoed",
            visibility="confidential",
            egress_policy="local_only",
            provenance="test",
        ),
    )
    conn.commit()
    return conn


def _context(conn, query: str, *, project: str, cross_scope: bool = False, limit: int = 10):
    return build_context_v1(
        conn,
        query,
        context=ScopeContext(project=project),
        limit=limit,
        cross_scope=cross_scope,
        delivery_target="local_model",
    )


def test_empty_scoped_result_falls_back_cross_scope_and_marks_coverage(tmp_path: Path) -> None:
    conn = _seed_core(tmp_path)

    packet, handles = _context(conn, "trino sandbox scratch tier", project="unrelated")

    assert [item["id"] for item in packet["items"]] == ["curated:neighbour:trino"]
    assert packet["coverage"]["scope_fallback"] == {
        "mode": "cross_scope_auto",
        "first_pass_eligible_count": 0,
        "first_pass_excluded_scope_count": 3,
    }
    # The caller asked for a scoped read and is told so; the widening is
    # coverage detail, not a silent rewrite of their request.
    assert packet["cross_scope"] is False
    # The item is visibly foreign; nothing pretends it belongs to this scope.
    assert packet["items"][0]["scope"]["scope_id"] == "project:neighbour"
    assert packet["coverage"]["returned"] == 1
    assert handles or packet["coverage"]["unavailable_sources"]
    conn.close()


def test_fallback_preserves_abstention_gates(tmp_path: Path) -> None:
    """A question nothing answers still comes back empty, in both passes."""
    conn = _seed_core(tmp_path)

    packet, _handles = _context(conn, "quartz zeppelin nonsense", project="unrelated")

    assert packet["items"] == []
    assert packet["coverage"]["returned"] == 0
    assert packet["coverage"]["feedback_needed"] is False
    # The retry ran and found nothing; the accounting stays the caller's own.
    assert packet["coverage"]["scope_fallback"]["mode"] == "cross_scope_auto"
    assert packet["coverage"]["ranking"]["eligible_count"] == 0
    assert packet["coverage"]["excluded_scope_count"] == 3
    conn.close()


def test_fallback_never_serves_confidential_or_client_scope(tmp_path: Path) -> None:
    conn = _seed_core(tmp_path)

    packet, _handles = _context(conn, "trino sandbox schemas client embargo", project="unrelated")

    encoded = json.dumps(packet)
    assert "PRIVATE_CLIENT_SENTINEL" not in encoded
    assert "PRIVATE_CONFIDENTIAL_SENTINEL" not in encoded
    assert "curated:bihua:client-note" not in encoded
    assert "curated:secret:programme" not in encoded
    assert {item["id"] for item in packet["items"]} <= {"curated:neighbour:trino"}
    conn.close()


def test_fallback_disabled_by_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCBRAIN_RETRIEVAL_SCOPE_FALLBACK_ENABLED", "false")
    conn = _seed_core(tmp_path)

    packet, _handles = _context(conn, "trino sandbox scratch tier", project="unrelated")

    assert packet["items"] == []
    assert "scope_fallback" not in packet["coverage"]
    conn.close()


def test_no_fallback_when_scoped_pass_returns_items(tmp_path: Path) -> None:
    """A scoped answer is never diluted by a wider one."""
    conn = _seed_core(tmp_path)
    _seed_belief(
        conn,
        belief_id="curated:mine:trino",
        body="Trino sandbox schemas graduate to analytics only with sign-off.",
        scope=ScopeTag("project", "project:mine", provenance="test"),
    )
    conn.commit()

    packet, _handles = _context(conn, "trino sandbox schemas", project="mine")

    assert [item["id"] for item in packet["items"]] == ["curated:mine:trino"]
    assert "scope_fallback" not in packet["coverage"]
    assert "curated:neighbour:trino" not in json.dumps(packet)
    conn.close()


def test_explicit_cross_scope_true_never_double_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _seed_core(tmp_path)
    calls: list[bool] = []

    def counting_search(*args, **kwargs):
        calls.append(bool(kwargs.get("cross_scope")))
        return search_core_v1(*args, **kwargs)

    monkeypatch.setattr("ocbrain.mcp_v1.search_core_v1", counting_search)

    packet, _handles = _context(
        conn, "quartz zeppelin nonsense", project="unrelated", cross_scope=True
    )

    assert packet["items"] == []
    assert calls == [True]
    assert "scope_fallback" not in packet["coverage"]
    assert packet["cross_scope"] is True
    conn.close()


def test_fallback_marker_survives_packet_budgeting(tmp_path: Path) -> None:
    """The marker must outlive the trim, or the coverage lies about the pass."""
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    for index in range(30):
        _seed_belief(
            conn,
            belief_id=f"curated:neighbour:long-{index:02d}",
            body=f"matching orchard fact {index} " + ("verified detail " * 400),
            scope=ScopeTag("project", "project:neighbour", provenance="test"),
        )
    conn.commit()

    packet, _handles = _context(
        conn, "matching orchard verified detail", project="unrelated", limit=50
    )

    assert packet["items"]
    assert packet["coverage"]["trimmed_for_packet_limit"] > 0
    assert packet["coverage"]["scope_fallback"]["mode"] == "cross_scope_auto"
    assert packet["coverage"]["serialized_bytes"] == len(
        json.dumps(packet, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    assert packet["coverage"]["serialized_bytes"] <= packet["coverage"]["hard_packet_limit_bytes"]
    conn.close()
