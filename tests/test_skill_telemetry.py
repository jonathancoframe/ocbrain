"""Skill-usage telemetry envelope convention (docs/SKILL_TELEMETRY.md)."""

from __future__ import annotations

import json

import pytest

from ocbrain.cli import main
from ocbrain.core_v1 import init_core_v1, set_automatic_activation
from ocbrain.db import connect, init_db
from ocbrain.events import (
    SKILL_TELEMETRY_KINDS,
    SKILL_TELEMETRY_SCHEMA_VERSION,
    validate_skill_telemetry,
)
from ocbrain.mcp import call_tool
from ocbrain.mcp_v1 import ingest_v1
from ocbrain.scope import ScopeContext


def _envelope(**overrides):
    base = {
        "schema_version": SKILL_TELEMETRY_SCHEMA_VERSION,
        "kind": "skill_load",
        "skill_id": "ocbrain-ops",
        "source_commit": "a5b35db",
    }
    base.update(overrides)
    return base


def test_all_six_kinds_are_registered():
    assert SKILL_TELEMETRY_KINDS == frozenset(
        {
            "skill_build",
            "skill_install",
            "skill_load",
            "skill_outcome",
            "skill_correction_candidate",
            "skill_retirement",
        }
    )


def test_valid_envelope_passes_and_accepts_json_text():
    parsed = validate_skill_telemetry(_envelope())
    assert parsed["skill_id"] == "ocbrain-ops"
    as_text = validate_skill_telemetry(
        json.dumps(_envelope(kind="skill_outcome", outcome="success"))
    )
    assert as_text["kind"] == "skill_outcome"


@pytest.mark.parametrize("locator", ["source_commit", "tree_sha256", "skill_uri"])
def test_each_locator_alone_satisfies_the_locator_rule(locator):
    envelope = {
        "schema_version": SKILL_TELEMETRY_SCHEMA_VERSION,
        "kind": "skill_build",
        "skill_id": "ocbrain-ops",
        locator: {
            "source_commit": "a5b35db",
            "tree_sha256": "ab" * 32,
            "skill_uri": "skill://ocbrain-ops/a5b35db",
        }[locator],
    }
    assert validate_skill_telemetry(envelope)["skill_id"] == "ocbrain-ops"


def test_missing_locator_is_rejected():
    envelope = _envelope()
    del envelope["source_commit"]
    with pytest.raises(ValueError, match="locator"):
        validate_skill_telemetry(envelope)


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown skill telemetry kind"):
        validate_skill_telemetry(_envelope(kind="skill_debug_dump"))


def test_wrong_schema_version_is_rejected():
    with pytest.raises(ValueError, match="schema_version"):
        validate_skill_telemetry(_envelope(schema_version="ocbrain.skill_telemetry.v0"))


@pytest.mark.parametrize(
    "field", ["skill_body", "transcript", "messages", "prompt", "tool_output"]
)
def test_content_fields_are_forbidden(field):
    with pytest.raises(ValueError, match="metadata-only"):
        validate_skill_telemetry(_envelope(**{field: "secret"}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("skill_id", {"prompt": "nested secret"}),
        ("skill_uri", ["skill://one", "skill://two"]),
        ("tree_sha256", "not-a-sha"),
        ("artifact_sha256", "abc123"),
        ("source_commit", "branch-name"),
    ],
)
def test_metadata_fields_reject_nested_content_and_malformed_hashes(field, value):
    with pytest.raises(ValueError, match="must be"):
        validate_skill_telemetry(_envelope(**{field: value}))


def test_telemetry_ingest_validates_and_never_auto_compiles(tmp_path):
    conn = connect(tmp_path / "telemetry.sqlite")
    init_core_v1(conn)
    set_automatic_activation(conn, True)
    result = ingest_v1(
        conn,
        body=json.dumps(_envelope()),
        kind="skill_load",
        context=ScopeContext(project="ocbrain", runtime="codex"),
        writer="test",
        session_id="telemetry-test",
        artifact_ref=None,
    )
    conn.commit()

    assert result["kind"] == "evidence_recorded"
    assert "auto_compiled_belief_id" not in result
    assert conn.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM current_beliefs").fetchone()[0] == 0


def test_telemetry_ingest_rejects_body_kind_mismatch(tmp_path):
    conn = connect(tmp_path / "telemetry-mismatch.sqlite")
    init_core_v1(conn)
    with pytest.raises(ValueError, match="body kind must match"):
        ingest_v1(
            conn,
            body=json.dumps(_envelope(kind="skill_outcome", outcome="success")),
            kind="skill_load",
            context=ScopeContext(project="ocbrain"),
            writer="test",
            session_id=None,
            artifact_ref=None,
        )


def test_legacy_ingest_enforces_the_same_telemetry_boundary(tmp_path):
    conn = connect(tmp_path / "telemetry-legacy.sqlite")
    init_db(conn)
    call_tool(
        conn,
        {
            "name": "brain.ingest",
            "arguments": {
                "body": json.dumps(_envelope(source_commit="A5B35DB")),
                "kind": "skill_load",
                "context": {"project": "ocbrain"},
            },
        },
    )
    outer = json.loads(
        conn.execute(
            "SELECT body_json FROM brain_events WHERE kind='evidence_recorded'"
        ).fetchone()[0]
    )
    assert json.loads(outer["body"])["source_commit"] == "a5b35db"

    with pytest.raises(ValueError, match="body kind must match"):
        call_tool(
            conn,
            {
                "name": "brain.ingest",
                "arguments": {
                    "body": json.dumps(
                        _envelope(kind="skill_outcome", outcome="success")
                    ),
                    "kind": "skill_load",
                },
            },
        )


def test_cli_event_ingest_enforces_telemetry_for_v1_global_and_legacy(
    tmp_path,
    capsys,
):
    nested_body = json.dumps(_envelope(skill_id={"prompt": "nested secret"}))

    v1_db = tmp_path / "telemetry-cli-v1.sqlite"
    assert main(["--db", str(v1_db), "init"]) == 0
    capsys.readouterr()
    with pytest.raises(ValueError, match="skill_id must be a string"):
        main(
            [
                "--db",
                str(v1_db),
                "event-ingest",
                "--kind",
                "skill_load",
                "--body",
                nested_body,
                "--global-doctrine",
            ]
        )
    verify_v1 = connect(v1_db)
    assert verify_v1.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == 0
    verify_v1.close()
    assert (
        main(
            [
                "--db",
                str(v1_db),
                "event-ingest",
                "--kind",
                "skill_load",
                "--body",
                json.dumps(_envelope()),
                "--global-doctrine",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["scope"]["scope_id"] == "global:doctrine"
    verify_v1 = connect(v1_db)
    assert verify_v1.execute("SELECT COUNT(*) FROM evidence_objects").fetchone()[0] == 1
    assert verify_v1.execute("SELECT COUNT(*) FROM current_beliefs").fetchone()[0] == 0
    verify_v1.close()

    legacy_db = tmp_path / "telemetry-cli-legacy.sqlite"
    legacy = connect(legacy_db)
    init_db(legacy)
    legacy.close()
    with pytest.raises(ValueError, match="skill_id must be a string"):
        main(
            [
                "--db",
                str(legacy_db),
                "event-ingest",
                "--kind",
                "skill_load",
                "--body",
                nested_body,
            ]
        )
    verify_legacy = connect(legacy_db)
    assert verify_legacy.execute("SELECT COUNT(*) FROM brain_events").fetchone()[0] == 0
    verify_legacy.close()
