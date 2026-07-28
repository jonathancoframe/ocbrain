"""Wiki freshness/supersession: frontmatter, index markers, and the lint script."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ocbrain.core_v1 import init_core_v1, record_core_v1_evidence
from ocbrain.db import connect
from ocbrain.scope import ScopeTag
from ocbrain.wiki import (
    materialize_wiki,
    page_staleness_markers,
    parse_page_frontmatter,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import importlib  # noqa: E402

wiki_lint = importlib.import_module("wiki-lint")

NOW = "2026-07-27T00:00:00Z"
RUN = {"at": NOW, "action": "curate", "model": "deterministic-local"}


def _add_wiki_belief(conn, belief_id, key, body, *, attributes=None, compiled_at=NOW):
    _evidence_id, event_id = record_core_v1_evidence(
        conn,
        body=f"evidence for {key}",
        kind="observation",
        scope=ScopeTag("project", "project:ocbrain"),
        writer="test",
    )
    conn.execute(
        """
        INSERT INTO current_beliefs(
          belief_id, body, belief_type, attributes_json,
          scope_type, scope_id, visibility, egress_policy,
          confidence, confidence_band, evidence_ids, status, serve,
          last_event_id, last_compiled_at
        ) VALUES (?, ?, 'wiki_fact', ?, 'project', 'project:ocbrain',
                  'internal', 'local_only', 0.9, 'high', '[]', 'current', 1, ?, ?)
        """,
        (belief_id, body, json.dumps(attributes or {"key": key}), event_id, compiled_at),
    )


def _materialize(tmp_path, beliefs):
    conn = connect(tmp_path / "wiki.sqlite")
    init_core_v1(conn)
    for belief in beliefs:
        _add_wiki_belief(conn, **belief)
    conn.commit()
    wiki_dir = tmp_path / "wiki"
    materialize_wiki(conn, wiki_dir, run=RUN)
    return conn, wiki_dir


def _page_text(wiki_dir, belief_id):
    suffix = belief_id.replace("belief-", "")[-10:]
    matches = list((wiki_dir / "pages").glob(f"*{suffix}*.md"))
    assert matches, f"no page for {belief_id}"
    return matches[0].name, matches[0].read_text(encoding="utf-8")


def test_fresh_page_has_valid_from_and_no_staleness_marker(tmp_path):
    conn, wiki_dir = _materialize(
        tmp_path,
        [
            {
                "belief_id": "belief-fresh0000000001",
                "key": "lake-location",
                "body": "The lake lives under the current data root.",
            }
        ],
    )
    conn.close()
    _name, text = _page_text(wiki_dir, "belief-fresh0000000001")
    frontmatter = parse_page_frontmatter(text)
    assert frontmatter["valid_from"] == NOW
    assert "valid_until" not in frontmatter
    assert "superseded_by" not in frontmatter
    assert "**Stale:**" not in text
    index = (wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "[stale:" not in index


def test_expired_and_superseded_pages_render_markers(tmp_path):
    conn, wiki_dir = _materialize(
        tmp_path,
        [
            {
                "belief_id": "belief-stale0000000001",
                "key": "retired-host",
                "body": "The old host serves the brain.",
                "attributes": {
                    "key": "retired-host",
                    "valid_until": "2026-07-01T00:00:00Z",
                },
            },
            {
                "belief_id": "belief-superseded00001",
                "key": "naming-convention",
                "body": "Use the old naming convention.",
                "attributes": {
                    "key": "naming-convention",
                    "superseded_by": "belief-fresh0000000001",
                },
            },
        ],
    )
    conn.close()

    _name, expired_text = _page_text(wiki_dir, "belief-stale0000000001")
    expired_fm = parse_page_frontmatter(expired_text)
    assert expired_fm["valid_until"] == "2026-07-01T00:00:00Z"
    assert "**Stale:**" in expired_text

    _name, superseded_text = _page_text(wiki_dir, "belief-superseded00001")
    superseded_fm = parse_page_frontmatter(superseded_text)
    assert superseded_fm["superseded_by"] == "belief-fresh0000000001"

    index = (wiki_dir / "index.md").read_text(encoding="utf-8")
    assert "**[stale: past valid_until 2026-07-01T00:00:00Z]**" in index
    assert "**[stale: superseded by belief-fresh0000000001]**" in index

    schema = (wiki_dir / "SCHEMA.md").read_text(encoding="utf-8")
    assert "valid_until" in schema
    assert "superseded_by" in schema


def test_page_staleness_markers_only_fire_when_derived():
    assert page_staleness_markers({"valid_until": "2026-07-28"}, now=NOW) == []
    assert page_staleness_markers({"valid_until": "2026-07-01"}, now=NOW) == [
        "past valid_until 2026-07-01"
    ]
    assert page_staleness_markers({"superseded_by": "belief-x"}, now=NOW) == [
        "superseded by belief-x"
    ]
    # Without a reference time, valid_until alone cannot mark staleness.
    assert page_staleness_markers({"valid_until": "2026-07-01"}, now="") == []


def test_wiki_lint_flags_expired_page_and_passes_fresh_wiki(tmp_path, capsys):
    conn, wiki_dir = _materialize(
        tmp_path,
        [
            {
                "belief_id": "belief-stale0000000001",
                "key": "retired-host",
                "body": "The old host serves the brain.",
                "attributes": {
                    "key": "retired-host",
                    "valid_until": "2026-07-01T00:00:00Z",
                },
            }
        ],
    )
    assert wiki_lint.main([str(wiki_dir), "--now", NOW]) == 1
    out = capsys.readouterr().out
    assert "expired" in out

    # Fresh wiki (no validity fields) lints clean against its own ledger.
    conn.close()
    conn2, fresh_dir = _materialize(
        tmp_path / "fresh",
        [
            {
                "belief_id": "belief-fresh0000000001",
                "key": "lake-location",
                "body": "The lake lives under the current data root.",
            }
        ],
    )
    assert wiki_lint.main([str(fresh_dir), "--now", NOW]) == 0
    conn2.close()


def test_wiki_lint_flags_ledger_drift_and_retired_beliefs(tmp_path, capsys):
    conn, wiki_dir = _materialize(
        tmp_path,
        [
            {
                "belief_id": "belief-drift0000000001",
                "key": "lake-location",
                "body": "The lake lives under the current data root.",
                "compiled_at": "2026-07-20T00:00:00Z",
            },
            {
                "belief_id": "belief-retired00000001",
                "key": "retired-host",
                "body": "The old host serves the brain.",
            },
        ],
    )
    # Ledger recompiled one belief after the page was built, and retired the
    # other; the materialized page now contradicts the ledger.
    conn.execute(
        "UPDATE current_beliefs SET last_compiled_at=? WHERE belief_id=?",
        ("2026-07-26T00:00:00Z", "belief-drift0000000001"),
    )
    conn.execute(
        "UPDATE current_beliefs SET status='stale', serve=0 WHERE belief_id=?",
        ("belief-retired00000001",),
    )
    conn.commit()
    db_path = tmp_path / "wiki.sqlite"
    conn.close()

    assert wiki_lint.main([str(wiki_dir), "--db", str(db_path), "--now", NOW]) == 1
    out = capsys.readouterr().out
    assert "ledger-newer-than-page" in out
    assert "not-current-in-ledger" in out


def test_wiki_lint_flags_conflicting_keys(tmp_path, capsys):
    conn, wiki_dir = _materialize(
        tmp_path,
        [
            {
                "belief_id": "belief-conflict-a000001",
                "key": "lake-location",
                "body": "The lake is at alpha.",
            },
            {
                "belief_id": "belief-conflict-b000001",
                "key": "lake-location",
                "body": "The lake is at beta.",
            },
        ],
    )
    conn.close()
    assert wiki_lint.main([str(wiki_dir), "--now", NOW]) == 1
    assert "conflicting-key" in capsys.readouterr().out
