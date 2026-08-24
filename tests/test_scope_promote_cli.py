"""`ocbrain scope-promote`: the missing emitter for `scope_promoted`.

The event kind, its projection, and its rebuild path all shipped; nothing wrote
one, which is why a live brain holds zero global beliefs and most retrievals
reach nothing. These tests hold down the three things a promotion must be: an
event that survives a full refold, an act with a named human behind it, and a
reach change that is never quietly an egress change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocbrain.cli import main
from ocbrain.core_v1 import (
    append_core_event,
    init_core_v1,
    project_core_v1,
    search_core_v1,
)
from ocbrain.db import connect
from ocbrain.mcp_v1 import decide_proposal_v1
from ocbrain.scope import ScopeContext, ScopeTag

WORKSPACE_PREFERENCE = "Never say 'load-bearing'; it is banned in every context."


def _run(capsys, db: Path, argv: list[str], *, expected: int = 0) -> dict:
    assert main(["--db", str(db), *argv]) == expected
    output = capsys.readouterr().out
    return json.loads(output) if output else {}


def _seed_wiki_fact(
    conn,
    *,
    belief_id: str,
    body: str,
    category: str,
    lifecycle: str = "durable",
    scope_id: str = "project:workspace",
    egress_policy: str = "local_only",
    visibility: str = "internal",
) -> None:
    proposal = append_core_event(
        conn,
        "compilation_proposed",
        {
            "belief_id": belief_id,
            "belief_type": "wiki_fact",
            "body": body,
            "evidence_ids": [],
            "scope": ScopeTag(
                "project",
                scope_id,
                visibility=visibility,
                egress_policy=egress_policy,
                provenance="wiki_curator",
            ).to_dict(),
            "confidence": 0.9,
            "attributes": {"category": category, "lifecycle": lifecycle, "key": belief_id},
        },
        writer="test",
    )
    decide_proposal_v1(
        conn,
        proposal_event_id=proposal,
        decision="approve",
        actor="test",
        edited_body=None,
        reason="scope promote fixture",
    )


def _seeded(tmp_path: Path) -> Path:
    db = tmp_path / "core.sqlite"
    conn = connect(db)
    init_core_v1(conn)
    _seed_wiki_fact(
        conn,
        belief_id="wiki:workspace:banned-word",
        body=WORKSPACE_PREFERENCE,
        category="preference",
    )
    _seed_wiki_fact(
        conn,
        belief_id="wiki:workspace:project-detail",
        body="The sandbox_sotu mart is rebuilt by a launchd job every morning.",
        category="project",
    )
    _seed_wiki_fact(
        conn,
        belief_id="wiki:workspace:current-state",
        body="The edge assign branch head is a moving target this week.",
        category="workflow",
        lifecycle="current",
    )
    conn.commit()
    conn.close()
    return db


def test_scope_promote_requires_approved_by(tmp_path: Path) -> None:
    db = _seeded(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["--db", str(db), "scope-promote", "--select-durable-preferences"])
    assert excinfo.value.code == 2


def test_scope_promote_selects_only_durable_preference_shaped_facts(
    tmp_path: Path, capsys
) -> None:
    db = _seeded(tmp_path)

    planned = _run(
        capsys,
        db,
        [
            "scope-promote",
            "--select-durable-preferences",
            "--approved-by",
            "human:jonathan",
            "--dry-run",
        ],
    )

    assert planned["status"] == "planned"
    assert [entry["belief_id"] for entry in planned["promoted"]] == [
        "wiki:workspace:banned-word"
    ]
    assert planned["promoted"][0]["to_scope"]["scope_id"] == "global:doctrine"
    # A dry run writes nothing.
    conn = connect(db)
    assert (
        conn.execute("SELECT COUNT(*) FROM brain_events WHERE kind='scope_promoted'").fetchone()[0]
        == 0
    )
    conn.close()


def test_scope_promote_event_survives_full_rebuild(tmp_path: Path, capsys) -> None:
    """The ledger is the authority: a refold must reproduce the promotion."""
    db = _seeded(tmp_path)

    applied = _run(
        capsys,
        db,
        ["scope-promote", "--select-durable-preferences", "--approved-by", "human:jonathan"],
    )
    assert applied["status"] == "applied"
    assert [entry["belief_id"] for entry in applied["promoted"]] == ["wiki:workspace:banned-word"]

    conn = connect(db)
    before = conn.execute(
        "SELECT scope_type, scope_id FROM current_beliefs WHERE belief_id=?",
        ("wiki:workspace:banned-word",),
    ).fetchone()
    assert (str(before["scope_type"]), str(before["scope_id"])) == ("global", "global:doctrine")

    project_core_v1(conn, full=True)
    after = conn.execute(
        "SELECT scope_type, scope_id FROM current_beliefs WHERE belief_id=?",
        ("wiki:workspace:banned-word",),
    ).fetchone()
    assert (str(after["scope_type"]), str(after["scope_id"])) == ("global", "global:doctrine")

    # And the point of the exercise: it is now reachable from a project that has
    # never heard of the workspace scope.
    result = search_core_v1(
        conn,
        "load-bearing banned word",
        context=ScopeContext(project="some-other-project"),
        limit=5,
    )
    assert "wiki:workspace:banned-word" in {item["belief_id"] for item in result["items"]}
    conn.close()


def test_scope_promote_never_widens_egress(tmp_path: Path, capsys) -> None:
    """Global reach is not hosted permission.

    A `local_only` belief promoted to `global:doctrine` becomes recallable from
    every project on this machine and is still refused for hosted delivery, which
    is exactly what `_delivery_sql('hosted_model')` enforces.
    """
    db = _seeded(tmp_path)
    _run(
        capsys,
        db,
        ["scope-promote", "--select-durable-preferences", "--approved-by", "human:jonathan"],
    )

    conn = connect(db)
    row = conn.execute(
        "SELECT visibility, egress_policy FROM current_beliefs WHERE belief_id=?",
        ("wiki:workspace:banned-word",),
    ).fetchone()
    assert str(row["visibility"]) == "internal"
    assert str(row["egress_policy"]) == "local_only"

    context = ScopeContext(project="some-other-project")
    local = search_core_v1(
        conn, "load-bearing banned word", context=context, limit=5, delivery_target="local_model"
    )
    hosted = search_core_v1(
        conn, "load-bearing banned word", context=context, limit=5, delivery_target="hosted_model"
    )
    assert {item["belief_id"] for item in local["items"]} == {"wiki:workspace:banned-word"}
    assert hosted["items"] == []
    conn.close()


def test_scope_promote_of_an_explicit_belief_carries_its_own_policy(
    tmp_path: Path, capsys
) -> None:
    db = _seeded(tmp_path)
    conn = connect(db)
    _seed_wiki_fact(
        conn,
        belief_id="wiki:workspace:shareable",
        body="OCBrain is Apache-2.0 and its config lives outside the checkout.",
        category="system",
        egress_policy="hosted_ok",
    )
    conn.commit()
    conn.close()

    applied = _run(
        capsys,
        db,
        [
            "scope-promote",
            "--belief-id",
            "wiki:workspace:shareable",
            "--to-scope-type",
            "global",
            "--to-scope-id",
            "global:doctrine",
            "--approved-by",
            "human:jonathan",
            "--reason",
            "project-independent project fact",
        ],
    )

    assert applied["promoted"][0]["to_scope"]["egress_policy"] == "hosted_ok"
    # Re-running is a no-op rather than a second event.
    again = _run(
        capsys,
        db,
        [
            "scope-promote",
            "--belief-id",
            "wiki:workspace:shareable",
            "--to-scope-type",
            "global",
            "--to-scope-id",
            "global:doctrine",
            "--approved-by",
            "human:jonathan",
        ],
    )
    assert again["promoted"] == []
    assert again["unchanged"] == ["wiki:workspace:shareable"]


def test_scope_promote_reports_unknown_beliefs_instead_of_inventing_them(
    tmp_path: Path, capsys
) -> None:
    db = _seeded(tmp_path)

    result = _run(
        capsys,
        db,
        [
            "scope-promote",
            "--belief-id",
            "wiki:workspace:does-not-exist",
            "--to-scope-type",
            "global",
            "--to-scope-id",
            "global:doctrine",
            "--approved-by",
            "human:jonathan",
        ],
    )

    assert result["missing"] == ["wiki:workspace:does-not-exist"]
    assert result["promoted"] == []


def test_scope_promote_requires_a_target_scope_without_the_selector(
    tmp_path: Path, capsys
) -> None:
    db = _seeded(tmp_path)

    result = _run(
        capsys,
        db,
        [
            "scope-promote",
            "--belief-id",
            "wiki:workspace:banned-word",
            "--approved-by",
            "human:jonathan",
        ],
        expected=2,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "target_scope_required"
