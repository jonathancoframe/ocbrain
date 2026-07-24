from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ocbrain.core_v1 import init_core_v1
from ocbrain.db import connect
from ocbrain.seal_truth import compile_sealed_release


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sealed_fixture(tmp_path: Path, *, release_id: str = "a" * 64) -> Path:
    release = tmp_path / release_id
    release.mkdir()
    closeout = {
        "schema_version": "ocbrain.closeout.v1",
        "status": "completed",
        "summary": (
            "Generation 11 completed across 110 experiment scopes with zero "
            "execution failures and 555 hash-verified artifacts."
        ),
        "verification_status": "verified",
        "verifier_refs": [
            {"uri": "pytest://generation-11", "status": "passed", "kind": "pytest"}
        ],
        "context": {
            "project": "coframe",
            "task": "generation-11",
            "runtime": "codex",
        },
        "decision": {
            "impact": "informed",
            "note": (
                "Performance findings remain exploratory because the same "
                "held-out set was consulted across generations."
            ),
        },
        "closed_at": "2026-07-23T10:00:00+00:00",
        "supersedes": [],
    }
    closeout_path = release / "closeout.json"
    closeout_path.write_text(json.dumps(closeout), encoding="utf-8")
    seal = {
        "schema_version": "agent-control.release.v1",
        "state": "sealed",
        "task_id": "generation-11",
        "release_id": release_id,
        "created_at": "2026-07-23T10:00:01+00:00",
        "artifacts": [
            {
                "role": "closeout",
                "filename": "closeout.json",
                "sha256": _sha(closeout_path),
                "bytes": closeout_path.stat().st_size,
            }
        ],
    }
    seal_path = release / "SEAL.json"
    seal_path.write_text(json.dumps(seal), encoding="utf-8")
    return seal_path


def test_verified_seal_compiles_one_human_readable_current_fact(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ocbrain.sqlite")
    init_core_v1(conn)
    seal = _sealed_fixture(tmp_path)
    wiki = tmp_path / "wiki"

    result = compile_sealed_release(conn, seal, wiki_dir=wiki)
    assert result["status"] == "compiled"
    belief = conn.execute(
        "SELECT * FROM current_beliefs WHERE belief_id=?", (result["belief_id"],)
    ).fetchone()
    assert belief["status"] == "current"
    assert belief["serve"] == 1
    assert belief["belief_type"] == "wiki_fact"
    assert "Generation 11 completed" in belief["body"]
    attributes = json.loads(belief["attributes_json"])
    assert attributes["verification_status"] == "verified"
    assert "held-out set" in attributes["uncertainty"]
    assert (wiki / "index.md").is_file()
    assert "Generation 11 completed" in (wiki / "index.md").read_text()

    repeated = compile_sealed_release(conn, seal, wiki_dir=wiki)
    assert repeated["status"] == "unchanged"
    assert conn.execute(
        "SELECT COUNT(*) FROM current_beliefs WHERE belief_id=?", (result["belief_id"],)
    ).fetchone()[0] == 1


def test_unverified_or_tampered_seal_is_rejected(tmp_path: Path) -> None:
    conn = connect(tmp_path / "ocbrain.sqlite")
    init_core_v1(conn)
    seal = _sealed_fixture(tmp_path)
    closeout = seal.parent / "closeout.json"
    closeout.write_text('{"status":"completed","summary":"tampered"}', encoding="utf-8")

    try:
        compile_sealed_release(conn, seal, wiki_dir=tmp_path / "wiki")
    except ValueError as exc:
        assert "hash mismatch" in str(exc)
    else:
        raise AssertionError("tampered sealed artifact was accepted")
