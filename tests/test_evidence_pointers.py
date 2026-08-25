"""Transcript evidence stored by reference instead of by value.

The window text was 99.13% of all evidence body bytes and 82.4% of the event
ledger. Storing a pointer instead is only safe if three things hold, and each
has a test here: the window can be rebuilt byte-for-byte and proven so, a
transcript that has moved on says so in a typed result rather than raising, and
every consumer that used to read ``body`` still works when ``body`` is empty.
"""

from __future__ import annotations

import json
from pathlib import Path

from ocbrain.bundle import export_bundle
from ocbrain.cli import import_history_file_v1
from ocbrain.core_v1 import (
    evidence_body_ref,
    get_core_v1_belief,
    get_core_v1_evidence,
    init_core_v1,
    project_core_v1,
    sha256_text,
)
from ocbrain.db import connect
from ocbrain.deslop import rewindowed_evidence_id
from ocbrain.history_window import (
    HISTORY_HEAD_CHARS,
    UNAVAILABLE_SOURCE_MISSING,
    UNAVAILABLE_WINDOW_CHANGED,
    build_history_window,
    history_text_window,
    rehydrate_history_window,
)
from ocbrain.mcp_v1 import _source_handles_for_belief, expand_source_v1
from ocbrain.scope import ScopeContext
from ocbrain.shared_context import issue_source_handles

MAX_BYTES = 8_000
HEAD_MARKER = "head-visible-sentinel"
TAIL_MARKER = "tail-visible-sentinel"


def _core(tmp_path: Path):
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    return conn


def _transcript(tmp_path: Path, *, name: str = "session.jsonl", rows: int = 4_000) -> Path:
    path = tmp_path / ".claude" / "projects" / "probe"
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_text(
        json.dumps({"role": "user", "content": HEAD_MARKER})
        + "\n"
        + "".join(json.dumps({"role": "assistant", "content": f"row {i}"}) + "\n"
                 for i in range(rows))
        + json.dumps({"role": "assistant", "content": TAIL_MARKER})
        + "\n",
        encoding="utf-8",
    )
    return target


def _import(conn, path: Path, *, privacy_scope: str = "project") -> dict:
    result = import_history_file_v1(
        conn, path, project="probe", privacy_scope=privacy_scope, max_bytes=MAX_BYTES
    )
    conn.commit()
    return result


def test_the_refactored_builder_produces_the_same_window_as_before(tmp_path: Path) -> None:
    """Splitting sample-from-compose must not move a single byte.

    The evidence id is derived from this text, so a changed window is a changed
    identity for every transcript in the corpus.
    """
    path = _transcript(tmp_path)
    window = build_history_window(path, max_bytes=MAX_BYTES, source_uri=str(path))
    assert window.text == history_text_window(path, max_bytes=MAX_BYTES)
    assert window.head == window.text[:HISTORY_HEAD_CHARS]
    assert window.body_ref["window_sha256"] == sha256_text(window.text)

    # Pinned against the shape itself, not against the other function: both
    # call the same composer, so agreeing with each other proves nothing about
    # agreeing with the windows already recorded in the corpus.
    marker_start = window.text.index("\n\n[... ")
    marker_end = window.text.index(" ...]\n\n") + len(" ...]\n\n")
    assert len(window.text[:marker_start].encode("utf-8")) == MAX_BYTES // 2
    assert len(window.text[marker_end:].encode("utf-8")) == MAX_BYTES - MAX_BYTES // 2
    assert HEAD_MARKER in window.text[:marker_start]
    assert TAIL_MARKER in window.text[marker_end:]


def test_a_pointer_from_an_older_builder_is_not_silently_rebuilt(tmp_path: Path) -> None:
    """A window recorded by a different builder cannot be reproduced by this one."""
    path = _transcript(tmp_path)
    window = build_history_window(path, max_bytes=MAX_BYTES, source_uri=str(path))
    stale = dict(window.body_ref, window_builder="history_text_window.v0")
    assert rehydrate_history_window(stale).reason == UNAVAILABLE_WINDOW_CHANGED
    assert rehydrate_history_window(window.body_ref).text == window.text


def test_import_stores_a_pointer_not_the_window(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    result = _import(conn, path)

    evidence = get_core_v1_evidence(conn, str(result["evidence_id"]))
    assert evidence["body"] == ""
    assert HEAD_MARKER in evidence["body_head"]
    assert len(evidence["body_head"]) <= HISTORY_HEAD_CHARS

    body_ref = evidence_body_ref(evidence)
    assert body_ref is not None
    assert body_ref["source_uri"] == str(path.resolve())
    assert body_ref["source_bytes"] == path.stat().st_size
    # `source_content_hash` was empty on every history row ever written.
    assert evidence["source_content_hash"] == body_ref["window_input_sha256"]
    # The row's content hash is the window's, never sha256("").
    assert evidence["content_hash"] == body_ref["window_sha256"]
    assert evidence["content_hash"] != sha256_text("")
    # And the head is not also duplicated into metadata.
    row = conn.execute(
        "SELECT metadata_json FROM evidence_objects WHERE evidence_id=?",
        (evidence["canonical_id"],),
    ).fetchone()
    assert HEAD_MARKER not in row["metadata_json"]


def test_the_evidence_id_is_still_the_id_the_window_would_have_had(tmp_path: Path) -> None:
    """Moving the text must not move the identity of a single row."""
    from ocbrain.cli import scope_for_privacy
    from ocbrain.ids import stable_id

    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    result = _import(conn, path)
    scope = scope_for_privacy("probe", "project")
    expected = stable_id(
        "evd",
        history_text_window(path, max_bytes=MAX_BYTES),
        "claude_history_file",
        str(path.resolve()),
        scope.scope_id,
    )
    assert result["evidence_id"] == expected


def test_brain_source_rereads_the_window_from_disk_and_verifies_it(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    result = _import(conn, path)
    payload = _expand(conn, str(result["belief_id"]))

    assert payload["content_availability"] == "available"
    assert payload["hash_verified"] is True
    assert HEAD_MARKER in payload["content"]
    assert TAIL_MARKER in payload["content"]


def test_a_deleted_transcript_is_a_typed_result_not_an_exception(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    result = _import(conn, path)
    handle_id = _issue(conn, str(result["belief_id"]))
    path.rename(path.with_suffix(".moved"))

    payload = expand_source_v1(
        conn,
        handle_id,
        context=ScopeContext(project="probe"),
        max_chars=8_000,
    )
    assert payload["content_availability"] == "content_unavailable"
    assert payload["unavailable_reason"] == UNAVAILABLE_SOURCE_MISSING
    assert payload["hash_verified"] is False
    assert payload["content"] == ""
    assert payload["body_storage"] == "pointer"
    # The reader still gets the excerpt the ledger actually recorded, labelled.
    assert HEAD_MARKER in payload["recorded_head_excerpt"]
    assert "not verified against the current source file" in (
        payload["recorded_head_excerpt_note"]
    )


def test_a_grown_transcript_is_reported_as_changed_not_served_differently(
    tmp_path: Path,
) -> None:
    """The window is not a slice; a longer file yields a different window."""
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    result = _import(conn, path)
    handle_id = _issue(conn, str(result["belief_id"]))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "assistant", "content": "appended later"}) + "\n")

    payload = expand_source_v1(
        conn,
        handle_id,
        context=ScopeContext(project="probe"),
        max_chars=8_000,
    )
    assert payload["content_availability"] == "content_unavailable"
    assert payload["unavailable_reason"] == UNAVAILABLE_WINDOW_CHANGED


def test_a_rewritten_transcript_of_the_same_length_is_also_caught(tmp_path: Path) -> None:
    """Length alone is not the check; the sampled ends are hashed too."""
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    _import(conn, path)
    evidence = get_core_v1_evidence(
        conn,
        str(conn.execute("SELECT evidence_id FROM evidence_objects").fetchone()[0]),
    )
    body_ref = evidence_body_ref(evidence)
    original = path.read_bytes()
    path.write_bytes(original.replace(TAIL_MARKER.encode(), b"x" * len(TAIL_MARKER)))
    assert path.stat().st_size == len(original)
    assert rehydrate_history_window(body_ref).reason == UNAVAILABLE_WINDOW_CHANGED


def test_the_rewindow_dedup_gate_still_matches_on_an_empty_body(tmp_path: Path) -> None:
    """The gate read substr(body, 1, 2000); pointer bodies are empty.

    Without the fallback to `body_head` every pointer row's head compares equal
    to the empty prefix, and unrelated transcripts collapse onto one id.
    """
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    first = _import(conn, path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "assistant", "content": "later turn"}) + "\n")
    text = history_text_window(path, max_bytes=MAX_BYTES)

    matched = rewindowed_evidence_id(
        conn, source_uri=str(path.resolve()), kind="claude_history_file", text=text
    )
    assert matched == first["evidence_id"]

    # A genuinely different transcript at the same path must NOT match.
    path.write_text(
        "".join(json.dumps({"role": "user", "content": f"unrelated {i}"}) + "\n"
                for i in range(4_000)),
        encoding="utf-8",
    )
    other = history_text_window(path, max_bytes=MAX_BYTES)
    assert (
        rewindowed_evidence_id(
            conn, source_uri=str(path.resolve()), kind="claude_history_file", text=other
        )
        is None
    )


def test_a_rewindowed_reimport_is_a_true_no_op(tmp_path: Path) -> None:
    """It must not re-propose the belief and append the transcript again."""
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    first = _import(conn, path)
    events_before = conn.execute("SELECT COUNT(*) FROM brain_events").fetchone()[0]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "assistant", "content": "later turn"}) + "\n")

    second = _import(conn, path)
    assert second["evidence_id"] == first["evidence_id"]
    assert second["changed"] is False
    assert conn.execute("SELECT COUNT(*) FROM brain_events").fetchone()[0] == events_before


def test_bundle_export_carries_a_pointer_row_instead_of_dropping_it(tmp_path: Path) -> None:
    """An empty body used to be refused as `invalid_body_size`."""
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    # human_export needs an exportable scope; the pointer handling is the same.
    result = _import(conn, path, privacy_scope="public")
    output = tmp_path / "bundle.json"

    export = export_bundle(
        conn,
        output,
        evidence_ids=[str(result["evidence_id"])],
        context=ScopeContext(project="probe"),
        approve_egress=True,
    )
    assert export["item_count"] == 1
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert HEAD_MARKER in bundle["items"][0]["body"]
    audit = conn.execute(
        "SELECT included_json FROM egress_audits WHERE id=?", (export["egress_audit_id"],)
    ).fetchone()
    assert json.loads(audit["included_json"])[0]["body_storage"] == "pointer_head_excerpt"


def test_a_full_replay_reproduces_the_pointer_row_exactly(tmp_path: Path) -> None:
    """Everything the projection needs rides the event body, or replay drifts."""
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    _import(conn, path)
    before = _evidence_rows(conn)

    conn.execute("DELETE FROM belief_evidence")
    conn.execute("DELETE FROM evidence_objects")
    conn.execute("DELETE FROM current_beliefs")
    conn.execute("UPDATE projection_cursor SET last_event_rowid = 0 WHERE id = 1")
    project_core_v1(conn, full=True)
    conn.commit()

    assert _evidence_rows(conn) == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_belief_body_holds_the_head_not_the_whole_window(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    path = _transcript(tmp_path)
    result = _import(conn, path)
    belief = get_core_v1_belief(conn, str(result["belief_id"]))
    assert HEAD_MARKER in belief["body"]
    assert TAIL_MARKER not in belief["body"]
    assert len(belief["body"]) < MAX_BYTES // 2


def _evidence_rows(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT evidence_id, body, body_head, kind, content_hash, source_content_hash, "
            "source_uri, metadata_json FROM evidence_objects ORDER BY evidence_id"
        )
    ]


def _issue(conn, belief_id: str) -> str:
    handles = _source_handles_for_belief(
        conn,
        belief_id,
        context=ScopeContext(project="probe"),
        delivery_target="local_model",
    )
    assert handles, "no source handle was issued for the imported transcript"
    from ocbrain.core_v1 import record_core_v1_retrieval

    retrieval_id = record_core_v1_retrieval(
        conn,
        query="probe",
        context={"project": "probe"},
        items=[{"belief_id": belief_id, "score": 1.0}],
        runtime="test",
        task_ref=None,
        session_id=None,
    )
    issue_source_handles(conn, handles, retrieval_use_id=retrieval_id)
    conn.commit()
    return str(handles[0]["id"])


def _expand(conn, belief_id: str) -> dict:
    return expand_source_v1(
        conn,
        _issue(conn, belief_id),
        context=ScopeContext(project="probe"),
        max_chars=20_000,
    )
