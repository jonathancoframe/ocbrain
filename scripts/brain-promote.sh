#!/bin/bash
# brain-promote.sh — turn harvested evidence into servable knowledge, and retire
# knowledge that has stopped earning its place.
#
# brain-sync.sh only records evidence; nothing it does makes a fact retrievable.
# Without a promotion pass the corpus freezes at whatever was last curated by
# hand, and without a retirement pass it only ever grows. This script is the
# other half of the loop:
#
#   1. curate   — compile eligible evidence into wiki facts (hosted model call)
#   2. hygiene  — retire expired / never-retrieved / badly-judged beliefs
#   3. wiki     — rematerialize the wiki, which is also what deletes orphan pages
#   4. lint     — post-condition check on the materialized tree
#   5. vectors  — rebuild the dense sidecar so new facts are semantically findable
#
# NOT installed by default. OCBrain ships no scheduler; an operator opts in by
# loading a launchd agent (see docs/SCHEDULED_MAINTENANCE.md). Step 1 is the only
# one that leaves the machine, and it is digest-gated: an unchanged corpus makes
# no API call, so a quiet cycle is free.
set -uo pipefail

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
# Do NOT pin the config into the checkout. ocbrain resolves
# ~/.ocbrain/ocbrain.config.json first, which survives a `git clean -xfd`, a
# fresh clone, and a worktree switch; forcing $REPO/data here would reintroduce
# the failure where operator settings vanish with the working tree and the loop
# keeps exiting 0 while promoting nothing. Set OCBRAIN_CONFIG yourself to
# override.

PROMOTE_PROJECT="${OCBRAIN_PROMOTE_PROJECT:-workspace}"
PROMOTE_PROVIDER="${OCBRAIN_PROMOTE_PROVIDER:-anthropic}"
PROMOTE_MAX_BELIEFS="${OCBRAIN_PROMOTE_MAX_BELIEFS:-24}"
WIKI_DIR="${OCBRAIN_WIKI_DIR:-$(dirname -- "$DB")/wiki}"
BUDGET_SECONDS="${OCBRAIN_PROMOTE_BUDGET_SECONDS:-1800}"

# Which hygiene classes may apply unattended. `expired` is unambiguous;
# `unused` and `unhelpful` are heuristics, and `unhelpful` additionally refuses
# to act until a feedback watermark has been set.
HYGIENE_CLASSES="${OCBRAIN_HYGIENE_CLASSES:---class expired --class unused --class unhelpful}"
# Report-only by default. Set OCBRAIN_HYGIENE_APPLY=1 to let it retire beliefs;
# every retraction is soft and undoable with `ocbrain hygiene --restore`.
HYGIENE_APPLY="${OCBRAIN_HYGIENE_APPLY:-0}"

echo "== $(date -u +%FT%TZ) brain-promote start =="

LOCKDIR="$HOME/.ocbrain/brain-promote.lock.d"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  holder="$(cat "$LOCKDIR/pid" 2>/dev/null || echo '')"
  if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
    echo "another brain-promote (pid $holder) is running; exiting"
    exit 0
  fi
  echo "stale lock from pid ${holder:-unknown}; reclaiming"
  rm -rf "$LOCKDIR"
  mkdir "$LOCKDIR" 2>/dev/null || { echo "lock contention; exiting"; exit 0; }
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT

run_with_budget() {
  local budget="$1"; shift
  "$@" &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 5
    waited=$((waited + 5))
    if (( waited >= budget )); then
      echo "budget ${budget}s exceeded for: $* — killing"
      kill "$pid" 2>/dev/null
      sleep 5
      kill -9 "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 124
    fi
  done
  wait "$pid"
}

renice -n 10 -p $$ >/dev/null 2>&1 || true

serving_count() {
  sqlite3 "$DB" \
    "SELECT COUNT(*) FROM current_beliefs WHERE serve=1 AND status='current';" \
    2>/dev/null || echo "?"
}

before="$(serving_count)"
echo "serving beliefs before: $before"

# 1. Curate. Digest-gated, so this is a no-op (and free) when nothing changed.
run_with_budget "$BUDGET_SECONDS" \
  "$PY" "$REPO/scripts/wiki-curator.py" \
  --db "$DB" \
  --provider "$PROMOTE_PROVIDER" \
  --project "$PROMOTE_PROJECT" \
  --wiki-dir "$WIKI_DIR" \
  --max-beliefs "$PROMOTE_MAX_BELIEFS" \
  --apply \
  || echo "curate step failed or was capped; continuing with retirement"

# 2. Retire. Reported either way; only applied when explicitly enabled.
hygiene_args=(--db "$DB" hygiene $HYGIENE_CLASSES)
if [[ "$HYGIENE_APPLY" == "1" ]]; then
  hygiene_args+=(--apply)
fi
"$PY" -m ocbrain.cli "${hygiene_args[@]}" \
  || echo "hygiene step failed; continuing"

# 3. Rematerialize the wiki. A full rebuild + atomic swap is what removes pages
# for beliefs retired above; retirements outside a curate run leave orphans until
# this happens.
"$PY" - "$DB" "$WIKI_DIR" <<'PYEOF' || echo "wiki rematerialize failed; continuing"
import sys
from pathlib import Path

from ocbrain.db import connect
from ocbrain.wiki import materialize_wiki

db_path, wiki_dir = Path(sys.argv[1]), Path(sys.argv[2])
conn = connect(db_path)
try:
    count = materialize_wiki(conn, wiki_dir, run={"action": "scheduled-rematerialize"})
    print(f"wiki pages: {count}")
finally:
    conn.close()
PYEOF

# 4. Post-condition check on the tree we just wrote. Non-zero means findings, so
# surface them in the log rather than swallowing the exit code.
"$PY" "$REPO/scripts/wiki-lint.py" "$WIKI_DIR" --db "$DB" \
  || echo "wiki-lint reported findings (see above)"

# 5. Rebuild the dense sidecar so newly promoted facts are semantically findable.
# Retrieval degrades to lexical-only against a stale sidecar, so a promote that
# skipped this would leave new knowledge half-reachable.
"$PY" -m ocbrain.cli --db "$DB" vector-build \
  || echo "vector-build failed (is the local embedder running?); continuing"

after="$(serving_count)"
echo "serving beliefs after: $after"
echo "== $(date -u +%FT%TZ) brain-promote done =="
