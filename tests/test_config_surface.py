from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocbrain import config as config_module
from ocbrain.cli import main as cli_main
from ocbrain.config import default_config_path, describe_config, load_config


def test_config_resolution_prefers_the_user_path_over_the_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    """Operator config must not live where a repo clean can delete it.

    The old default was the *relative* ``data/ocbrain.config.json``: resolution
    depended on the working directory, and a `git clean -xfd`, fresh clone, or
    worktree switch silently discarded settings. A brain that loses its curator
    policy that way keeps exiting 0 while promoting nothing.
    """
    monkeypatch.delenv("OCBRAIN_CONFIG", raising=False)
    user_path = tmp_path / "user" / "ocbrain.config.json"
    legacy_path = tmp_path / "checkout" / "data" / "ocbrain.config.json"
    user_path.parent.mkdir(parents=True)
    legacy_path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_module, "USER_CONFIG_PATH", user_path)
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", legacy_path)

    # Neither present: the user path is still what we report, so an operator is
    # told where to put one rather than where it used to go.
    assert default_config_path() == user_path

    # Legacy only: honored, so an existing install keeps working.
    legacy_path.write_text(json.dumps({"curator": {"max_beliefs": 7}}), encoding="utf-8")
    assert default_config_path() == legacy_path
    assert load_config().curator.max_beliefs == 7

    # Both present: the durable location wins.
    user_path.write_text(json.dumps({"curator": {"max_beliefs": 9}}), encoding="utf-8")
    assert default_config_path() == user_path
    assert load_config().curator.max_beliefs == 9

    # An explicit override still beats both.
    monkeypatch.setenv("OCBRAIN_CONFIG", str(legacy_path))
    assert default_config_path() == legacy_path


def test_describe_config_attributes_every_value_to_its_layer(tmp_path: Path, monkeypatch) -> None:
    """A layered config is only usable if you can see which layer won."""
    config_path = tmp_path / "ocbrain.config.json"
    config_path.write_text(
        json.dumps({"curator": {"egress_policies": ["hosted_ok", "local_only"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OCBRAIN_CONFIG", str(config_path))
    monkeypatch.setenv("OCBRAIN_CURATOR_MAX_BELIEFS", "5")

    report = describe_config()
    curator = report["sections"]["curator"]
    assert report["config_path"] == str(config_path)
    assert report["config_path_exists"] is True

    assert curator["egress_policies"]["source"] == "file"
    assert curator["egress_policies"]["value"] == ["hosted_ok", "local_only"]
    assert curator["egress_policies"]["default"] == ["hosted_ok"]

    assert curator["max_beliefs"]["source"] == "env"
    assert curator["max_beliefs"]["value"] == 5

    assert curator["current_ttl_days"]["source"] == "default"
    assert (
        curator["current_ttl_days"]["value"] == curator["current_ttl_days"]["default"]
    )


def test_describe_config_reports_defaults_when_no_file_exists(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OCBRAIN_CONFIG", str(tmp_path / "absent.json"))
    report = describe_config()
    assert report["config_path_exists"] is False
    assert all(
        entry["source"] == "default"
        for section in report["sections"].values()
        for entry in section.values()
    )


def test_cli_config_route_and_filters(tmp_path: Path, capsys, monkeypatch) -> None:
    db = tmp_path / "core.sqlite"
    assert cli_main(["--db", str(db), "init"]) == 0
    capsys.readouterr()

    config_path = tmp_path / "ocbrain.config.json"
    config_path.write_text(json.dumps({"curator": {"max_beliefs": 3}}), encoding="utf-8")
    monkeypatch.setenv("OCBRAIN_CONFIG", str(config_path))

    assert cli_main(["--db", str(db), "config"]) == 0
    full = json.loads(capsys.readouterr().out)
    assert full["action"] == "config"
    assert "curator" in full["sections"]
    assert "retrieval" in full["sections"]

    assert cli_main(["--db", str(db), "config", "--section", "curator"]) == 0
    scoped = json.loads(capsys.readouterr().out)
    assert set(scoped["sections"]) == {"curator"}

    assert cli_main(["--db", str(db), "config", "--changed-only"]) == 0
    changed = json.loads(capsys.readouterr().out)
    # Only the one field set in the file survives the filter.
    assert changed["sections"] == {
        "curator": {"max_beliefs": {"value": 3, "source": "file", "default": 24}}
    }

    with pytest.raises(SystemExit, match="unknown section"):
        cli_main(["--db", str(db), "config", "--section", "nonsense"])
