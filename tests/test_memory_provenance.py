from __future__ import annotations

from pathlib import Path

import pytest

from ocbrain.cli import import_memory_file, import_memory_file_v1
from ocbrain.core_v1 import init_core_v1
from ocbrain.db import connect, init_db

MEMORY_RUNTIME_CASES = [
    (Path(".codex/AGENTS.md"), "codex"),
    (Path(".claude/CLAUDE.md"), "claude"),
    (Path(".hermes/MEMORY.md"), "hermes"),
    (Path(".openclaw/workspace/MEMORY.md"), "openclaw"),
]


def _memory_file(tmp_path: Path, relative_path: Path) -> Path:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Memory\n\nRuntime provenance sentinel.\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(("relative_path", "expected_runtime"), MEMORY_RUNTIME_CASES)
def test_v1_memory_import_infers_runtime_from_path(
    tmp_path: Path, relative_path: Path, expected_runtime: str
) -> None:
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    path = _memory_file(tmp_path, relative_path)

    result = import_memory_file_v1(
        conn,
        path,
        project="ocbrain",
        privacy_scope="workspace",
        max_bytes=100_000,
        activate=False,
    )

    assert result is not None
    assert result["runtime"] == expected_runtime
    evidence = conn.execute(
        "SELECT source_runtime FROM evidence_objects WHERE evidence_id=?",
        (result["evidence_id"],),
    ).fetchone()
    assert evidence is not None
    assert evidence["source_runtime"] == f"ocbrain-import:{expected_runtime}"
    conn.close()


@pytest.mark.parametrize(("relative_path", "expected_runtime"), MEMORY_RUNTIME_CASES)
def test_legacy_memory_import_infers_runtime_from_path(
    tmp_path: Path, relative_path: Path, expected_runtime: str
) -> None:
    conn = connect(tmp_path / "legacy.sqlite")
    init_db(conn)
    path = _memory_file(tmp_path, relative_path)

    result = import_memory_file(
        conn,
        path,
        project="ocbrain",
        privacy_scope="workspace",
        max_bytes=100_000,
    )

    assert result is not None
    evidence = conn.execute(
        "SELECT source_runtime FROM evidence WHERE id=?", (result["evidence_id"],)
    ).fetchone()
    assert evidence is not None
    assert evidence["source_runtime"] == expected_runtime
    conn.close()
