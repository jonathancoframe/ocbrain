from __future__ import annotations

import importlib.util
from pathlib import Path

from ocbrain.core_v1 import init_core_v1, record_core_v1_evidence
from ocbrain.db import connect
from ocbrain.scope import ScopeTag


def _curator_module():
    path = Path(__file__).parents[1] / "scripts" / "kimi-wiki-curator.py"
    spec = importlib.util.spec_from_file_location("kimi_wiki_curator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selector_enforces_visibility_egress_and_kind_boundaries(tmp_path: Path) -> None:
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)

    cases = (
        ("public hosted", "audit_finding", "public", "hosted_ok"),
        ("internal local", "audit_finding", "internal", "local_only"),
        ("confidential hosted", "audit_finding", "confidential", "hosted_ok"),
        ("internal prohibited", "audit_finding", "internal", "prohibited"),
        ("raw transcript", "codex_history_file", "public", "hosted_ok"),
    )
    ids: dict[str, str] = {}
    for body, kind, visibility, egress_policy in cases:
        evidence_id, _ = record_core_v1_evidence(
            conn,
            body=body,
            kind=kind,
            scope=ScopeTag(
                "project",
                "project:test",
                visibility=visibility,
                egress_policy=egress_policy,
            ),
            writer="test",
        )
        ids[body] = evidence_id
    conn.commit()

    curator = _curator_module()
    default_ids = {
        row["evidence_id"] for row in curator.select_evidence(conn, limit=20)
    }
    acknowledged_ids = {
        row["evidence_id"]
        for row in curator.select_evidence(
            conn, limit=20, allow_hosted_egress=True
        )
    }

    assert default_ids == {ids["public hosted"]}
    assert acknowledged_ids == {ids["public hosted"], ids["internal local"]}
    assert ids["confidential hosted"] not in acknowledged_ids
    assert ids["internal prohibited"] not in acknowledged_ids
    assert ids["raw transcript"] not in acknowledged_ids
    conn.close()
