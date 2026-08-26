#!/usr/bin/env bash
# OCBrain SessionStart hook for Claude Code.
#
# Prints the deterministic session-start briefing for the current project.
# Claude Code injects a SessionStart hook's stdout into the conversation as
# context (2.1.0 and later), so what this prints is what the session starts
# holding.
#
# It is intentionally boring. The whole value of the object is that it produces
# the same bytes every time, so this script must not add a timestamp, a random
# tip, or a "welcome back" -- any of those turn a contract into a variable.
#
# Install (the file itself is NOT written by ocbrain; you install it):
#
#   1. cp examples/harness/ocbrain-session-start.sh ~/.claude/hooks/
#      chmod +x ~/.claude/hooks/ocbrain-session-start.sh
#
#   2. add to ~/.claude/settings.json:
#
#      {
#        "hooks": {
#          "SessionStart": [
#            {
#              "hooks": [
#                {
#                  "type": "command",
#                  "command": "~/.claude/hooks/ocbrain-session-start.sh"
#                }
#              ]
#            }
#          ]
#        }
#      }
#
#   3. set OCBRAIN_PROJECT per repo (direnv, or a project .claude/settings.json
#      env block). Without it this falls back to the git repo's directory name,
#      which is usually right and is never wrong in a way that fails loudly.
#
# Exits 0 and prints nothing when the brain is unreachable. A session that
# cannot reach OCBrain should still start; a hook that fails the session start
# because a database is locked is a hook that gets uninstalled.

set -uo pipefail

OCBRAIN_BIN="${OCBRAIN_BIN:-ocbrain}"
BUDGET="${OCBRAIN_BRIEFING_BUDGET:-1500}"

project="${OCBRAIN_PROJECT:-}"
if [ -z "$project" ]; then
  root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$root" ] && project="$(basename "$root")"
fi
[ -z "$project" ] && exit 0

command -v "$OCBRAIN_BIN" >/dev/null 2>&1 || exit 0

"$OCBRAIN_BIN" briefing \
  --project "$project" \
  --runtime claude-code \
  --budget-chars "$BUDGET" \
  --text 2>/dev/null || exit 0
