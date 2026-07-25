from __future__ import annotations

from pathlib import Path

from ocbrain.core_v1 import init_core_v1
from ocbrain.db import connect
from ocbrain.runtime_call import invoke


def test_one_shot_runtime_fallback_records_closeout(tmp_path: Path) -> None:
    db_path = tmp_path / "ocbrain.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    conn.close()

    result = invoke(
        db_path,
        "brain.closeout",
        {
            "summary": "Verified fallback closeout.",
            "status": "completed",
            "task_ref": "fallback-test",
            "context": {"project": "test", "task": "fallback-test"},
            "decision_impact": "none",
            "retrieval_use_ids": [],
            "artifact_refs": [],
            "verifier_refs": [
                {
                    "uri": "pytest://runtime-fallback",
                    "kind": "pytest",
                    "status": "passed",
                    "detail": "One-shot runtime call completed.",
                }
            ],
        },
    )

    assert result["status"] == "completed"
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM task_closeouts").fetchone()[0] == 1


def test_one_shot_runtime_fallback_rejects_admin_tool(tmp_path: Path) -> None:
    db_path = tmp_path / "ocbrain.sqlite"
    conn = connect(db_path)
    init_core_v1(conn)
    conn.close()

    try:
        invoke(db_path, "brain.correct", {})
    except PermissionError as exc:
        assert "runtime tools only" in str(exc)
    else:
        raise AssertionError("admin tool unexpectedly allowed")
