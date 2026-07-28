#!/usr/bin/env python3
"""Flag stale, expired, superseded, or contradicted pages in an OCBrain wiki.

The materialized wiki (``index.md`` + ``pages/``) is a disposable view of
current ``wiki_fact`` beliefs; this linter is the curation pass that keeps it
honest. It reports, per page:

- ``expired`` — ``valid_until`` is in the past (see ``--now``);
- ``superseded`` — the page names a ``superseded_by`` successor but is still
  rendered as current truth;
- ``not-current-in-ledger`` — with ``--db``: the belief is missing, tombstoned,
  or no longer served in the ledger the wiki was materialized from;
- ``ledger-newer-than-page`` — with ``--db``: the ledger recompiled the belief
  after the page's ``updated_at``, so the page serves an older body;
- ``conflicting-key`` — two current pages share a ``key`` with different
  bodies, i.e. one of them is an unmarked supersession.

Exit code is 1 when any finding is reported, 0 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from ocbrain.core_v1 import get_core_v1_belief, is_core_v1
from ocbrain.db import connect
from ocbrain.wiki import page_staleness_markers, parse_page_frontmatter


def _read_body(text: str) -> str:
    """Page body without frontmatter, used only for conflict comparison."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[index + 1 :]).strip()
    return text.strip()


def lint_wiki(
    wiki_dir: Path,
    *,
    now: str,
    db_path: Path | None = None,
) -> list[str]:
    wiki_dir = wiki_dir.expanduser().resolve()
    pages_dir = wiki_dir / "pages"
    findings: list[str] = []
    if not pages_dir.is_dir():
        return [f"{wiki_dir}: no pages/ directory — not a materialized wiki"]

    conn = None
    if db_path is not None:
        conn = connect(db_path.expanduser().resolve())
        if not is_core_v1(conn):
            findings.append(f"{db_path}: not an OCBrain v1 core; ledger checks skipped")
            conn.close()
            conn = None

    pages: dict[str, dict[str, str]] = {}
    bodies: dict[str, str] = {}
    try:
        for page in sorted(pages_dir.glob("*.md")):
            text = page.read_text(encoding="utf-8", errors="replace")
            frontmatter = parse_page_frontmatter(text)
            if not frontmatter.get("id"):
                findings.append(f"{page.name}: missing frontmatter id")
                continue
            pages[page.name] = frontmatter
            bodies[page.name] = _read_body(text)

            for marker in page_staleness_markers(frontmatter, now=now):
                kind = "superseded" if marker.startswith("superseded by") else "expired"
                findings.append(f"{page.name}: {kind} ({marker})")

            if conn is not None:
                belief = get_core_v1_belief(conn, str(frontmatter["id"]))
                if (
                    belief is None
                    or belief.get("status") != "current"
                    or not belief.get("serve")
                ):
                    findings.append(f"{page.name}: not-current-in-ledger")
                elif str(belief.get("last_compiled_at") or "") > str(
                    frontmatter.get("updated_at") or ""
                ):
                    findings.append(
                        f"{page.name}: ledger-newer-than-page "
                        f"(ledger {belief['last_compiled_at']} > page "
                        f"{frontmatter.get('updated_at')})"
                    )
    finally:
        if conn is not None:
            conn.close()

    by_key: dict[str, list[str]] = {}
    for page_name, frontmatter in pages.items():
        key = str(frontmatter.get("key") or "").strip()
        if key:
            by_key.setdefault(key, []).append(page_name)
    for key, names in sorted(by_key.items()):
        if len(names) < 2:
            continue
        distinct = {bodies[name] for name in names}
        if len(distinct) > 1:
            findings.append(
                f"key {key!r}: conflicting-key across {', '.join(sorted(names))}"
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("wiki_dir", type=Path, help="materialized wiki directory")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="optional v1 ledger path for ledger-vs-page checks",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="ISO-8601 reference time for valid_until checks (default: now, UTC)",
    )
    args = parser.parse_args(argv)
    now = args.now or datetime.now(UTC).isoformat()
    findings = lint_wiki(args.wiki_dir, now=now, db_path=args.db)
    for finding in findings:
        print(finding)
    if findings:
        print(f"wiki-lint: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("wiki-lint: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
