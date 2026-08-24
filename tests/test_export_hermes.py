"""Tests for scripts/export-hermes-transcripts.py.

The fleet moved from one ``~/.hermes/state.db`` to one store per agent profile,
and the exporter kept reporting success against the frozen legacy store alone.
These tests pin the two properties that made that failure invisible: every
profile store is swept, and the sweep says so per profile.

The profile fixture deliberately declares its columns in a different order than
the legacy fixture. Both real schemas are column-name-compatible supersets of
what the exporter reads, but their physical order differs, so a positional read
would pass against one store and silently mis-attribute the other.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts" / "export-hermes-transcripts.py"
    spec = importlib.util.spec_from_file_location("export_hermes_transcripts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_LEGACY_SESSIONS = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    display_name TEXT,
    model TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    message_count INTEGER DEFAULT 0
)
"""
# Same column names, deliberately different physical order.
_PROFILE_SESSIONS = """
CREATE TABLE sessions (
    source TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    started_at REAL NOT NULL,
    model TEXT,
    id TEXT PRIMARY KEY,
    ended_at REAL,
    display_name TEXT
)
"""
_LEGACY_MESSAGES = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
)
"""
_PROFILE_MESSAGES = """
CREATE TABLE messages (
    session_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    content TEXT,
    role TEXT NOT NULL,
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT
)
"""


def _build_db(path: Path, session_ids: list[str], *, profile_shaped: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(_PROFILE_SESSIONS if profile_shaped else _LEGACY_SESSIONS)
    conn.execute(_PROFILE_MESSAGES if profile_shaped else _LEGACY_MESSAGES)
    for index, sid in enumerate(session_ids):
        conn.execute(
            "INSERT INTO sessions (id, source, display_name, model, started_at, ended_at, "
            "message_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, "telegram", f"chat {index}", "opus", 1_700_000_000.0 + index, None, 2),
        )
        for turn, (role, content) in enumerate((("user", "hi"), ("assistant", "hello"))):
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_name, timestamp, active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (sid, role, content, None, 1_700_000_000.0 + turn),
            )
    conn.commit()
    conn.close()
    return path


def _estate(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A legacy store plus two profile stores, laid out as on the real machine."""
    home = tmp_path / "hermes"
    legacy = _build_db(home / "state.db", ["legacy-a", "legacy-b"], profile_shaped=False)
    profiles = home / "profiles"
    _build_db(profiles / "squirtlecoframe" / "state.db", ["sq-1"], profile_shaped=True)
    _build_db(profiles / "togepicoframe" / "state.db", ["tg-1", "tg-2"], profile_shaped=True)
    return legacy, profiles, tmp_path / "out"


def _run(module, legacy: Path, profiles: Path, out: Path, *extra: str) -> dict:
    """Drive the script through its real CLI and return the JSON it prints."""
    argv = ["--db", str(legacy), "--profiles-root", str(profiles), "--out", str(out), *extra]
    buffer = io.StringIO()
    saved = sys.argv
    sys.argv = ["export-hermes-transcripts.py", *argv]
    try:
        with contextlib.redirect_stdout(buffer):
            assert module.main() == 0
    finally:
        sys.argv = saved
    return json.loads(buffer.getvalue())


def test_exports_legacy_and_all_profiles(tmp_path):
    module = _module()
    legacy, profiles, out = _estate(tmp_path)

    payload = _run(module, legacy, profiles, out)

    assert payload["legacy"] == {"exported": 2, "unchanged": 0}
    assert payload["profiles"] == {
        "squirtlecoframe": {"exported": 1, "unchanged": 0},
        "togepicoframe": {"exported": 2, "unchanged": 0},
    }
    assert payload["exported"] == 5
    assert payload["errors"] == []

    # Legacy keeps the flat filename scheme; profiles get a subdirectory each.
    assert (out / "hermes-legacy-a.jsonl").is_file()
    assert (out / "squirtlecoframe" / "hermes-sq-1.jsonl").is_file()
    assert (out / "togepicoframe" / "hermes-tg-2.jsonl").is_file()


def test_profile_output_paths_do_not_collide_with_legacy(tmp_path):
    """A profile session id equal to a legacy one must still write two files."""
    module = _module()
    home = tmp_path / "hermes"
    legacy = _build_db(home / "state.db", ["shared-id"], profile_shaped=False)
    profiles = home / "profiles"
    _build_db(profiles / "pikacoframe" / "state.db", ["shared-id"], profile_shaped=True)
    out = tmp_path / "out"

    payload = _run(module, legacy, profiles, out)

    assert payload["legacy"]["exported"] == 1
    assert payload["profiles"]["pikacoframe"]["exported"] == 1
    legacy_file = out / "hermes-shared-id.jsonl"
    profile_file = out / "pikacoframe" / "hermes-shared-id.jsonl"
    assert legacy_file.is_file() and profile_file.is_file()
    assert legacy_file.read_text() != profile_file.read_text()


def test_unchanged_sessions_preserve_mtime(tmp_path):
    """The harvester's fingerprint gate keys on mtime; a no-op run must not touch it."""
    module = _module()
    legacy, profiles, out = _estate(tmp_path)
    _run(module, legacy, profiles, out)

    watched = [
        out / "hermes-legacy-a.jsonl",
        out / "squirtlecoframe" / "hermes-sq-1.jsonl",
    ]
    # Backdate so a rewrite is detectable even on a coarse-grained clock.
    for path in watched:
        os.utime(path, (1_600_000_000, 1_600_000_000))
    before = [path.stat().st_mtime for path in watched]

    payload = _run(module, legacy, profiles, out)

    assert payload["exported"] == 0
    assert payload["unchanged"] == 5
    assert payload["profiles"]["squirtlecoframe"] == {"exported": 0, "unchanged": 1}
    assert [path.stat().st_mtime for path in watched] == before


def test_meta_line_carries_profile(tmp_path):
    module = _module()
    legacy, profiles, out = _estate(tmp_path)
    _run(module, legacy, profiles, out)

    profile_meta = json.loads(
        (out / "squirtlecoframe" / "hermes-sq-1.jsonl").read_text().splitlines()[0]
    )["_meta"]
    assert profile_meta["profile"] == "squirtlecoframe"
    assert profile_meta["session_id"] == "sq-1"
    assert profile_meta["source"] == "telegram"
    assert profile_meta["display_name"] == "chat 0"

    # The legacy store has no profile, and must not grow the key: its 800-odd
    # existing exports have to keep comparing equal or the whole corpus rewrites.
    legacy_meta = json.loads((out / "hermes-legacy-a.jsonl").read_text().splitlines()[0])["_meta"]
    assert "profile" not in legacy_meta


def test_opens_source_read_only(tmp_path):
    """These are live gateway databases: the exporter must never hold a write lock."""
    module = _module()
    legacy = _build_db(tmp_path / "state.db", ["only"], profile_shaped=False)
    uris: list[str] = []
    real_connect = module.sqlite3.connect

    def spy(database, *args, **kwargs):
        conn = real_connect(database, *args, **kwargs)
        uris.append(database)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('x', 'y', 1)")
        return conn

    module.sqlite3.connect = spy
    try:
        module.export_db(legacy, tmp_path / "out", 200_000)
    finally:
        module.sqlite3.connect = real_connect

    assert uris and all("mode=ro" in uri for uri in uris)


def test_one_unreadable_profile_does_not_abort_the_others(tmp_path):
    module = _module()
    legacy, profiles, out = _estate(tmp_path)
    (profiles / "bulbacoframe").mkdir(parents=True)
    (profiles / "bulbacoframe" / "state.db").write_text("this is not a database")

    payload = _run(module, legacy, profiles, out)

    assert [entry["profile"] for entry in payload["errors"]] == ["bulbacoframe"]
    assert set(payload["profiles"]) == {"squirtlecoframe", "togepicoframe"}
    assert payload["exported"] == 5


def test_no_profiles_sweeps_only_the_named_db(tmp_path):
    module = _module()
    legacy, profiles, out = _estate(tmp_path)

    payload = _run(module, legacy, profiles, out, "--no-profiles")

    assert payload["profiles"] == {}
    assert payload["exported"] == 2
    assert not (out / "squirtlecoframe").exists()


def test_missing_legacy_store_still_sweeps_profiles(tmp_path):
    """The legacy store is retired eventually; that must not silence the fleet."""
    module = _module()
    _, profiles, out = _estate(tmp_path)
    absent = tmp_path / "hermes" / "gone.db"

    payload = _run(module, absent, profiles, out)

    assert payload["legacy"]["reason"].startswith("no state.db at")
    assert payload["exported"] == 3
    assert set(payload["profiles"]) == {"squirtlecoframe", "togepicoframe"}
