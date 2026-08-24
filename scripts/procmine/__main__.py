"""CLI: ``python -m procmine <stage>``.

    PYTHONPATH=src:scripts python -m procmine extract --out /tmp/traces.jsonl
    PYTHONPATH=src:scripts python -m procmine atlas \
        --traces /tmp/traces.jsonl \
        --report docs/PROCEDURE-ATLAS-20260824.md \
        --json docs/procedures.json

Reads live stores read-only and writes nothing back to the brain.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .atlas import build, render_markdown, write_outputs
from .extract import read_cache, write_cache


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="procmine", description=__doc__)
    sub = parser.add_subparsers(dest="stage", required=True)

    extract = sub.add_parser("extract", help="normalize every runtime's history into a cache")
    extract.add_argument("--out", type=Path, required=True)
    extract.add_argument("--source", action="append", dest="sources")

    stats = sub.add_parser("stats", help="summarize an existing trace cache")
    stats.add_argument("--traces", type=Path, required=True)

    atlas = sub.add_parser("atlas", help="mine procedures and render the report")
    atlas.add_argument("--traces", type=Path, required=True)
    atlas.add_argument("--report", type=Path, required=True)
    atlas.add_argument("--json", dest="json_path", type=Path, required=True)
    atlas.add_argument("--brain-db", type=Path, default=None)
    atlas.add_argument("--dump-episodes", type=Path, default=None)

    args = parser.parse_args(argv)

    if args.stage == "extract":
        summary = write_cache(args.out, args.sources)
        print(json.dumps(summary, indent=2))
        return 0

    if args.stage == "stats":
        from .atlas import corpus_stats

        print(json.dumps(corpus_stats(read_cache(args.traces)), indent=2))
        return 0

    data = build(args.traces, brain_db=args.brain_db)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_markdown(data))
    write_outputs(data, docs_dir=args.report.parent, json_path=args.json_path)
    if args.dump_episodes:
        args.dump_episodes.parent.mkdir(parents=True, exist_ok=True)
        args.dump_episodes.write_text(json.dumps(data["episodes"], indent=2) + "\n")
    print(f"wrote {args.report} and {args.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
