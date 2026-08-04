#!/bin/bash
# brain-sync.sh — incremental local-activity harvest into the OCBrain v1 core.
#
# Fingerprint-gated: unchanged transcript files are skipped, so this is cheap
# to run every few minutes. Safe under concurrency: SQLite WAL + busy timeout,
# and ocbrain evidence ids are stable/deduped.
set -uo pipefail

# Resolve the installed checkout and the same active core used by MCP clients.
# Explicit environment values win; otherwise use the checkout-local active DB
# pointer and only fall back to ~/.ocbrain for installations without one.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${OCBRAIN_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PY="${OCBRAIN_PYTHON:-$REPO/.venv/bin/python}"
ACTIVE_DB_FILE="${OCBRAIN_ACTIVE_DB_FILE:-$REPO/data/active-core.path}"
DB="${OCBRAIN_DB:-}"
if [[ -z "$DB" && -r "$ACTIVE_DB_FILE" ]]; then
  IFS= read -r DB < "$ACTIVE_DB_FILE"
fi
DB="${DB:-$HOME/.ocbrain/ocbrain.sqlite}"
if [[ "$DB" != /* ]]; then
  echo "OCBrain DB path must be absolute: $DB" >&2
  exit 2
fi
export OCBRAIN_CONFIG="${OCBRAIN_CONFIG:-$REPO/data/ocbrain.config.json}"

SYNC_PROJECT="${OCBRAIN_SYNC_PROJECT:-workspace}"
SYNC_PRIVACY_SCOPE="${OCBRAIN_SYNC_PRIVACY_SCOPE:-workspace}"
SYNC_BATCH_SIZE="${OCBRAIN_SYNC_BATCH_SIZE:-25}"

# Hard ceiling for the whole harvest. A cold multi-GB import can take a while,
# but a stuck run must never block the launchd schedule indefinitely (launchd
# will not fire a new instance while a previous one is still alive).
HARVEST_BUDGET_SECONDS="${OCBRAIN_SYNC_BUDGET_SECONDS:-2700}"

echo "== $(date -u +%FT%TZ) brain-sync start =="

# Single-instance, portable: macOS has no flock(1). A mkdir lock is atomic on
# POSIX, and we recover from stale locks left by killed runs via PID liveness.
LOCKDIR="$HOME/.ocbrain/brain-sync.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  holder="$(cat "$LOCKDIR/pid" 2>/dev/null || echo '')"
  if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
    echo "another brain-sync (pid $holder) is running; exiting"
    exit 0
  fi
  echo "stale lock from pid ${holder:-unknown}; reclaiming"
  rm -rf "$LOCKDIR"
  mkdir "$LOCKDIR" 2>/dev/null || { echo "lock contention; exiting"; exit 0; }
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT

# Run one command under a hard time budget; partial batches stay committed and
# the next run resumes via the fingerprint/dedup gates.
run_with_budget() {
  local budget="$1"; shift
  "$@" &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 15
    waited=$((waited + 15))
    if (( waited >= budget )); then
      echo "budget ${budget}s exceeded for: $* — killing (partial work stays committed)"
      kill "$pid" 2>/dev/null
      sleep 5
      kill -9 "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 124
    fi
  done
  wait "$pid"
}

# Yield CPU to interactive/agent work; the harvest is background maintenance.
renice -n 10 -p $$ >/dev/null 2>&1 || true

event_count() {
  sqlite3 "$DB" "SELECT COUNT(*) FROM brain_events;" 2>/dev/null || echo "?"
}

before="$(event_count)"
echo "brain_events before: $before"

# 1. Hermes transcripts: state.db -> JSONL export (content-compared writes).
"$PY" "$REPO/scripts/export-hermes-transcripts.py"

# 2. Cursor chats: state.vscdb -> JSONL export (content-compared writes).
"$PY" "$REPO/scripts/export-cursor-chats.py"

# 3. Agent runtime history (Codex, Claude Code, Hermes, Cursor) -> v1 core.
run_with_budget "$HARVEST_BUDGET_SECONDS" \
  "$PY" -m ocbrain.cli --db "$DB" import-history \
  "$HOME/.codex/sessions" \
  "$HOME/.codex/archived_sessions" \
  "$HOME/.claude/projects" \
  "$HOME/.hermes/sessions" \
  "$HOME/.ocbrain/exports/cursor" \
  --project "$SYNC_PROJECT" --privacy-scope "$SYNC_PRIVACY_SCOPE" \
  --batch-size "$SYNC_BATCH_SIZE" --evidence-only

# 4. Agent memory/instruction files.
"$PY" -m ocbrain.cli --db "$DB" import-memory \
  "$HOME/.claude/CLAUDE.md" \
  "$HOME/.codex/AGENTS.md" \
  "$HOME/.hermes/SOUL.md" \
  "$HOME/.hermes/memories" \
  --project "$SYNC_PROJECT" --privacy-scope "$SYNC_PRIVACY_SCOPE" --evidence-only

# 5. Reconcile core projections. `sync` refuses past its --max-events bound
# rather than doing partial work, and a cold or long-delayed harvest can enqueue
# far more than the 1000 default, so raise the ceiling for the scheduled path.
"$PY" -m ocbrain.cli --db "$DB" sync --max-events "${OCBRAIN_SYNC_MAX_EVENTS:-200000}"

after="$(event_count)"
echo "brain_events after: $after"
echo "== $(date -u +%FT%TZ) brain-sync done =="
