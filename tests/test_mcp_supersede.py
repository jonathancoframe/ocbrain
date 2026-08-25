"""``brain.supersede`` on the wire: tiering, review queue, reads, and packets.

The tool is deliberately in the runtime profile. An agent that has just proved
a stored fact wrong is the party best placed to say so, and every path that
made it ask an admin first went unused -- so the corpus kept serving facts
somebody had already disproved. What bounds the authority is not a profile gate
but the tier: doctrine, pinned beliefs, and anything over the daily cap become a
proposal an admin decides, and the proposal itself *is* the pending correction.
"""

from __future__ import annotations

import json
import sqlite3
from array import array
from pathlib import Path

import pytest

from ocbrain.core_v1 import (
    append_core_event,
    init_core_v1,
    record_core_v1_evidence,
)
from ocbrain.db import connect
from ocbrain.mcp import handle_request
from ocbrain.mcp_v1 import pending_supersede_count
from ocbrain.scope import ScopeTag

PROJECT_SCOPE = ScopeTag(
    "project",
    "project:bountiful",
    visibility="internal",
    egress_policy="local_only",
    provenance="test",
)
DOCTRINE_SCOPE = ScopeTag(
    "global",
    "global:doctrine",
    visibility="internal",
    egress_policy="local_only",
    provenance="test",
)
CONTEXT = {"project": "bountiful"}


def _core(tmp_path: Path):
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    return conn


def _seed(
    conn,
    *,
    belief_id: str,
    body: str,
    scope: ScopeTag = PROJECT_SCOPE,
    attributes: dict | None = None,
    confidence: float = 0.9,
) -> str:
    evidence_id, _event = record_core_v1_evidence(
        conn,
        body=f"evidence for {belief_id}",
        kind="observation",
        scope=scope,
        writer="test",
    )
    proposal = append_core_event(
        conn,
        "compilation_proposed",
        {
            "belief_id": belief_id,
            "belief_type": "wiki_fact",
            "body": body,
            "evidence_ids": [evidence_id],
            "scope": scope.to_dict(),
            "confidence": confidence,
            "attributes": attributes or {},
        },
        writer="test",
    )
    append_core_event(
        conn,
        "compilation_decided",
        {"proposal_event_id": proposal, "decision": "approve", "actor": "test"},
        writer="test",
        project=True,
    )
    conn.commit()
    return belief_id


def _call(conn, name, arguments, *, profile=None, session_state=None, request_id=1):
    return handle_request(
        conn,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        profile=profile,
        session_state=session_state,
    )


def _payload(response):
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _supersede(conn, target, body, *, reason="the stored host no longer exists", **kwargs):
    return _payload(
        _call(
            conn,
            "brain.supersede",
            {"target": target, "body": body, "reason": reason, "context": CONTEXT},
            **kwargs,
        )
    )


def _serving(conn) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT belief_id FROM current_beliefs WHERE serve=1 AND status='current'"
        )
    }


def _corrections(conn, op: str) -> list[dict]:
    return [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT body_json FROM brain_events WHERE kind='correction_recorded' "
            "AND json_extract(body_json, '$.op')=? ORDER BY rowid",
            (op,),
        )
    ]


# --------------------------------------------------------------------------- #
# The runtime surface
# --------------------------------------------------------------------------- #
def test_supersede_is_callable_in_the_runtime_profile(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")

    payload = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    assert payload["mode"] == "direct"
    assert _serving(conn) == {payload["successor_id"]}
    assert payload["correction_event_id"]


def test_a_legacy_core_does_not_advertise_supersede(tmp_path: Path) -> None:
    """The primitive is defined in v1 events; a legacy core has nowhere to put it."""
    from ocbrain.db import init_db

    conn = connect(tmp_path / "legacy.sqlite")
    init_db(conn)
    response = handle_request(conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "brain.supersede" not in {tool["name"] for tool in response["result"]["tools"]}


# --------------------------------------------------------------------------- #
# Tiering: the routing predicate, never a refusal
# --------------------------------------------------------------------------- #
def test_a_pinned_target_becomes_a_proposal(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    _payload(
        _call(
            conn,
            "brain.correct",
            {"layer": "belief", "target": old, "op": "pin"},
            profile="admin",
        )
    )

    payload = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    assert payload["mode"] == "pending"
    assert "pinned" in payload["pending_reason"]
    assert "correction_event_id" not in payload
    # The contested belief keeps serving until an admin decides.
    assert _serving(conn) == {old}
    assert pending_supersede_count(conn) == 1


def test_doctrine_is_never_replaced_unattended(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    old = _seed(
        conn,
        belief_id="belief:doctrine",
        body="Agents ask permission before spending money.",
        scope=DOCTRINE_SCOPE,
    )

    payload = _payload(
        _call(
            conn,
            "brain.supersede",
            {
                "target": old,
                "body": "Agents ask permission before spending money or mutating production.",
                "reason": "the rule now covers production mutation",
                "context": {"project": "bountiful"},
            },
        )
    )

    assert payload["mode"] == "pending"
    assert "doctrine" in payload["pending_reason"]
    assert _serving(conn) == {old}


def test_the_rate_cap_routes_to_pending_and_never_refuses(tmp_path: Path, monkeypatch) -> None:
    """An agent over its budget still gets its correction recorded, as a proposal.

    Refusing would put the agent back where it started -- holding a correction
    with nowhere to put it -- which is the failure this whole primitive exists
    to end. The cap bounds unattended *authority*, not the right to be heard.
    """
    monkeypatch.setenv("OCBRAIN_SUPERSEDE_DIRECT_CAP", "1")
    conn = _core(tmp_path)
    first = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    second = _seed(conn, belief_id="belief:job", body="The hourly job runs at :05.")

    landed = _supersede(conn, first, "The research VM is reached with ssh asa2.")
    capped = _supersede(conn, second, "The hourly job runs at :20 since the July deploy.")

    assert landed["mode"] == "direct"
    assert capped["mode"] == "pending"
    assert "rate cap" in capped["pending_reason"]
    assert _serving(conn) == {landed["successor_id"], second}


def test_pending_all_sends_every_supersession_to_review(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OCBRAIN_SUPERSEDE_TIER", "pending_all")
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")

    payload = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    assert payload["mode"] == "pending"
    assert "pending_all" in payload["pending_reason"]
    assert _serving(conn) == {old}


def test_an_unrecognised_tier_falls_back_to_the_default(tmp_path: Path, monkeypatch) -> None:
    """A typo resolves to the documented default, not to whichever tier sorts first."""
    monkeypatch.setenv("OCBRAIN_SUPERSEDE_TIER", "pendign_all")
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")

    assert _supersede(conn, old, "The research VM is reached with ssh asa2.")["mode"] == "direct"


def test_the_tier_flag_tolerates_case_and_padding(tmp_path: Path, monkeypatch) -> None:
    """Operators set this by hand, in a JSON file or a shell export."""
    monkeypatch.setenv("OCBRAIN_SUPERSEDE_TIER", "  Pending_All  ")
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")

    assert _supersede(conn, old, "The research VM is reached with ssh asa2.")["mode"] == "pending"


# --------------------------------------------------------------------------- #
# The pending ledger is the undecided proposal
# --------------------------------------------------------------------------- #
def test_a_pending_supersession_is_listed_and_counted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OCBRAIN_SUPERSEDE_TIER", "pending_all")
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    payload = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    listed = _payload(_call(conn, "brain.proposals", {}, profile="admin"))["proposals"]
    pending = [item for item in listed if item["proposal_event_id"] == payload["proposal_event_id"]]
    assert pending and pending[0]["decided"] is False
    assert pending[0]["attributes"]["supersedes"] == old
    assert pending[0]["supersede_requested_by"] == "agent"

    digest = _payload(_call(conn, "brain.digest", {"context": CONTEXT}))
    assert digest["pending_corrections"] == 1


def test_approving_a_pending_supersession_completes_the_pair(tmp_path: Path, monkeypatch) -> None:
    """Approval has to finish both halves or the corpus serves the conflict.

    The undecided proposal is the pending correction. If approving it only
    compiled the successor, the old belief and its replacement would both serve
    and nothing would ever close the gap.
    """
    monkeypatch.setenv("OCBRAIN_SUPERSEDE_TIER", "pending_all")
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    payload = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    decision = _payload(
        _call(
            conn,
            "brain.proposal_decide",
            {
                "proposal_event_id": payload["proposal_event_id"],
                "decision": "approve",
                "actor": "human:jonathan",
            },
            profile="admin",
        )
    )

    assert decision["supersede_status"] == "retired"
    assert decision["superseded_id"] == old
    assert _serving(conn) == {payload["successor_id"]}
    correction = _corrections(conn, "supersede")[-1]
    # The admin owns the decision; the agent that asked stays named.
    assert correction["author"] == "human:jonathan"
    assert correction["requested_by"] == "agent"
    assert correction["successor_id"] == payload["successor_id"]
    assert correction["hard"] is False


def test_rejecting_a_pending_supersession_leaves_the_old_belief_serving(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OCBRAIN_SUPERSEDE_TIER", "pending_all")
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    payload = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    decision = _payload(
        _call(
            conn,
            "brain.proposal_decide",
            {
                "proposal_event_id": payload["proposal_event_id"],
                "decision": "reject",
                "actor": "human:jonathan",
                "reason": "asa1 is still the host",
            },
            profile="admin",
        )
    )

    assert "supersede_status" not in decision
    assert _serving(conn) == {old}
    assert _corrections(conn, "supersede") == []
    assert pending_supersede_count(conn) == 0
    # The rationale the agent wrote survives as curatable evidence even though
    # the correction was refused.
    assert conn.execute(
        "SELECT 1 FROM evidence_objects WHERE evidence_id=?",
        (payload["correction_evidence_id"],),
    ).fetchone()


def test_approving_a_supersession_of_an_already_retired_belief_reports_rather_than_lies(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OCBRAIN_SUPERSEDE_TIER", "pending_all")
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    payload = _supersede(conn, old, "The research VM is reached with ssh asa2.")
    _payload(
        _call(
            conn,
            "brain.correct",
            {"layer": "belief", "target": old, "op": "retract", "body": "withdrawn"},
            profile="admin",
        )
    )

    decision = _payload(
        _call(
            conn,
            "brain.proposal_decide",
            {
                "proposal_event_id": payload["proposal_event_id"],
                "decision": "approve",
                "actor": "human:jonathan",
            },
            profile="admin",
        )
    )

    assert decision["supersede_status"] == f"target already retired: {old}"
    assert _corrections(conn, "supersede") == []


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
def test_the_correction_event_records_who_and_which_connection(
    tmp_path: Path, monkeypatch
) -> None:
    """Corrections were the least attributable events in the ledger.

    Every one of 719 correction events in one real corpus carried a NULL session
    id and an author of the literal string "human". A correction is the most
    consequential thing anyone writes here; it has to say who wrote it.
    """
    monkeypatch.setenv("OCBRAIN_SESSION_ID", "session-abc123")
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")

    session_state: dict = {}
    handle_request(
        conn,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "0"},
            },
        },
        session_state=session_state,
    )
    _payload(
        _call(
            conn,
            "brain.supersede",
            {
                "target": old,
                "body": "The research VM is reached with ssh asa2.",
                "reason": "asa1 was terminated",
                "context": CONTEXT,
                "actor": "agent:claude-code",
            },
            session_state=session_state,
        )
    )

    row = conn.execute(
        "SELECT writer, session_id, body_json FROM brain_events "
        "WHERE kind='correction_recorded' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    body = json.loads(row["body_json"])
    assert row["writer"] == "agent:claude-code"
    assert row["session_id"] == "session-abc123"
    assert body["author"] == "agent:claude-code"
    assert body["provenance"]["client_session_hint"] == "session-abc123"
    assert body["provenance"]["client_session_hint_trust"] == "harness_attested"
    assert body["provenance"]["client_name"] == "claude-code"
    assert body["provenance"]["server_connection_id"]


def test_the_rate_cap_counts_one_session_not_the_whole_brain(
    tmp_path: Path, monkeypatch
) -> None:
    """The cap is per caller, so one busy agent cannot mute another."""
    monkeypatch.setenv("OCBRAIN_SUPERSEDE_DIRECT_CAP", "1")
    conn = _core(tmp_path)
    first = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    second = _seed(conn, belief_id="belief:job", body="The hourly job runs at :05.")
    third = _seed(conn, belief_id="belief:mab", body="MAB iterations run every hour.")

    monkeypatch.setenv("OCBRAIN_SESSION_ID", "session-one")
    state_one: dict = {}
    assert (
        _supersede(
            conn,
            first,
            "The research VM is reached with ssh asa2.",
            session_state=state_one,
        )["mode"]
        == "direct"
    )
    assert (
        _supersede(
            conn,
            second,
            "The hourly job runs at :20 since the July deploy.",
            session_state=state_one,
        )["mode"]
        == "pending"
    )

    monkeypatch.setenv("OCBRAIN_SESSION_ID", "session-two")
    state_two: dict = {}
    assert (
        _supersede(
            conn,
            third,
            "MAB iterations run every four hours since the July deploy.",
            session_state=state_two,
        )["mode"]
        == "direct"
    )


# --------------------------------------------------------------------------- #
# brain.get modes
# --------------------------------------------------------------------------- #
def test_get_resolves_a_superseded_id_forward(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    first = _supersede(conn, old, "The research VM is reached with ssh asa2.")
    second = _supersede(
        conn, first["successor_id"], "The research VM is reached with ssh asa3 since the move."
    )

    payload = _payload(_call(conn, "brain.get", {"id": old, "context": CONTEXT}))

    assert payload["mode"] == "resolve"
    assert payload["belief_id"] == second["successor_id"]
    assert payload["requested_id"] == old
    assert payload["resolved_from"] == [old, first["successor_id"]]
    assert payload["resolution_hops"] == 2
    assert "asa3" in payload["body"]


def test_get_as_stored_returns_the_retired_belief_labelled(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    result = _supersede(conn, old, "The research VM is reached with ssh asa2.")

    payload = _payload(
        _call(conn, "brain.get", {"id": old, "context": CONTEXT, "mode": "as_stored"})
    )

    assert payload["invalidated"] is True
    assert payload["belief_id"] == old
    assert "asa1" in payload["body"]
    assert payload["superseded_by"] == result["successor_id"]
    assert payload["valid_until"]


def test_get_still_refuses_a_retracted_belief_with_no_successor(tmp_path: Path) -> None:
    """Filtering invalidated facts by default is the point, not an oversight."""
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    _payload(
        _call(
            conn,
            "brain.correct",
            {"layer": "belief", "target": old, "op": "retract", "body": "withdrawn"},
            profile="admin",
        )
    )

    for mode in ("resolve", "as_stored"):
        response = _call(conn, "brain.get", {"id": old, "context": CONTEXT, "mode": mode})
        assert response["error"]["message"] == "non-current beliefs are not served by brain.get"


def test_get_refuses_a_supersession_cycle_instead_of_looping(tmp_path: Path) -> None:
    """A corrupted chain is a bounded refusal, never an unbounded walk."""
    conn = _core(tmp_path)
    left = _seed(conn, belief_id="belief:left", body="Assignments are sticky per session.")
    right = _seed(conn, belief_id="belief:right", body="Assignments are sticky per visitor.")
    # Point the two retracted beliefs at each other by hand: no legitimate path
    # produces this, which is exactly why the reader must survive it.
    for target, successor in ((left, right), (right, left)):
        append_core_event(
            conn,
            "correction_recorded",
            {
                "schema_version": "ocbrain.correction.v1",
                "target_layer": "belief",
                "target_id": target,
                "op": "supersede",
                "successor_id": successor,
                "author": "test",
                "hard": False,
            },
            writer="test",
            project=True,
        )
    conn.commit()

    response = _call(conn, "brain.get", {"id": left, "context": CONTEXT})
    assert "cycles back to" in response["error"]["message"]
    assert "mode=as_stored" in response["error"]["message"]


def test_get_rejects_an_unknown_mode(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    response = _call(conn, "brain.get", {"id": old, "context": CONTEXT, "mode": "whatever"})
    assert "mode must be one of" in response["error"]["message"]


# --------------------------------------------------------------------------- #
# Serve-time contradictions
# --------------------------------------------------------------------------- #
def test_context_flags_two_beliefs_claiming_the_same_key(tmp_path: Path) -> None:
    """The key is a fact's identity; two of them in one packet is two answers."""
    conn = _core(tmp_path)
    _seed(
        conn,
        belief_id="belief:one",
        body="The research VM is reached with ssh asa1 over the office VPN.",
        attributes={"key": "vm-access"},
    )
    _seed(
        conn,
        belief_id="belief:two",
        body="Research VM connections use the asa2 alias configured in ssh config.",
        attributes={"key": "vm-access"},
    )

    packet = _payload(
        _call(conn, "brain.context", {"query": "research VM ssh access", "context": CONTEXT})
    )

    duplicates = [
        conflict for conflict in packet["contradictions"] if conflict["reason"] == "duplicate_key"
    ]
    assert duplicates, packet["contradictions"]
    assert {duplicates[0]["belief_id"], duplicates[0]["other_belief_id"]} == {
        "belief:one",
        "belief:two",
    }
    assert duplicates[0]["advisory"] is True


def test_context_flags_a_near_duplicate_pair_from_the_vector_sidecar(tmp_path: Path) -> None:
    conn = _core(tmp_path)
    _seed(conn, belief_id="belief:one", body="The research VM is reached with ssh asa1.")
    _seed(conn, belief_id="belief:two", body="Reach the research VM by running ssh asa1.")
    _write_sidecar(
        tmp_path / "core-vectors.sqlite",
        {
            "belief:one": [1.0, 0.0, 0.0],
            # cos = 0.9578, above the 0.90 advisory threshold.
            "belief:two": [0.96, 0.28, 0.0],
        },
    )

    packet = _payload(
        _call(conn, "brain.context", {"query": "research VM ssh access", "context": CONTEXT})
    )

    pairs = [
        conflict
        for conflict in packet["contradictions"]
        if conflict["reason"] == "embedding_similarity"
    ]
    assert pairs, packet["contradictions"]
    assert {pairs[0]["belief_id"], pairs[0]["other_belief_id"]} == {"belief:one", "belief:two"}


def test_the_advisory_pass_stands_down_without_a_sidecar(tmp_path: Path) -> None:
    """An optional index must never be able to break a retrieval."""
    conn = _core(tmp_path)
    _seed(conn, belief_id="belief:one", body="The research VM is reached with ssh asa1.")
    _seed(conn, belief_id="belief:two", body="Reach the research VM by running ssh asa1.")
    assert not (tmp_path / "core-vectors.sqlite").exists()

    packet = _payload(
        _call(conn, "brain.context", {"query": "research VM ssh access", "context": CONTEXT})
    )

    assert packet["contradictions"] == []


def _write_sidecar(path: Path, vectors: dict[str, list[float]]) -> None:
    """A minimal read-compatible vector sidecar. No embedding endpoint involved."""
    sidecar = sqlite3.connect(path)
    sidecar.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE belief_vectors(
          belief_id TEXT PRIMARY KEY,
          content_hash TEXT NOT NULL,
          model TEXT NOT NULL,
          dimensions INTEGER NOT NULL,
          vector BLOB NOT NULL,
          scope_type TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          visibility TEXT NOT NULL,
          egress_policy TEXT NOT NULL,
          last_compiled_at TEXT NOT NULL
        );
        """
    )
    sidecar.execute(
        "INSERT INTO meta VALUES ('schema_version', 'ocbrain.vectors.v2')",
    )
    for belief_id, values in vectors.items():
        sidecar.execute(
            "INSERT INTO belief_vectors VALUES (?, '', 'test', ?, ?, 'project', "
            "'project:bountiful', 'internal', 'local_only', '2026-08-25T00:00:00+00:00')",
            (belief_id, len(values), array("f", values).tobytes()),
        )
    sidecar.commit()
    sidecar.close()


@pytest.mark.parametrize("missing", ["target", "body", "reason"])
def test_supersede_requires_its_three_arguments(tmp_path: Path, missing: str) -> None:
    conn = _core(tmp_path)
    old = _seed(conn, belief_id="belief:vm", body="The research VM is reached with ssh asa1.")
    arguments = {
        "target": old,
        "body": "The research VM is reached with ssh asa2.",
        "reason": "asa1 was terminated",
    }
    arguments.pop(missing)
    response = _call(conn, "brain.supersede", arguments)
    assert "error" in response
