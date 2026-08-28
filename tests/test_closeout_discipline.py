"""Write-time closeout discipline: session identity, runtime family, failure.

Every number asserted here was measured on a copy of the live core on
2026-08-28 (1,236 closeouts, 2026-07-15 to 2026-08-28) and is frozen into a
census fixture rather than recomputed, so a test failure means the rule changed
rather than that the corpus grew. The measurement scripts are in the PR
description; the counts are the contract.
"""

from __future__ import annotations

import collections
import sqlite3

import pytest

from ocbrain.closeout import (
    RUNTIME_FAMILIES,
    SERVER_CONNECTION_SESSION_PREFIX,
    SESSION_ID_SOURCES,
    _requires_unresolved,
    classify_session_id,
    record_closeout,
    resolve_session_identity,
    runtime_family,
)
from ocbrain.core_v1 import init_core_v1, migrate_core_v1_columns
from ocbrain.db import connect, init_db
from ocbrain.provenance import Provenance
from ocbrain.scope import ScopeContext

# --------------------------------------------------------------------------- #
# Frozen live-corpus censuses
# --------------------------------------------------------------------------- #

# `task_closeouts.session_id` on the live core, 2026-08-28: one representative
# literal per shape, with the number of rows that shape covers. The literals are
# real values the server accepted; none is invented.
LIVE_SESSION_CENSUS: tuple[tuple[str | None, str, int], ...] = (
    (None, "absent", 431),
    ("2026-07-22", "date_like", 296),
    ("fleet_cleanup_audit", "slug", 239),
    ("019f5dea-77b1-7693-a34c-23fead2ce442", "runtime_uuid", 208),
    ("2026-07-21 personalization headers", "contains_space", 35),
    ("/root/portfolio_receipt", "filesystem_path", 27),
)
LIVE_CLOSEOUTS = 1236
# Closeouts whose session id is byte-identical to a Claude Code transcript
# filename -- the join this whole gate exists to protect.
LIVE_TRANSCRIPT_JOINS = 91

# `task_closeouts.runtime` on the live core: the thirteen most-used spellings,
# 693 of 1,236 rows. Five spell "local mac", two spell "codex desktop", and one
# has an environment description welded onto the client name.
LIVE_RUNTIME_CENSUS: tuple[tuple[str, int], ...] = (
    ("codex-desktop", 171),
    ("mcp", 96),
    ("local", 95),
    ("codex", 92),
    ("claude-code", 78),
    ("desktop", 67),
    ("codex-desktop-heartbeat", 60),
    ("local-mac", 50),
    ("Codex desktop", 49),
    ("local macOS", 23),
    ("local-macos", 18),
    ("local macOS + readonlyprod ClickHouse", 13),
    ("macos", 12),
)
LIVE_RUNTIME_SPELLINGS = 159

# (status, verification_status, rows) over all 1,236 closeouts.
LIVE_STATUS_CENSUS: tuple[tuple[str, str, int], ...] = (
    ("completed", "verified", 888),
    ("partial", "verified", 116),
    ("completed", "failed", 94),
    ("completed", "agent_reported", 48),
    ("blocked", "failed", 35),
    ("completed", "agent_reported", 19),
    ("partial", "failed", 19),
    ("partial", "agent_reported", 10),
    ("partial", "agent_reported", 3),
    ("blocked", "verified", 3),
    ("failed", "failed", 1),
)


def _core(tmp_path):
    conn = connect(tmp_path / "core.sqlite")
    init_core_v1(conn)
    return conn


def _close(conn, **kwargs):
    payload = {
        "task_ref": "COFASC-292",
        "status": "completed",
        "summary": "Closed the receipt discipline defect at the write path.",
    }
    payload.update(kwargs)
    return record_closeout(conn, **payload)


# --------------------------------------------------------------------------- #
# Defect 1 -- session identity
# --------------------------------------------------------------------------- #


def test_the_shape_gate_reproduces_the_live_session_id_census():
    tally = collections.Counter()
    for value, expected, rows in LIVE_SESSION_CENSUS:
        assert classify_session_id(value) == expected, value
        tally[expected] += rows
    assert sum(tally.values()) == LIVE_CLOSEOUTS
    # 208 of 1,236 (16.8%) are runtime-shaped. The other 1,028 are absent or
    # hand-built, and 597 of those are a human typing something descriptive.
    assert tally["runtime_uuid"] == 208
    assert tally["absent"] == 431
    hand_written = sum(
        rows for shape, rows in tally.items() if shape not in {"absent", "runtime_uuid"}
    )
    assert hand_written == 597
    assert tally["runtime_uuid"] + tally["absent"] + hand_written == LIVE_CLOSEOUTS


def test_the_gate_admits_every_id_that_joins_a_transcript_and_no_others():
    """The refusal cannot cost a joinable row, because none of them is refused.

    All 91 closeouts that join a Claude Code transcript are ``runtime_uuid``;
    zero of the 597 hand-written ids join one. So admitting exactly the
    runtime-minted shapes keeps 91/91 and drops 0/91 -- the gate is not a
    trade-off between strictness and coverage, and that is why it is a shape
    question rather than a taste question.
    """
    joinable_shape = "runtime_uuid"
    admitted = [
        rows for value, shape, rows in LIVE_SESSION_CENSUS if classify_session_id(value) == shape
    ]
    assert sum(admitted) == LIVE_CLOSEOUTS
    by_shape = {shape: rows for _v, shape, rows in LIVE_SESSION_CENSUS}
    assert LIVE_TRANSCRIPT_JOINS <= by_shape[joinable_shape]
    for value, shape, _rows in LIVE_SESSION_CENSUS:
        if shape == joinable_shape:
            assert resolve_session_identity(value, Provenance())["session_id"] == value
        elif value is not None:
            with pytest.raises(ValueError):
                resolve_session_identity(value, Provenance())


@pytest.mark.parametrize(
    "value",
    [
        "telegram-jonathan-2026-08-25",
        "2026-07-21 personalization headers",
        "/root/portfolio_receipt",
        "fleet_cleanup_audit",
        "2026-07-22",
        "20260801_075651_56e87d0b",
        "current Codex thread",
    ],
)
def test_a_hand_written_session_id_is_refused_and_the_error_says_where_to_get_one(
    tmp_path, value
):
    """Every literal here is a value the live server actually accepted."""
    conn = _core(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        _close(conn, context=ScopeContext(project="ocbrain", session=value))
    message = str(excinfo.value)
    assert "CLAUDE_CODE_SESSION_ID" in message
    assert "OCBRAIN_SESSION_ID" in message
    assert "omit context.session" in message
    assert repr(value) in message


def test_omitting_the_session_is_legal_and_the_server_fills_it_from_its_own_connection(
    tmp_path,
):
    """The gate has to be satisfiable by a client that has no session id at all.

    Otherwise it is a gate that refuses work nobody can do differently, which is
    worse than the free-text column it replaces.
    """
    conn = _core(tmp_path)
    receipt = _close(
        conn,
        context=ScopeContext(project="ocbrain", runtime="hermes-cron"),
        provenance=Provenance(server_connection_id="cafe" * 8),
    )
    conn.commit()
    row = conn.execute(
        "SELECT session_id, session_id_source FROM task_closeouts WHERE id=?",
        (receipt["id"],),
    ).fetchone()
    assert row["session_id"] == f"{SERVER_CONNECTION_SESSION_PREFIX}{'cafe' * 8}"
    assert row["session_id_source"] == "server_connection"
    # Prefixed, so a later transcript join can never mistake it for one.
    assert not row["session_id"].startswith("cafe")


def test_a_caller_with_neither_a_session_nor_a_connection_still_files_a_closeout(tmp_path):
    conn = _core(tmp_path)
    receipt = _close(conn, context=ScopeContext(project="ocbrain"))
    conn.commit()
    row = conn.execute(
        "SELECT session_id, session_id_source FROM task_closeouts WHERE id=?",
        (receipt["id"],),
    ).fetchone()
    assert row["session_id"] is None
    assert row["session_id_source"] == "none"


def test_the_harness_attested_hint_outranks_the_model_and_the_disagreement_is_kept(
    tmp_path,
):
    conn = _core(tmp_path)
    observed = "3ebe3a24-6162-4af2-a4ee-4e8c1de121f7"
    claimed = "019f5dea-77b1-7693-a34c-23fead2ce442"
    receipt = _close(
        conn,
        context=ScopeContext(project="ocbrain", session=claimed),
        provenance=Provenance(
            server_connection_id="beef" * 8, client_session_hint=observed
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT session_id, session_id_source FROM task_closeouts WHERE id=?",
        (receipt["id"],),
    ).fetchone()
    assert row["session_id"] == observed
    assert row["session_id_source"] == "harness_attested"
    identity = receipt["provenance"]["session_identity"]
    assert identity["session_id_claim"] == claimed
    assert identity["session_id_conflict"] is True
    # The model's claim is still in the receipt under its historical key.
    assert receipt["provenance"]["session_id"] == claimed


def test_quarantine_keeps_the_claim_out_of_the_column_without_refusing(tmp_path):
    resolved = resolve_session_identity(
        "fleet_cleanup_audit",
        Provenance(server_connection_id="feed" * 8),
        policy="quarantine",
    )
    assert resolved["session_id"] == f"{SERVER_CONNECTION_SESSION_PREFIX}{'feed' * 8}"
    assert resolved["session_id_source"] == "server_connection"
    assert resolved["session_id_claim"] == "fleet_cleanup_audit"
    assert "session_id_conflict" not in resolved


def test_every_source_a_closeout_can_carry_is_a_declared_one():
    """``SESSION_ID_SOURCES`` is the documented vocabulary of that column.

    A constant nothing checks drifts away from the code, and then a consumer
    filtering on it silently drops rows. Every path through
    ``resolve_session_identity`` is exercised here.
    """
    uuid = "019f5dea-77b1-7693-a34c-23fead2ce442"
    other = "3ebe3a24-6162-4af2-a4ee-4e8c1de121f7"
    produced = {
        resolve_session_identity(None, Provenance())["session_id_source"],
        resolve_session_identity(uuid, Provenance())["session_id_source"],
        resolve_session_identity(
            None, Provenance(server_connection_id="ab" * 16)
        )["session_id_source"],
        resolve_session_identity(
            uuid, Provenance(client_session_hint=other)
        )["session_id_source"],
        resolve_session_identity("a-slug", Provenance(), policy="off")["session_id_source"],
        resolve_session_identity("a-slug", Provenance(), policy="quarantine")[
            "session_id_source"
        ],
    }
    assert produced == SESSION_ID_SOURCES


def test_policy_off_restores_the_pre_gate_behaviour_exactly(tmp_path):
    resolved = resolve_session_identity("fleet_cleanup_audit", Provenance(), policy="off")
    assert resolved["session_id"] == "fleet_cleanup_audit"
    assert resolved["session_id_source"] == "agent_reported"


# --------------------------------------------------------------------------- #
# Defect 2 -- runtime family
# --------------------------------------------------------------------------- #


def test_the_thirteen_top_runtime_spellings_collapse_to_three_families():
    tally = collections.Counter()
    for spelling, rows in LIVE_RUNTIME_CENSUS:
        tally[runtime_family(spelling)] += rows
    assert sum(tally.values()) == 824
    # Four spellings of "codex desktop" plus bare "codex" -- 372 rows, one family.
    assert tally["codex"] == 171 + 92 + 60 + 49
    assert tally["mcp"] == 96
    assert tally["claude-code"] == 78
    # "local", "desktop", "macOS" and friends name the machine, not the client.
    # 227 rows across six spellings, and `unknown` is the honest answer for all
    # of them: inventing a client here would be guessing.
    assert tally["unknown"] == 95 + 67 + 50 + 23 + 18 + 13 + 12
    assert set(tally) == {"codex", "mcp", "claude-code", "unknown"}
    assert LIVE_RUNTIME_SPELLINGS == 159


def test_a_normaliser_matching_substrings_invents_data():
    """Regression: "ClickHouse" contains "cli".

    Matching family tokens as substrings put the 13 live rows spelled
    'local macOS + readonlyprod ClickHouse' in the `cli` family, and 16 more
    besides. Segment matching is what stops a normaliser being confidently
    wrong about a third of a family.
    """
    assert runtime_family("local macOS + readonlyprod ClickHouse") == "unknown"
    assert runtime_family("local Mac; Dagster localhost; readonlyprod lake") == "cli"
    assert runtime_family("gcloud-cli") == "cli"
    # Path- and profile-separated spellings still resolve, which is what the
    # wider separator set buys.
    assert runtime_family("hermes@f15a38ee73631b3cd5f7d30765c37d5f0245d403") == "hermes"
    assert runtime_family("~/.local/share/hermes-runtimes/f15a38ee") == "hermes"
    assert runtime_family("hermes:squirtlecoframe") == "hermes"


def test_the_server_observed_key_outranks_the_model_and_detail_gets_its_own_field(
    tmp_path,
):
    """The value that smuggled an environment into the client name, fixed.

    'local macOS + readonlyprod ClickHouse' appeared 13 times because there was
    nowhere else to put the second half.
    """
    conn = _core(tmp_path)
    receipt = _close(
        conn,
        context=ScopeContext(project="ocbrain", runtime="local macOS"),
        runtime_detail="readonlyprod ClickHouse",
        provenance=Provenance(
            server_connection_id="0" * 32, client_runtime_key="hermes:squirtlecoframe"
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT runtime, runtime_family FROM task_closeouts WHERE id=?",
        (receipt["id"],),
    ).fetchone()
    # The model said "local macOS"; the process saw Hermes. The observed one wins.
    assert row["runtime_family"] == "hermes"
    assert row["runtime"] == "local macOS"
    assert receipt["provenance"]["runtime_detail"] == "readonlyprod ClickHouse"


def test_an_unrecognised_server_key_falls_through_to_the_model_rather_than_unknown():
    """64 live rows carry a client key the shipped rules do not know.

    Their model claim does say `claude-code`. Falling through preserves that
    rather than throwing it away for the sake of precedence.
    """
    assert runtime_family("local-agent-mode-ocbrain", "claude-code") == "claude-code"
    assert runtime_family("local-agent-mode-ocbrain", "local") == "unknown"


def test_an_operator_alias_can_name_an_install_specific_label():
    aliases = {"local-agent-mode-ocbrain": "claude-code", "f15a38ee": "hermes"}
    assert runtime_family("local-agent-mode-ocbrain", aliases=aliases) == "claude-code"
    assert runtime_family("f15a38ee", aliases=aliases) == "hermes"
    # An alias may not invent an eighth family.
    assert runtime_family("whatever", aliases={"whatever": "teapot"}) == "unknown"
    assert "teapot" not in RUNTIME_FAMILIES


def test_runtime_family_is_pure_so_history_stays_analysable():
    """``task_closeouts`` is append-only; the 159 historical spellings can never
    be rewritten in place. A pure function is what keeps them groupable."""
    for spelling, _rows in LIVE_RUNTIME_CENSUS:
        assert runtime_family(spelling) == runtime_family(spelling)
        assert runtime_family(spelling) in RUNTIME_FAMILIES


# --------------------------------------------------------------------------- #
# Defect 3 -- failure reporting
# --------------------------------------------------------------------------- #


def test_the_unresolved_gate_catches_281_of_the_1236_live_closeouts():
    """281 rows (22.7%) carry evidence something did not work and no field for it.

    94 of those claim `completed`. Gating on `status` alone would have missed
    every one of them, which is the whole reason the verifier evidence is a
    second, independent trigger.
    """
    assert sum(rows for _s, _v, rows in LIVE_STATUS_CENSUS) == LIVE_CLOSEOUTS
    caught = sum(
        rows for status, verification, rows in LIVE_STATUS_CENSUS
        if _requires_unresolved(status, verification)
    )
    assert caught == 281
    by_status_alone = sum(
        rows for status, _v, rows in LIVE_STATUS_CENSUS if status != "completed"
    )
    assert by_status_alone == 187
    assert caught - by_status_alone == 94
    clean = sum(
        rows for status, verification, rows in LIVE_STATUS_CENSUS
        if not _requires_unresolved(status, verification)
    )
    assert clean == LIVE_CLOSEOUTS - 281 == 955


def test_a_completed_closeout_with_a_failed_verifier_must_say_what_failed(tmp_path):
    conn = _core(tmp_path)
    verifiers = [
        {"uri": "repo://ocbrain/pytest", "status": "passed"},
        {"uri": "repo://ocbrain/ruff", "status": "failed"},
    ]
    with pytest.raises(ValueError, match="unresolved is required"):
        _close(conn, status="completed", verifier_refs=verifiers)
    receipt = _close(
        conn,
        status="completed",
        verifier_refs=verifiers,
        unresolved="ruff still reports two E501s in closeout.py.",
    )
    conn.commit()
    row = conn.execute(
        "SELECT unresolved FROM task_closeouts WHERE id=?", (receipt["id"],)
    ).fetchone()
    assert row["unresolved"] == "ruff still reports two E501s in closeout.py."
    assert receipt["unresolved"] == "ruff still reports two E501s in closeout.py."


def test_both_gates_report_together_so_one_retry_fixes_both(tmp_path):
    """A caller with two problems learns both at once.

    Refusing one at a time costs an unattended agent two retries for one
    closeout, and the second refusal arrives only after it has already
    rewritten something.
    """
    conn = _core(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        _close(
            conn,
            status="partial",
            context=ScopeContext(project="ocbrain", session="fleet_cleanup_audit"),
        )
    message = str(excinfo.value)
    assert "CLAUDE_CODE_SESSION_ID" in message
    assert "unresolved is required" in message


def test_a_clean_success_is_not_asked_for_an_explanation(tmp_path):
    conn = _core(tmp_path)
    receipt = _close(
        conn, verifier_refs=[{"uri": "repo://ocbrain/pytest", "status": "passed"}]
    )
    conn.commit()
    assert receipt["unresolved"] is None
    # And a `completed` with no verifiers at all is still clean: the ledger
    # already reports that as `in_flight` rather than done, and charging it for
    # an explanation would tax the 19 live rows that simply had nothing to run.
    assert _close(conn, task_ref="no-verifier")["unresolved"] is None


def test_an_audit_whose_verifiers_all_failed_may_still_be_completed(tmp_path):
    """Do not derive the status from the evidence. Seven live closeouts claim
    `completed` with every verifier failed, and all seven are read-only audits
    where the FAIL verdict IS the deliverable -- "Read-only re-review found
    remaining blockers; verdict FAIL". Relabelling those `failed` would call
    successful work a failure. The caller keeps the verdict and owes a sentence.
    """
    conn = _core(tmp_path)
    receipt = _close(
        conn,
        status="completed",
        summary="Read-only re-review found remaining blockers; verdict FAIL.",
        verifier_refs=[
            {"uri": "audit://autoresearch/lineage", "status": "failed"},
            {"uri": "audit://autoresearch/capacity", "status": "failed"},
        ],
        unresolved="The four blocking defects are reported, not fixed; nobody owns them yet.",
    )
    conn.commit()
    assert receipt["status"] == "completed"
    assert receipt["verification_status"] == "failed"


def test_every_non_completion_status_owes_an_explanation(tmp_path):
    conn = _core(tmp_path)
    for status in ("partial", "failed", "cancelled"):
        with pytest.raises(ValueError, match="unresolved is required"):
            _close(conn, task_ref=f"t-{status}", status=status)
    # `blocked` already required `awaiting`; it now owes both, because "what
    # unblocks me" and "what did not work" are different sentences.
    with pytest.raises(ValueError, match="unresolved is required"):
        _close(conn, task_ref="t-blocked", status="blocked", awaiting="a human")
    receipt = _close(
        conn,
        task_ref="t-blocked",
        status="blocked",
        awaiting="Jonathan to approve the readonlyprod credential",
        unresolved="The source refresh has never run against production.",
    )
    assert receipt["awaiting"] != receipt["unresolved"]


def test_an_existing_core_gains_the_columns_before_the_first_closeout_lands(tmp_path):
    """The live core is 208 MB and predates all three columns.

    ``CREATE TABLE IF NOT EXISTS`` means a fresh core gets them from the schema
    and proves nothing about an existing one. The write path names all three, so
    without the additive migration the first ``brain.closeout`` after deploy
    fails on an unknown column -- and a fresh-core test cannot see that.
    """
    path = tmp_path / "legacy-core.sqlite"
    conn = connect(path)
    init_core_v1(conn)
    for column in ("session_id_source", "runtime_family", "unresolved"):
        conn.execute(f"ALTER TABLE task_closeouts DROP COLUMN {column}")
    conn.commit()
    present = {row[1] for row in conn.execute("PRAGMA table_info(task_closeouts)")}
    assert not present & {"session_id_source", "runtime_family", "unresolved"}
    with pytest.raises(sqlite3.OperationalError):
        _close(conn, task_ref="before-migration")

    assert migrate_core_v1_columns(conn)
    conn.commit()
    receipt = _close(conn, task_ref="after-migration")
    conn.commit()
    row = conn.execute(
        "SELECT session_id_source, runtime_family, unresolved FROM task_closeouts "
        "WHERE id=?",
        (receipt["id"],),
    ).fetchone()
    assert row["session_id_source"] == "none"
    assert row["runtime_family"] == "unknown"
    assert row["unresolved"] is None


def test_the_legacy_initializer_also_migrates_an_existing_database(tmp_path):
    """``db.init_db`` carries its own copy of the schema and its own migration
    list. Both have to add the columns, or a legacy store is broken by a deploy
    the v1 core survived."""
    conn = connect(tmp_path / "legacy.sqlite")
    init_db(conn)
    for column in ("session_id_source", "runtime_family", "unresolved"):
        conn.execute(f"ALTER TABLE task_closeouts DROP COLUMN {column}")
    conn.commit()
    with pytest.raises(sqlite3.OperationalError):
        _close(conn, task_ref="before-migration")

    init_db(conn)
    conn.commit()
    receipt = _close(conn, task_ref="after-migration")
    conn.commit()
    assert conn.execute(
        "SELECT runtime_family FROM task_closeouts WHERE id=?", (receipt["id"],)
    ).fetchone()["runtime_family"] == "unknown"


def test_a_misspelled_policy_falls_back_instead_of_taking_the_write_path_down(
    tmp_path, monkeypatch
):
    """A typo in a config file must not refuse every closeout on the install."""
    monkeypatch.setenv("OCBRAIN_CLOSEOUT_SESSION_ID_POLICY", "enfroce")
    conn = _core(tmp_path)
    receipt = _close(conn, context=ScopeContext(project="ocbrain"))
    conn.commit()
    assert receipt["provenance"]["session_identity"]["session_id_source"] == "none"
    # Still the shipped default, not "anything goes".
    with pytest.raises(ValueError, match="CLAUDE_CODE_SESSION_ID"):
        _close(
            conn,
            task_ref="typo-policy",
            context=ScopeContext(project="ocbrain", session="a-slug"),
        )


def test_the_gates_are_configurable_and_off_reproduces_the_old_behaviour(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OCBRAIN_CLOSEOUT_SESSION_ID_POLICY", "off")
    monkeypatch.setenv("OCBRAIN_CLOSEOUT_REQUIRE_UNRESOLVED", "false")
    conn = _core(tmp_path)
    receipt = _close(
        conn,
        status="partial",
        context=ScopeContext(project="ocbrain", session="fleet_cleanup_audit"),
    )
    conn.commit()
    row = conn.execute(
        "SELECT session_id, unresolved FROM task_closeouts WHERE id=?", (receipt["id"],)
    ).fetchone()
    assert row["session_id"] == "fleet_cleanup_audit"
    assert row["unresolved"] is None
