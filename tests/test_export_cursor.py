"""Tests for scripts/export-cursor-chats.py.

The exporter counted skipped workspaces without saying which or why, so three
workspaces yielding nothing looked identical to three workspaces that were
genuinely idle. These tests pin the attribution, and the timestamp handling for
the two spellings Cursor uses.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "scripts" / "export-cursor-chats.py"
    spec = importlib.util.spec_from_file_location("export_cursor_chats", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workspace(root: Path, name: str, items: dict[str, object] | None = None) -> Path:
    """Build one workspaceStorage/<name>/state.vscdb with the given ItemTable."""
    ws = root / name
    ws.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ws / "state.vscdb")
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    for key, value in (items or {}).items():
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            (key, value if isinstance(value, str) else json.dumps(value)),
        )
    conn.commit()
    conn.close()
    return ws


def _run(module, storage: Path, out: Path) -> dict:
    argv = ["--storage", str(storage), "--out", str(out)]
    buffer = io.StringIO()
    saved = sys.argv
    sys.argv = ["export-cursor-chats.py", *argv]
    try:
        with contextlib.redirect_stdout(buffer):
            assert module.main() == 0
    finally:
        sys.argv = saved
    return json.loads(buffer.getvalue())


def test_skip_reasons_are_reported(tmp_path):
    module = _module()
    storage = tmp_path / "workspaceStorage"

    # One real workspace, so the run is not trivially all-skips.
    _workspace(storage, "has-chat", {"aiService.prompts": [{"text": "how do I ship this"}]})
    # Skipped by name, the way Cursor's folder-less windows always have been.
    _workspace(storage, "empty-window", {"aiService.prompts": [{"text": "orphan"}]})
    # Present but idle: the arrays exist and are empty. This is what all three
    # of the real zero-record workspaces turned out to be.
    _workspace(storage, "idle-ws", {"aiService.prompts": [], "aiService.generations": []})
    # Unreadable file where a database should be.
    broken = storage / "broken-ws"
    broken.mkdir(parents=True)
    (broken / "state.vscdb").write_text("not a database at all")

    payload = _run(module, storage, tmp_path / "out")

    assert payload["exported"] == 1
    assert payload["skipped"] == 3
    assert payload["skipped_by_reason"] == {"empty-window": 1, "no_records": 1, "sqlite_error": 1}

    by_workspace = {entry["workspace"]: entry["reason"] for entry in payload["skipped_sample"]}
    assert by_workspace["empty-window"] == "empty-window"
    assert by_workspace["idle-ws"] == "no_records"
    assert by_workspace["broken-ws"].startswith("sqlite_error:")
    # The reason has to name the fault, not just its class.
    assert len(by_workspace["broken-ws"]) > len("sqlite_error:")


def test_skip_sample_is_capped_but_counts_are_not(tmp_path):
    module = _module()
    storage = tmp_path / "workspaceStorage"
    for index in range(module._SKIPPED_SAMPLE + 5):
        _workspace(storage, f"idle-{index:03d}", {"aiService.prompts": []})

    payload = _run(module, storage, tmp_path / "out")

    assert payload["skipped"] == module._SKIPPED_SAMPLE + 5
    assert payload["skipped_by_reason"] == {"no_records": module._SKIPPED_SAMPLE + 5}
    assert len(payload["skipped_sample"]) == module._SKIPPED_SAMPLE


def test_composer_bubbles_accept_iso_string_timestamps(tmp_path):
    """Composer bubbles date themselves with an ISO string, not epoch millis.

    The numeric path divides by 1000, so a string used to raise TypeError —
    which the caller did not catch — and would have taken down the whole sweep
    the moment a workspace carried composer rows.
    """
    module = _module()
    ws = _workspace(tmp_path / "workspaceStorage", "composer-ws")
    conn = sqlite3.connect(ws / "state.vscdb")
    bubbles = {
        "bubbleId:c1:b1": {"type": 1, "text": "ask", "createdAt": "2026-07-13T19:52:33.774Z"},
        "bubbleId:c1:b2": {"type": 2, "text": "answer", "unixMs": 1_768_000_000_000},
    }
    for key, bubble in bubbles.items():
        conn.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (key, json.dumps(bubble))
        )
    conn.commit()

    records = module.extract_records(conn)
    conn.close()

    by_content = {record["content"]: record for record in records}
    assert by_content["ask"]["role"] == "user"
    assert by_content["ask"]["timestamp"] == "2026-07-13T19:52:33.774000+00:00"
    assert by_content["answer"]["role"] == "assistant"
    assert by_content["answer"]["timestamp"].startswith("2026-")


def test_parse_timestamp_handles_both_spellings_and_junk(tmp_path):
    module = _module()
    assert module.parse_timestamp(None) is None
    assert module.parse_timestamp("") is None
    assert module.parse_timestamp("not a date") is None
    assert module.parse_timestamp({"nested": "object"}) is None
    assert module.parse_timestamp(1_768_000_000_000).startswith("2026-")
    assert module.parse_timestamp("2026-07-13T19:52:33.774Z").endswith("+00:00")


def test_workspace_with_records_still_exports_and_regates(tmp_path):
    """The skip rework must not disturb the content-compare gate."""
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "has-chat", {"aiService.prompts": [{"text": "first question"}]})
    out = tmp_path / "out"

    first = _run(module, storage, out)
    second = _run(module, storage, out)

    assert first["exported"] == 1 and first["unchanged"] == 0
    assert second["exported"] == 0 and second["unchanged"] == 1
    body = (out / "cursor-has-chat.jsonl").read_text().splitlines()
    assert json.loads(body[0])["_meta"]["workspace_id"] == "has-chat"
    assert json.loads(body[1])["content"] == "first question"


def test_per_record_truncation_keeps_head_and_tail(tmp_path):
    module = _module()
    text = "HEAD" + ("m" * 5_000) + "TAIL"

    capped, was_cut = module.truncate_middle(text, 1_000)

    assert was_cut
    assert len(capped.encode("utf-8")) <= 1_000
    assert capped.startswith("HEAD")
    assert capped.endswith("TAIL")
    assert "omitted from middle" in capped
    # Under the cap, nothing is touched and nothing claims to have been.
    assert module.truncate_middle("short", 1_000) == ("short", False)


def test_file_budget_drops_the_oldest_records_not_the_newest(tmp_path):
    """The budget used to be spent front to back, dropping the newest chat.

    That is exactly backwards for a memory system: the newest turn is the one
    no earlier export already carries.
    """
    module = _module()
    records = [
        {"role": "user", "timestamp": f"2026-08-{day:02d}T00:00:00+00:00", "content": f"day {day}"}
        for day in range(1, 21)
    ]

    text = module.render_jsonl(records, {"workspace_id": "ws"}, 700, lambda value: value)

    lines = [json.loads(line) for line in text.splitlines()]
    kept = [line["content"] for line in lines if "content" in line]
    note = next(line["_truncated"] for line in lines if "_truncated" in line)

    assert kept, "the budget dropped everything"
    assert len(kept) < len(records), "the fixture did not exercise the budget"
    # The survivors are the newest, contiguous, and still in ascending order.
    assert kept == [f"day {day}" for day in range(21 - len(kept), 21)]
    assert kept[-1] == "day 20"
    assert note == {"dropped_oldest": len(records) - len(kept), "kept_newest": len(kept)}
    assert len(text) <= 700
