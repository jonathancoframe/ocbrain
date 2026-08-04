#!/usr/bin/env python3
"""Deprecated shim: use ``scripts/wiki-curator.py --provider moonshot``.

The curator became provider-pluggable so the same evidence gates and local quote
validation apply whichever hosted model runs. This entry point is kept so
existing operator scripts and launchd jobs keep working; it forwards to
``wiki-curator.py`` with the Moonshot/Kimi defaults and its historical dotenv
location.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FORWARD_TO = Path(__file__).resolve().parent / "wiki-curator.py"


def main() -> int:
    argv = sys.argv[1:]
    forwarded = ["--provider", "moonshot"]
    if not any(arg == "--env-file" or arg.startswith("--env-file=") for arg in argv):
        # Preserve the original default; ~/.common is the generalized script's.
        forwarded += ["--env-file", str(Path.home() / ".hermes" / ".env")]
    print(
        "kimi-wiki-curator.py is deprecated; "
        "use wiki-curator.py --provider moonshot",
        file=sys.stderr,
    )
    os.execv(sys.executable, [sys.executable, str(FORWARD_TO), *forwarded, *argv])


if __name__ == "__main__":
    raise SystemExit(main())
