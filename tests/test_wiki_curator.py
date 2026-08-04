from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

import ocbrain.curator
from ocbrain.core_v1 import append_core_event, init_core_v1, record_core_v1_evidence
from ocbrain.curator import select_evidence
from ocbrain.db import connect
from ocbrain.mcp_v1 import decide_proposal_v1
from ocbrain.scope import ScopeTag
from ocbrain.wiki import current_wiki_beliefs


def _curator_module():
    path = Path(__file__).parents[1] / "scripts" / "wiki-curator.py"
    spec = importlib.util.spec_from_file_location("wiki_curator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("provider", "model", "expected_field", "unsupported_field"),
    [
        ("openai", "gpt-5-mini", "max_completion_tokens", "max_tokens"),
        ("moonshot", "moonshot-v1-32k", "max_tokens", "max_completion_tokens"),
    ],
)
def test_openai_compatible_provider_uses_supported_token_budget_field(
    monkeypatch, provider, model, expected_field, unsupported_field
):
    captured: dict[str, bytes] = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["request"] = request.data
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({"beliefs": []})},
                }
            ]
        }
        return io.BytesIO(json.dumps(response).encode())

    monkeypatch.setattr(ocbrain.curator.urllib.request, "urlopen", fake_urlopen)
    result = ocbrain.curator.request_claims(
        provider=provider,
        api_key="test-key-never-sent",
        base_url="https://provider.invalid/v1",
        model=model,
        evidence=[],
        existing=[],
        max_beliefs=1,
        max_tokens=1_234,
    )

    assert result == {"beliefs": []}
    payload = json.loads(captured["request"])
    assert payload[expected_field] == 1_234
    assert unsupported_field not in payload


def _seed_wiki_belief(
    conn,
    *,
    belief_id: str,
    body: str,
    project: str = "test",
    visibility: str = "internal",
    egress_policy: str = "hosted_ok",
) -> None:
    proposal_id = append_core_event(
        conn,
        "compilation_proposed",
        {
            "belief_id": belief_id,
            "belief_type": "wiki_fact",
            "body": body,
            "evidence_ids": [],
            "scope": ScopeTag(
                "project",
                f"project:{project}",
                visibility=visibility,
                egress_policy=egress_policy,
                provenance="test",
            ).to_dict(),
            "confidence": 0.9,
            "attributes": {
                "key": belief_id.removeprefix("belief:"),
                "title": body,
                "category": "system",
            },
        },
        writer="test",
    )
    decide_proposal_v1(
        conn,
        proposal_event_id=proposal_id,
        decision="approve",
        actor="test",
        edited_body=None,
        reason="test seed",
    )


def test_selector_enforces_visibility_egress_and_kind_boundaries(tmp_path: Path) -> None:
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)

    cases = (
        ("public hosted", "audit_finding", "public", "hosted_ok", "test"),
        (
            "internal approval required",
            "audit_finding",
            "internal",
            "approval_required",
            "test",
        ),
        ("internal local", "audit_finding", "internal", "local_only", "test"),
        (
            "confidential approval required",
            "audit_finding",
            "confidential",
            "approval_required",
            "test",
        ),
        (
            "confidential hosted",
            "audit_finding",
            "confidential",
            "hosted_ok",
            "test",
        ),
        ("internal prohibited", "audit_finding", "internal", "prohibited", "test"),
        ("raw transcript", "codex_history_file", "public", "hosted_ok", "test"),
        ("other project", "audit_finding", "public", "hosted_ok", "other"),
    )
    ids: dict[str, str] = {}
    for body, kind, visibility, egress_policy, project in cases:
        evidence_id, _ = record_core_v1_evidence(
            conn,
            body=body,
            kind=kind,
            scope=ScopeTag(
                "project",
                f"project:{project}",
                visibility=visibility,
                egress_policy=egress_policy,
            ),
            writer="test",
        )
        ids[body] = evidence_id
    conn.commit()

    default_ids = {
        row["evidence_id"] for row in select_evidence(conn, limit=20, project="test")
    }
    acknowledged_ids = {
        row["evidence_id"]
        for row in select_evidence(
            conn, limit=20, allow_hosted_egress=True, project="test"
        )
    }

    assert default_ids == {ids["public hosted"]}
    assert acknowledged_ids == {
        ids["public hosted"],
        ids["internal approval required"],
    }
    assert ids["internal local"] not in acknowledged_ids
    assert ids["confidential approval required"] not in acknowledged_ids
    assert ids["confidential hosted"] not in acknowledged_ids
    assert ids["internal prohibited"] not in acknowledged_ids
    assert ids["raw transcript"] not in acknowledged_ids
    assert ids["other project"] not in acknowledged_ids
    with pytest.raises(ValueError, match="project is required"):
        select_evidence(conn, limit=20)
    conn.close()


def test_hosted_existing_wiki_gate_never_admits_local_or_confidential(
    tmp_path: Path,
) -> None:
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    cases = (
        ("belief:hosted", "hosted belief", "internal", "hosted_ok", "test"),
        (
            "belief:approval",
            "approval belief",
            "internal",
            "approval_required",
            "test",
        ),
        ("belief:local", "local belief", "internal", "local_only", "test"),
        ("belief:prohibited", "prohibited belief", "public", "prohibited", "test"),
        (
            "belief:confidential",
            "confidential belief",
            "confidential",
            "hosted_ok",
            "test",
        ),
        ("belief:secret", "secret belief", "secret", "approval_required", "test"),
        ("belief:other", "other project belief", "public", "hosted_ok", "other"),
    )
    for belief_id, body, visibility, egress_policy, project in cases:
        _seed_wiki_belief(
            conn,
            belief_id=belief_id,
            body=body,
            project=project,
            visibility=visibility,
            egress_policy=egress_policy,
        )

    default_ids = {
        row["belief_id"]
        for row in current_wiki_beliefs(
            conn,
            project="test",
            hosted_egress=True,
        )
    }
    acknowledged_ids = {
        row["belief_id"]
        for row in current_wiki_beliefs(
            conn,
            project="test",
            hosted_egress=True,
            allow_approval_required=True,
        )
    }

    assert default_ids == {"belief:hosted"}
    assert acknowledged_ids == {"belief:hosted", "belief:approval"}
    assert "belief:local" not in acknowledged_ids
    assert "belief:prohibited" not in acknowledged_ids
    assert "belief:confidential" not in acknowledged_ids
    assert "belief:secret" not in acknowledged_ids
    assert "belief:other" not in acknowledged_ids
    with pytest.raises(ValueError, match="requires hosted_egress"):
        current_wiki_beliefs(conn, allow_approval_required=True)
    with pytest.raises(ValueError, match="project is required"):
        current_wiki_beliefs(conn, hosted_egress=True)
    conn.close()


def test_hosted_prompt_excludes_local_and_confidential_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "core.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    evidence_cases = (
        ("HOSTED EVIDENCE SAFE QUOTE", "internal", "hosted_ok"),
        ("APPROVAL EVIDENCE SAFE QUOTE", "internal", "approval_required"),
        ("LOCAL EVIDENCE MUST NEVER EGRESS", "internal", "local_only"),
        ("CONFIDENTIAL EVIDENCE MUST NEVER EGRESS", "confidential", "hosted_ok"),
    )
    evidence_ids: dict[str, str] = {}
    for body, visibility, egress_policy in evidence_cases:
        evidence_id, _ = record_core_v1_evidence(
            conn,
            body=body,
            kind="audit_finding",
            scope=ScopeTag(
                "project",
                "project:test",
                visibility=visibility,
                egress_policy=egress_policy,
            ),
            writer="test",
        )
        evidence_ids[body] = evidence_id
    belief_cases = (
        ("belief:prompt-hosted", "HOSTED BELIEF SAFE", "internal", "hosted_ok"),
        (
            "belief:prompt-approval",
            "APPROVAL BELIEF SAFE",
            "internal",
            "approval_required",
        ),
        (
            "belief:prompt-local",
            "LOCAL BELIEF MUST NEVER EGRESS",
            "internal",
            "local_only",
        ),
        (
            "belief:prompt-confidential",
            "CONFIDENTIAL BELIEF MUST NEVER EGRESS",
            "confidential",
            "hosted_ok",
        ),
    )
    for belief_id, body, visibility, egress_policy in belief_cases:
        _seed_wiki_belief(
            conn,
            belief_id=belief_id,
            body=body,
            visibility=visibility,
            egress_policy=egress_policy,
        )
    conn.commit()
    conn.close()

    captured: dict[str, bytes] = {}

    def fake_urlopen(request, timeout):
        del timeout
        captured["request"] = request.data
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "beliefs": [
                                    {
                                        "key": "hosted-evidence-safe",
                                        "title": "Hosted evidence is safe",
                                        "body": (
                                            "The hosted evidence passed every outbound "
                                            "scope and privacy gate."
                                        ),
                                        "category": "system",
                                        "lifecycle": "current",
                                        "confidence": 0.9,
                                        "supports": [
                                            {
                                                "evidence_id": evidence_ids[
                                                    "HOSTED EVIDENCE SAFE QUOTE"
                                                ],
                                                "quote": "HOSTED EVIDENCE SAFE QUOTE",
                                            }
                                        ],
                                    }
                                ]
                            }
                        )
                    },
                }
            ]
        }
        return io.BytesIO(json.dumps(response).encode())

    curator = _curator_module()
    monkeypatch.setattr(ocbrain.curator.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("KIMI_API_KEY", "test-key-never-sent")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "wiki-curator.py",
            "--provider",
            "moonshot",
            "--db",
            str(db_path),
            "--wiki-dir",
            str(tmp_path / "wiki"),
            "--project",
            "test",
            "--allow-hosted-egress",
            "--apply",
            "--force",
        ],
    )

    assert curator.main() == 0
    payload = json.loads(captured["request"])
    prompt = payload["messages"][1]["content"]
    assert "HOSTED EVIDENCE SAFE QUOTE" in prompt
    assert "APPROVAL EVIDENCE SAFE QUOTE" in prompt
    assert "HOSTED BELIEF SAFE" in prompt
    assert "APPROVAL BELIEF SAFE" in prompt
    assert "LOCAL EVIDENCE MUST NEVER EGRESS" not in prompt
    assert "CONFIDENTIAL EVIDENCE MUST NEVER EGRESS" not in prompt
    assert "LOCAL BELIEF MUST NEVER EGRESS" not in prompt
    assert "CONFIDENTIAL BELIEF MUST NEVER EGRESS" not in prompt


def test_selection_policy_floor_cannot_be_configured_away() -> None:
    """An operator may widen what their curator reads, but not past the floor."""
    from ocbrain.curator import resolve_selection_policy

    egress, visibility = resolve_selection_policy(
        egress_policies=["hosted_ok", "local_only", "prohibited"],
        visibilities=["internal", "confidential", "secret"],
    )
    assert "prohibited" not in egress
    assert "local_only" in egress
    assert "secret" not in visibility
    assert "confidential" in visibility

    # Shipped default stays narrow.
    assert resolve_selection_policy() == (("hosted_ok",), ("internal", "public"))
    # A policy that admits nothing is an error, not a silent empty selection.
    with pytest.raises(ValueError, match="admits nothing"):
        resolve_selection_policy(egress_policies=["prohibited"])
    with pytest.raises(ValueError, match="admits nothing"):
        resolve_selection_policy(visibilities=["secret"])


def test_widened_policy_admits_local_only_but_never_prohibited(tmp_path: Path) -> None:
    """The case that matters: a brain whose evidence is all local_only."""
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    cases = (
        ("LOCAL ONLY INTERNAL BODY", "internal", "local_only"),
        ("HOSTED OK INTERNAL BODY", "internal", "hosted_ok"),
        ("PROHIBITED CONFIDENTIAL BODY", "confidential", "prohibited"),
        ("SECRET LOCAL BODY", "secret", "local_only"),
    )
    for body, visibility, egress_policy in cases:
        record_core_v1_evidence(
            conn,
            body=body,
            kind="audit_finding",
            scope=ScopeTag(
                "project", "project:test", visibility=visibility, egress_policy=egress_policy
            ),
            writer="test",
        )
    conn.commit()

    default_bodies = {
        row["body"] for row in select_evidence(conn, limit=20, project="test")
    }
    assert default_bodies == {"HOSTED OK INTERNAL BODY"}

    widened_bodies = {
        row["body"]
        for row in select_evidence(
            conn,
            limit=20,
            project="test",
            egress_policies=["hosted_ok", "local_only"],
            visibilities=["public", "internal", "confidential"],
        )
    }
    assert widened_bodies == {"HOSTED OK INTERNAL BODY", "LOCAL ONLY INTERNAL BODY"}
    assert "PROHIBITED CONFIDENTIAL BODY" not in widened_bodies
    assert "SECRET LOCAL BODY" not in widened_bodies


def test_curation_egress_is_audited_before_the_send(tmp_path: Path) -> None:
    """Widening the allow-list is only defensible if every send is accountable."""
    from ocbrain.curator import record_curation_egress

    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    evidence_id, _ = record_core_v1_evidence(
        conn,
        body="AUDITED EVIDENCE BODY",
        kind="audit_finding",
        scope=ScopeTag(
            "project", "project:test", visibility="internal", egress_policy="local_only"
        ),
        writer="test",
    )
    conn.commit()
    rows = select_evidence(
        conn, limit=20, project="test", egress_policies=["local_only"]
    )
    assert len(rows) == 1

    audit_id = record_curation_egress(
        conn,
        evidence=rows,
        provider="anthropic",
        model="claude-sonnet-5",
        project="test",
        egress_policies=("local_only",),
    )
    audit = conn.execute(
        "SELECT target, context_json, included_json, payload_hash FROM egress_audits WHERE id=?",
        (audit_id,),
    ).fetchone()
    assert audit["target"] == "anthropic:claude-sonnet-5"
    assert evidence_id in audit["included_json"]
    assert "wiki_curation" in audit["context_json"]
    assert "local_only" in audit["context_json"]
    # The body itself is not copied into the audit, only its identity and size.
    assert "AUDITED EVIDENCE BODY" not in audit["included_json"]
    assert audit["payload_hash"]


def test_curator_updates_a_restated_fact_instead_of_minting_a_second(tmp_path: Path) -> None:
    """A reworded claim must update the belief that already states the fact.

    belief_id derives from the topic key the model chose, so a later run that
    rewords the same fact under a new key used to create a second served belief.
    Exact-body dedup never catches it, and every run added another phrasing.
    """
    from ocbrain.curator import apply_claims

    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)

    first = [
        {
            "key": "hermes-runtime-config",
            "title": "Hermes runtime",
            "body": "Hermes runs as the launchd service ai.hermes.gateway with auto-start "
            "and restart, delivering to Telegram.",
            "category": "system",
            "lifecycle": "durable",
            "confidence": 0.9,
            "evidence_ids": [],
        }
    ]
    applied = apply_claims(conn, first, model="test", project="test")
    assert len(applied["applied"]) == 1
    original_id = applied["applied"][0]

    # Same fact, different key and wording — the shape a later run produces.
    second = [
        {
            "key": "hermes-runtime-service",
            "title": "Hermes runtime",
            "body": "Hermes runs as launchd service ai.hermes.gateway with auto-start and "
            "auto-restart, delivering to Telegram.",
            "category": "system",
            "lifecycle": "durable",
            "confidence": 0.9,
            "evidence_ids": [],
        }
    ]
    reapplied = apply_claims(conn, second, model="test", project="test")

    # It updated the existing belief rather than adding a second one.
    assert reapplied["applied"] == [original_id]
    serving = conn.execute(
        "SELECT belief_id, body FROM current_beliefs WHERE serve=1 AND status='current'"
    ).fetchall()
    assert len(serving) == 1
    assert str(serving[0]["belief_id"]) == original_id
    assert "auto-restart" in str(serving[0]["body"])
    conn.close()


def test_curator_still_adds_a_genuinely_different_fact(tmp_path: Path) -> None:
    """Restatement collapsing must not swallow distinct knowledge."""
    from ocbrain.curator import apply_claims

    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)

    def claim(key: str, body: str) -> dict:
        return {
            "key": key,
            "title": key,
            "body": body,
            "category": "system",
            "lifecycle": "durable",
            "confidence": 0.9,
            "evidence_ids": [],
        }

    apply_claims(
        conn,
        [claim("hermes-runtime", "Hermes runs as the launchd service ai.hermes.gateway.")],
        model="test",
        project="test",
    )
    apply_claims(
        conn,
        [
            claim(
                "clickhouse-access",
                "Production ClickHouse access is SELECT-only and the live host rotates.",
            )
        ],
        model="test",
        project="test",
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM current_beliefs WHERE serve=1 AND status='current'"
        ).fetchone()[0]
        == 2
    )
    conn.close()
