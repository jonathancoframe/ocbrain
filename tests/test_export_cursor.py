"""Tests for scripts/export-cursor-chats.py.

Two blind spots are pinned here. The exporter counted skipped workspaces
without saying which or why, so three workspaces yielding nothing looked
identical to three workspaces that were genuinely idle. And it read only the
per-workspace stores, while Cursor had moved every composer chat into one
global store keyed by ``composerHeaders.workspaceId`` — so the workspaces with
the most chat were exactly the ones reported as idle.

Every test names ``--global-storage`` explicitly. The default points at the
live editor store, and a test that silently read it would both fail
unpredictably and touch a database no test has any business opening.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import time
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


def _global_storage(path: Path, composers: list[dict]) -> Path:
    """Build a globalStorage/state.vscdb in the shape Cursor writes.

    Each composer is ``{"id", "workspace", "age_days", "bubbles": [...]}`` and
    each bubble ``{"type", "text", "createdAt"}``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT, "
        "createdAt INTEGER, lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, "
        "recency INTEGER, checkpointAt INTEGER, value TEXT)"
    )
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    now_ms = time.time() * 1000
    for composer in composers:
        recency = int(now_ms - composer.get("age_days", 0) * 86_400_000)
        conn.execute(
            "INSERT INTO composerHeaders (composerId, workspaceId, recency) VALUES (?, ?, ?)",
            (composer["id"], composer.get("workspace"), recency),
        )
        for index, bubble in enumerate(composer.get("bubbles", [])):
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (f"bubbleId:{composer['id']}:b{index:03d}", json.dumps(bubble)),
            )
    conn.commit()
    conn.close()
    return path


def _run(module, storage: Path, out: Path, *extra: str, global_storage: Path | None = None) -> dict:
    argv = [
        "--storage",
        str(storage),
        "--out",
        str(out),
        "--global-storage",
        str(global_storage if global_storage is not None else storage / "no-such-global.vscdb"),
        *extra,
    ]
    buffer = io.StringIO()
    saved = sys.argv
    sys.argv = ["export-cursor-chats.py", *argv]
    try:
        with contextlib.redirect_stdout(buffer):
            assert module.main() == 0
    finally:
        sys.argv = saved
    return json.loads(buffer.getvalue())


def _records(path: Path) -> list[dict]:
    """Every non-meta line of an export, in file order."""
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return [line for line in lines if "_meta" not in line and "_truncated" not in line]


# --- skip attribution --------------------------------------------------------


def test_skip_reasons_are_reported(tmp_path):
    module = _module()
    storage = tmp_path / "workspaceStorage"

    # One real workspace, so the run is not trivially all-skips.
    _workspace(storage, "has-chat", {"aiService.prompts": [{"text": "how do I ship this"}]})
    # Present but idle: the arrays exist and are empty. This is what all three
    # of the real zero-record workspaces turned out to be.
    _workspace(storage, "idle-ws", {"aiService.prompts": [], "aiService.generations": []})
    # Unreadable file where a database should be.
    broken = storage / "broken-ws"
    broken.mkdir(parents=True)
    (broken / "state.vscdb").write_text("not a database at all")

    payload = _run(module, storage, tmp_path / "out")

    assert payload["exported"] == 1
    assert payload["skipped"] == 2
    assert payload["skipped_by_reason"] == {"no_records": 1, "sqlite_error": 1}

    by_workspace = {entry["workspace"]: entry["reason"] for entry in payload["skipped_sample"]}
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


def test_empty_window_is_harvested_like_any_other_workspace(tmp_path):
    """``empty-window`` was skipped on its name alone, and holds real chat.

    Cursor files a window opened without a folder under that fixed name. It is
    not a placeholder: on this machine it carried 2,843 bytes of prompts and
    14,427 bytes of generations, none of which ever reached the brain.
    """
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "empty-window", {"aiService.prompts": [{"text": "orphan window chat"}]})

    payload = _run(module, storage, tmp_path / "out")

    assert payload["exported"] == 1
    assert payload["skipped"] == 0
    body = _records(tmp_path / "out" / "cursor-empty-window.jsonl")
    assert [record["content"] for record in body] == ["orphan window chat"]


# --- global composer store ---------------------------------------------------


def test_composer_bubbles_merge_into_their_workspace(tmp_path):
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "ws-a", {"aiService.prompts": [{"text": "local prompt"}]})
    _workspace(storage, "ws-b", {"aiService.prompts": []})
    _global_storage(
        tmp_path / "globalStorage" / "state.vscdb",
        [
            {
                "id": "c-a",
                "workspace": "ws-a",
                "bubbles": [{"type": 1, "text": "global ask", "createdAt": "2026-08-20T10:00:00Z"}],
            },
            {
                "id": "c-b",
                "workspace": "ws-b",
                "bubbles": [
                    {"type": 2, "text": "reply in b", "createdAt": "2026-08-21T10:00:00Z"}
                ],
            },
        ],
    )
    out = tmp_path / "out"

    payload = _run(
        module, storage, out, global_storage=tmp_path / "globalStorage" / "state.vscdb"
    )

    assert payload["exported"] == 2
    assert payload["global_storage"]["workspaces"] == {"ws-a": 1, "ws-b": 1}
    # ws-a merges local and global into ONE file rather than a parallel pile.
    assert sorted(path.name for path in out.iterdir()) == [
        "cursor-ws-a.jsonl",
        "cursor-ws-b.jsonl",
    ]
    a_body = _records(out / "cursor-ws-a.jsonl")
    assert {record["content"] for record in a_body} == {"local prompt", "global ask"}
    assert [record["role"] for record in _records(out / "cursor-ws-b.jsonl")] == ["assistant"]
    # ws-b had no local records at all; without the global sweep it was a skip.
    assert payload["skipped"] == 0


def test_unresolvable_workspace_goes_to_the_unattributed_bucket(tmp_path):
    """A bubble whose workspace is gone is still chat, and must not vanish."""
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "ws-a", {"aiService.prompts": [{"text": "local"}]})
    _global_storage(
        tmp_path / "globalStorage" / "state.vscdb",
        [
            {
                "id": "c-gone",
                "workspace": "1785350482316",
                "bubbles": [{"type": 1, "text": "chat from a deleted workspace"}],
            },
            {
                "id": "c-null",
                "workspace": None,
                "bubbles": [{"type": 1, "text": "chat with no workspace at all"}],
            },
        ],
    )
    out = tmp_path / "out"

    _run(module, storage, out, global_storage=tmp_path / "globalStorage" / "state.vscdb")

    body = _records(out / f"cursor-{module.UNATTRIBUTED}.jsonl")
    assert {record["content"] for record in body} == {
        "chat from a deleted workspace",
        "chat with no workspace at all",
    }
    # The claimed id survives in the record, since the filename cannot carry it.
    claimed = {record["content"]: record["workspace_id"] for record in body}
    assert claimed["chat from a deleted workspace"] == "1785350482316"
    assert claimed["chat with no workspace at all"] is None


def test_recency_window_bounds_the_sweep(tmp_path):
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "ws-a", {"aiService.prompts": []})
    _global_storage(
        tmp_path / "globalStorage" / "state.vscdb",
        [
            {
                "id": "c-fresh",
                "workspace": "ws-a",
                "age_days": 2,
                "bubbles": [{"type": 1, "text": "this week"}],
            },
            {
                "id": "c-stale",
                "workspace": "ws-a",
                "age_days": 400,
                "bubbles": [{"type": 1, "text": "last year"}],
            },
        ],
    )
    out = tmp_path / "out"

    payload = _run(
        module,
        storage,
        out,
        "--since-days",
        "45",
        global_storage=tmp_path / "globalStorage" / "state.vscdb",
    )

    assert payload["global_storage"]["composers_in_window"] == 1
    contents = {record["content"] for record in _records(out / "cursor-ws-a.jsonl")}
    assert contents == {"this week"}

    # Widening the window reaches the old composer, so the bound is the window
    # and not some other accident of the fixture.
    wide = _run(
        module,
        storage,
        tmp_path / "wide",
        "--since-days",
        "500",
        global_storage=tmp_path / "globalStorage" / "state.vscdb",
    )
    assert wide["global_storage"]["composers_in_window"] == 2


def test_a_satisfied_workspace_stops_being_read(tmp_path):
    """Once a workspace has a file's worth of the newest chat, stop reading it.

    This is the rule that turns a 2.10GB store into a 116MB read: everything
    older than the file budget would be dropped by ``render_jsonl`` anyway.
    """
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "ws-a", {"aiService.prompts": []})
    composers = [
        {
            "id": f"c-{index:03d}",
            "workspace": "ws-a",
            "age_days": index,
            "bubbles": [{"type": 1, "text": "x" * 900}],
        }
        for index in range(40)
    ]
    _global_storage(tmp_path / "globalStorage" / "state.vscdb", composers)

    payload = _run(
        module,
        storage,
        tmp_path / "out",
        "--max-file-bytes",
        "5000",
        global_storage=tmp_path / "globalStorage" / "state.vscdb",
    )

    stats = payload["global_storage"]
    assert stats["composers_in_window"] == 40
    # 900 bytes of text per composer against a 5,000-byte file: a handful, not
    # all forty, and never zero.
    assert 1 < stats["composers_scanned"] < 12


def test_budget_stops_the_walk_and_says_so(tmp_path):
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "ws-a", {"aiService.prompts": []})
    composers = [
        {
            "id": f"c-{index:03d}",
            "workspace": "ws-a",
            "age_days": index,
            "bubbles": [{"type": 1, "text": "y" * 200} for _ in range(5)],
        }
        for index in range(20)
    ]
    _global_storage(tmp_path / "globalStorage" / "state.vscdb", composers)

    payload = _run(
        module,
        storage,
        tmp_path / "out",
        "--max-file-bytes",
        "10000000",
        "--max-bubbles",
        "12",
        global_storage=tmp_path / "globalStorage" / "state.vscdb",
    )

    stats = payload["global_storage"]
    assert stats["budget_exhausted"] == "max_bubbles"
    assert stats["bubbles_read"] < 20 * 5


def test_bubbles_are_read_by_range_never_by_like(tmp_path):
    """``LIKE 'bubbleId:<id>:%'`` full-scans a 7.8GB table; the range is 0.8ms.

    The range is what makes the sweep affordable, so this asserts the query
    shape and not only the rows it returns.
    """
    module = _module()
    low, high = module.bubble_key_range("c-1")
    assert (low, high) == ("bubbleId:c-1:", "bubbleId:c-1;")

    db = tmp_path / "globalStorage" / "state.vscdb"
    _global_storage(
        db,
        [
            {"id": "c-1", "workspace": "ws-a", "bubbles": [{"type": 1, "text": "mine"}]},
            {"id": "c-10", "workspace": "ws-a", "bubbles": [{"type": 1, "text": "not mine"}]},
        ],
    )
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        records, _ = module.harvest_global_bubbles(
            conn,
            known_workspaces={"ws-a"},
            since_ms=0,
            file_budget=200_000,
            max_record_bytes=16_000,
            max_bubbles=1000,
            max_scan_bytes=10_000_000,
        )
    finally:
        conn.close()

    assert {record["content"] for record in records["ws-a"]} == {"mine", "not mine"}
    bubble_sql = [sql for sql in statements if "cursorDiskKV" in sql]
    assert bubble_sql, "the bubble table was never queried"
    assert not any("LIKE" in sql.upper() for sql in bubble_sql)
    # ``c-10`` must not be swept up by ``c-1``'s range.
    plan = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = plan.execute(
            "SELECT key FROM cursorDiskKV WHERE key >= ? AND key < ?",
            module.bubble_key_range("c-1"),
        ).fetchall()
    finally:
        plan.close()
    assert all(key.startswith("bubbleId:c-1:") for (key,) in rows)


# --- truncation --------------------------------------------------------------


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


def test_oversized_bubbles_are_capped_in_the_export(tmp_path):
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "ws-a", {"aiService.prompts": []})
    _global_storage(
        tmp_path / "globalStorage" / "state.vscdb",
        [
            {
                "id": "c-big",
                "workspace": "ws-a",
                "bubbles": [{"type": 2, "text": "S" + "z" * 100_000 + "E"}],
            }
        ],
    )
    out = tmp_path / "out"

    payload = _run(
        module,
        storage,
        out,
        "--max-record-bytes",
        "2000",
        global_storage=tmp_path / "globalStorage" / "state.vscdb",
    )

    assert payload["global_storage"]["bubbles_truncated"] == 1
    (record,) = _records(out / "cursor-ws-a.jsonl")
    assert len(record["content"].encode("utf-8")) <= 2_000
    assert record["content"].startswith("S")
    assert record["content"].endswith("E")


def test_file_budget_drops_the_oldest_records_not_the_newest(tmp_path):
    """The budget used to be spent front to back, dropping the newest chat.

    That is exactly backwards for a memory system, and it got worse the moment
    the global store multiplied the volume behind the budget.
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


# --- redaction, idempotence --------------------------------------------------


def test_composer_bubbles_are_redacted(tmp_path):
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "ws-a", {"aiService.prompts": []})
    secret = "sk-ant-api03-" + "A" * 40
    _global_storage(
        tmp_path / "globalStorage" / "state.vscdb",
        [
            {
                "id": "c-1",
                "workspace": "ws-a",
                "bubbles": [{"type": 1, "text": f"here is my key {secret} keep it safe"}],
            }
        ],
    )
    out = tmp_path / "out"

    _run(module, storage, out, global_storage=tmp_path / "globalStorage" / "state.vscdb")

    body = (out / "cursor-ws-a.jsonl").read_text()
    assert secret not in body
    assert "keep it safe" in body


def test_a_second_run_over_unchanged_chat_rewrites_nothing(tmp_path):
    """Both gates matter: content-compare here, and stat-fingerprint downstream.

    ``import-history`` keys its skip on path plus size plus mtime_ns, so a
    rewrite with identical bytes would still cost a full re-import.
    """
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "ws-a", {"aiService.prompts": [{"text": "first question"}]})
    _global_storage(
        tmp_path / "globalStorage" / "state.vscdb",
        [
            {
                "id": "c-1",
                "workspace": "ws-a",
                "bubbles": [
                    {
                        "type": 1,
                        "text": f"turn {index}",
                        "createdAt": f"2026-08-{index:02d}T00:00:00Z",
                    }
                    for index in range(1, 12)
                ],
            }
        ],
    )
    out = tmp_path / "out"
    db = tmp_path / "globalStorage" / "state.vscdb"

    first = _run(module, storage, out, global_storage=db)
    exported = out / "cursor-ws-a.jsonl"
    stat_before = exported.stat()
    second = _run(module, storage, out, global_storage=db)

    assert first["exported"] == 1 and first["unchanged"] == 0
    assert second["exported"] == 0 and second["unchanged"] == 1
    assert exported.stat().st_mtime_ns == stat_before.st_mtime_ns
    body = exported.read_text().splitlines()
    assert json.loads(body[0])["_meta"]["workspace_id"] == "ws-a"
    assert json.loads(body[0])["_meta"]["composer_record_count"] == 11


def test_workspace_with_records_still_exports_and_regates(tmp_path):
    """The local-only path keeps working when there is no global store at all."""
    module = _module()
    storage = tmp_path / "workspaceStorage"
    _workspace(storage, "has-chat", {"aiService.prompts": [{"text": "first question"}]})
    out = tmp_path / "out"

    first = _run(module, storage, out)
    second = _run(module, storage, out)

    assert first["global_storage"] == {
        "status": "absent",
        "path": str(storage / "no-such-global.vscdb"),
    }
    assert first["exported"] == 1 and first["unchanged"] == 0
    assert second["exported"] == 0 and second["unchanged"] == 1
    body = (out / "cursor-has-chat.jsonl").read_text().splitlines()
    assert json.loads(body[0])["_meta"]["workspace_id"] == "has-chat"
    assert json.loads(body[1])["content"] == "first question"


# --- timestamps --------------------------------------------------------------


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
