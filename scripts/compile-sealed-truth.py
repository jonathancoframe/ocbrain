#!/usr/bin/env python3
"""Compile one Agent Control sealed release into sparse OCBrain truth."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Keep the standalone script usable from a plain source checkout. Pytest adds
# src/ for its own process, but a subprocess launched by path starts with only
# scripts/ on sys.path.
_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if _SRC_DIR.is_dir():
    sys.path.insert(0, str(_SRC_DIR))

from ocbrain.db import connect  # noqa: E402
from ocbrain.seal_truth import compile_sealed_release, preview_sealed_release  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seal", type=Path, required=True)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(
            os.environ.get(
                "OCBRAIN_DB", str(Path.home() / ".ocbrain/ocbrain.sqlite")
            )
        ),
    )
    parser.add_argument("--wiki-dir", type=Path, default=Path.home() / ".ocbrain/wiki")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the validated sealed release; without this flag only preview",
    )
    args = parser.parse_args()
    conn = connect(args.db.expanduser())
    try:
        if args.apply:
            result = compile_sealed_release(
                conn,
                args.seal,
                wiki_dir=args.wiki_dir,
            )
        else:
            result = preview_sealed_release(conn, args.seal)
    finally:
        conn.close()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
