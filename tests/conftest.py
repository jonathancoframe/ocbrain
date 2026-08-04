from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_operator_config(tmp_path_factory, monkeypatch) -> None:
    """Point config resolution at a path that does not exist, for every test.

    ``load_config`` falls back to the *relative* path ``data/ocbrain.config.json``,
    so a suite run from a checkout that has one silently inherits whatever the
    operator configured there. That made behavior depend on the working directory
    and on a gitignored file: a curator egress-boundary test passed in CI (no such
    file) and failed on a real machine (file present, policies widened) — the worst
    possible split, since CI can never reproduce it.

    Tests assert shipped defaults unless they set config explicitly, so isolate by
    default and let an individual test opt in by overriding OCBRAIN_CONFIG itself.
    """
    absent = Path(tmp_path_factory.mktemp("ocbrain-config")) / "absent.json"
    monkeypatch.setenv("OCBRAIN_CONFIG", str(absent))
