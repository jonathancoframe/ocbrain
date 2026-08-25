"""Atomic human-readable materialization of current wiki beliefs."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def current_wiki_beliefs(
    conn,
    *,
    project: str | None = None,
    hosted_egress: bool = False,
    allow_approval_required: bool = False,
) -> list[dict[str, Any]]:
    """Return current wiki facts, optionally restricted for hosted delivery.

    Local materialization intentionally uses the unfiltered default. Hosted
    callers must set ``hosted_egress`` so project, visibility, and egress gates
    are applied before any belief body leaves SQLite. Explicit approval can add
    ``approval_required`` facts; it never admits ``local_only``, ``prohibited``,
    ``confidential``, or ``secret`` facts.
    """
    if allow_approval_required and not hosted_egress:
        raise ValueError("allow_approval_required requires hosted_egress")
    if hosted_egress and not project:
        raise ValueError("project is required for hosted wiki selection")
    filters = [
        "status='current'",
        "serve=1",
        "belief_type='wiki_fact'",
    ]
    params: list[str] = []
    if project is not None:
        filters.extend(("scope_type='project'", "scope_id=?"))
        params.append(f"project:{project}")
    if hosted_egress:
        filters.append("visibility='internal'")
        if allow_approval_required:
            filters.append("egress_policy IN ('hosted_ok', 'approval_required')")
        else:
            filters.append("egress_policy='hosted_ok'")
    where_clause = " AND ".join(filters)
    beliefs: list[dict[str, Any]] = []
    for row in conn.execute(
        f"""
        SELECT belief_id, body, attributes_json, confidence, evidence_ids,
               last_compiled_at, scope_type, scope_id, visibility, egress_policy
        FROM current_beliefs
        WHERE {where_clause}
        ORDER BY scope_id, belief_id
        """,
        params,
    ):
        attributes = json.loads(row["attributes_json"] or "{}")
        beliefs.append(
            {
                "belief_id": str(row["belief_id"]),
                "key": attributes.get("key"),
                "title": attributes.get("title"),
                "body": str(row["body"]),
                "category": attributes.get("category"),
                "confidence": row["confidence"],
                "evidence_ids": json.loads(row["evidence_ids"] or "[]"),
                "updated_at": str(row["last_compiled_at"]),
                "scope_type": str(row["scope_type"]),
                "scope_id": str(row["scope_id"]),
                "visibility": str(row["visibility"]),
                "egress_policy": str(row["egress_policy"]),
                "attributes": attributes,
            }
        )
    return beliefs


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "belief"


def parse_page_frontmatter(text: str) -> dict[str, str]:
    """Parse the simple ``key: value`` frontmatter materialized wiki pages use."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        fields[key.strip()] = value
    return fields


def page_staleness_markers(
    frontmatter: dict[str, Any],
    *,
    now: str,
) -> list[str]:
    """Return human-readable staleness markers derivable from frontmatter.

    ``valid_until`` and ``now`` are normalized to UTC before comparison so
    equivalent ISO-8601 timestamps with different offsets cannot invert the
    result. No wall-clock or ledger access happens here.
    """
    markers: list[str] = []
    superseded_by = str(frontmatter.get("superseded_by") or "").strip()
    if superseded_by:
        markers.append(f"superseded by {superseded_by}")
    valid_until = str(frontmatter.get("valid_until") or "").strip()
    valid_until_at = _parse_iso_timestamp(valid_until)
    now_at = _parse_iso_timestamp(now)
    if valid_until_at is not None and now_at is not None and valid_until_at < now_at:
        markers.append(f"past valid_until {valid_until}")
    return markers


def _parse_iso_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def materialize_wiki(conn, wiki_dir: Path, *, run: dict[str, Any]) -> int:
    """Rebuild the disposable wiki directory and swap it into place atomically."""
    wiki_dir = wiki_dir.expanduser().resolve()
    wiki_dir.parent.mkdir(parents=True, exist_ok=True)
    effective_run = dict(run)
    # A non-curator rebuild (for example after hygiene) must not erase the digest
    # that makes the next unchanged curator cycle a no-op. Preserve only that
    # cursor; the rest of state.json should continue to describe this run.
    # ``projects`` is the multi-project form of the same cursor, one digest per
    # curated scope; ``input_digest`` is the flat pre-multi-project shape.
    if not ({"input_digest", "projects"} & set(effective_run)):
        prior_state_path = wiki_dir / "state.json"
        try:
            prior_state = json.loads(prior_state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            prior_state = {}
        if not isinstance(prior_state, dict):
            prior_state = {}
        prior_digest = prior_state.get("input_digest")
        if isinstance(prior_digest, str) and prior_digest:
            effective_run["input_digest"] = prior_digest
        prior_projects = prior_state.get("projects")
        if isinstance(prior_projects, dict) and prior_projects:
            effective_run["projects"] = prior_projects
    temp_dir = Path(
        tempfile.mkdtemp(prefix=f".{wiki_dir.name}-build-", dir=wiki_dir.parent)
    )
    backup_dir = wiki_dir.parent / f".{wiki_dir.name}-previous"
    try:
        count = _build_wiki(conn, temp_dir, previous=wiki_dir, run=effective_run)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        moved_previous = False
        try:
            if wiki_dir.exists():
                os.replace(wiki_dir, backup_dir)
                moved_previous = True
            os.replace(temp_dir, wiki_dir)
        except Exception:
            if moved_previous and not wiki_dir.exists() and backup_dir.exists():
                os.replace(backup_dir, wiki_dir)
            raise
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        return count
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def _build_wiki(
    conn,
    target: Path,
    *,
    previous: Path,
    run: dict[str, Any],
) -> int:
    os.chmod(target, 0o700)
    pages_dir = target / "pages"
    pages_dir.mkdir()
    beliefs = current_wiki_beliefs(conn)
    now = str(run.get("at") or "")
    index_lines = [
        "# OCBrain current-truth wiki",
        "",
        "Sparse, source-linked beliefs compiled from the append-only OCBrain ledger.",
        "",
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    page_names: dict[str, str] = {}
    page_markers: dict[str, list[str]] = {}
    for belief in beliefs:
        category = str(belief.get("category") or "system")
        grouped.setdefault(category, []).append(belief)
        key = str(belief.get("key") or belief["belief_id"])
        page_name = (
            f"{safe_slug(key)}-{safe_slug(str(belief['belief_id']))[-10:]}.md"
        )
        page_names[str(belief["belief_id"])] = page_name
        title = str(belief.get("title") or key.replace("-", " ").title())
        attributes = belief.get("attributes") or {}
        caveat = str(attributes.get("uncertainty") or "").strip()
        valid_until = str(attributes.get("valid_until") or "").strip()
        superseded_by = str(attributes.get("superseded_by") or "").strip()
        frontmatter: dict[str, Any] = {
            "valid_from": str(belief.get("updated_at") or ""),
            "valid_until": valid_until,
            "superseded_by": superseded_by,
        }
        markers = page_staleness_markers(frontmatter, now=now)
        page_markers[str(belief["belief_id"])] = markers
        page_lines = [
            "---",
            f'id: "{belief["belief_id"]}"',
            f'key: "{key}"',
            f'category: "{category}"',
            f'scope: "{belief["scope_id"]}"',
            f"confidence: {belief.get('confidence')}",
            f'updated_at: "{belief.get("updated_at")}"',
            f'valid_from: "{frontmatter["valid_from"]}"',
            "status: current",
        ]
        if valid_until:
            page_lines.append(f'valid_until: "{valid_until}"')
        if superseded_by:
            page_lines.append(f'superseded_by: "{superseded_by}"')
        page_lines.extend(
            (
                "---",
                "",
                f"# {title}",
                "",
            )
        )
        if markers:
            page_lines.extend(
                (
                    f"> **Stale:** {'; '.join(markers)}.",
                    "",
                )
            )
        page_lines.extend((str(belief["body"]), ""))
        if caveat:
            page_lines.extend(("## Caveat", "", caveat, ""))
        page_lines.extend(
            (
                "## Sources",
                "",
                *[f"- `ocbrain://evidence/{item}`" for item in belief["evidence_ids"]],
                "",
            )
        )
        _write(target / "pages" / page_name, "\n".join(page_lines))

    for category in sorted(grouped):
        index_lines.extend((f"## {category.title()}", ""))
        for belief in sorted(
            grouped[category],
            key=lambda item: (str(item["scope_id"]), str(item.get("title") or "")),
        ):
            key = str(belief.get("key") or belief["belief_id"])
            title = str(belief.get("title") or key.replace("-", " ").title())
            page_name = page_names[str(belief["belief_id"])]
            markers = page_markers.get(str(belief["belief_id"])) or []
            staleness = f" **[stale: {'; '.join(markers)}]**" if markers else ""
            index_lines.append(
                f"- [{title}](pages/{page_name}) — {belief['body']} "
                f"`{belief['scope_id']}`{staleness}"
            )
        index_lines.append("")
    _write(target / "index.md", "\n".join(index_lines))
    _write(
        target / "SCHEMA.md",
        """# OCBrain wiki schema

The SQLite event ledger is the immutable source layer. Files in `pages/` are an
atomic, human-readable materialization of current `wiki_fact` beliefs.

Only concise, project-scoped, source-linked claims may enter the wiki. A sealed
mission closeout must be hash-verified and verifier-backed. Kimi may edit
explicitly selected candidates, but it is never evidence or authority. Raw
transcripts, routine progress, tool output, health chatter, and unsupported
inferences remain outside current truth.

## Freshness and supersession

Pages carry lightweight freshness frontmatter so readers can tell current
truth from stale belief without querying the ledger:

- `valid_from` — when the belief was last compiled (derived from
  `last_compiled_at`).
- `valid_until` — optional; the claim should not be trusted after this
  ISO-8601 time.
- `superseded_by` — optional; the belief id of the claim that replaces this
  one.

`index.md` renders a `**[stale: ...]**` marker next to any page whose
`valid_until` has passed or that names a `superseded_by` successor, and the
page itself carries a `> **Stale:**` notice under its title.
`scripts/wiki-lint.py` flags expired/superseded pages, pages the ledger no
longer serves as current, pages older than the ledger's latest compilation,
and conflicting pages that share a key.
""",
    )
    prior_log = ""
    prior_log_path = previous / "log.md"
    if prior_log_path.is_file():
        prior_log = prior_log_path.read_text(encoding="utf-8", errors="replace")
    if not prior_log:
        prior_log = "# OCBrain wiki log\n\n"
    log_entry = (
        f"## [{run.get('at')}] {run.get('action', 'curate')} | "
        f"{run.get('model', 'deterministic-local')}\n\n"
        f"- Evidence considered: {run.get('evidence_count', 0)}\n"
        f"- Claims accepted: {run.get('accepted_count', 0)}\n"
        f"- Claims applied: {run.get('applied_count', 0)}\n"
        f"- Input digest: `{run.get('input_digest', '')}`\n\n"
    )
    _write(target / "log.md", prior_log.rstrip() + "\n\n" + log_entry)
    _write(target / "state.json", json.dumps(run, indent=2, sort_keys=True) + "\n")
    return len(beliefs)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)


__all__ = [
    "current_wiki_beliefs",
    "materialize_wiki",
    "page_staleness_markers",
    "parse_page_frontmatter",
    "safe_slug",
]
