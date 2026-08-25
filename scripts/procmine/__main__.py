"""CLI: ``python -m procmine <stage>``.

    PYTHONPATH=src:scripts python -m procmine extract --out /tmp/traces.jsonl
    PYTHONPATH=src:scripts python -m procmine atlas \
        --traces /tmp/traces.jsonl \
        --report docs/PROCEDURE-ATLAS-20260824.md \
        --json docs/procedures.json
    PYTHONPATH=src:scripts python -m procmine mint --traces /tmp/traces.jsonl --apply

``extract`` is incremental by default: an unchanged source file is replayed from
its cached segment under ``~/.ocbrain/procmine/cache``. ``atlas`` always refreshes
the standing episodes artifact. Only ``mint --apply`` writes to the brain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .atlas import build, render_markdown, write_outputs
from .extract import PROCMINE_STATE_DIR, read_cache, write_cache

DEFAULT_EPISODES_PATH = PROCMINE_STATE_DIR / "episodes.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="procmine", description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)

    extract = sub.add_parser("extract", help="normalize every runtime's history into a cache")
    extract.add_argument("--out", type=Path, required=True)
    extract.add_argument("--source", action="append", dest="sources")
    extract.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help=f"where the fingerprint state and segments live (default {PROCMINE_STATE_DIR})",
    )
    extract.add_argument(
        "--no-incremental",
        action="store_true",
        help="reparse every source file and ignore the cached segments",
    )

    stats = sub.add_parser("stats", help="summarize an existing trace cache")
    stats.add_argument("--traces", type=Path, required=True)

    atlas = sub.add_parser("atlas", help="mine procedures and render the report")
    atlas.add_argument("--traces", type=Path, required=True)
    atlas.add_argument("--report", type=Path, required=True)
    atlas.add_argument("--json", dest="json_path", type=Path, required=True)
    atlas.add_argument("--brain-db", type=Path, default=None)
    atlas.add_argument(
        "--dump-episodes",
        type=Path,
        default=DEFAULT_EPISODES_PATH,
        help=f"standing per-episode sequence artifact (default {DEFAULT_EPISODES_PATH})",
    )

    mint = sub.add_parser("mint", help="turn mined gotchas into scoped beliefs")
    mint.add_argument("--traces", type=Path, required=True)
    mint.add_argument("--brain-db", type=Path, default=None)
    mint.add_argument("--limit", type=int, default=None)
    mint.add_argument("--apply", action="store_true", help="write; otherwise report only")

    args = parser.parse_args(argv)

    if args.stage == "extract":
        summary = write_cache(
            args.out,
            args.sources,
            state_dir=args.state_dir,
            incremental=not args.no_incremental,
        )
        print(json.dumps(summary, indent=2))
        return 0

    if args.stage == "stats":
        from .atlas import corpus_stats

        print(json.dumps(corpus_stats(read_cache(args.traces)), indent=2))
        return 0

    if args.stage == "mint":
        from .mint import mint_gotchas

        result = mint_gotchas(
            read_cache(args.traces),
            brain_db=args.brain_db,
            apply=args.apply,
            limit=args.limit,
        )
        print(json.dumps(result, indent=2))
        return 0

    data = build(args.traces, brain_db=args.brain_db)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_markdown(data))
    write_outputs(
        data,
        docs_dir=args.report.parent,
        json_path=args.json_path,
        episodes_path=args.dump_episodes,
    )
    print(f"wrote {args.report}, {args.json_path}, and {args.dump_episodes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
