"""Skill-usage telemetry envelope convention (docs/SKILL_TELEMETRY.md)."""

from __future__ import annotations

import json

import pytest

from ocbrain.events import (
    SKILL_TELEMETRY_KINDS,
    SKILL_TELEMETRY_SCHEMA_VERSION,
    validate_skill_telemetry,
)


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
        locator: "x" if locator != "tree_sha256" else "ab" * 32,
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
