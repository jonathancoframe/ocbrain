#!/usr/bin/env bash
# Mutation proof for the closeout-discipline gates added on fix/closeout-discipline.
#
# Every gate here is mutated so it SHOULD fail, the named test is run, and the
# run must fail. Then the mutation is reverted and the test must pass again.
# A gate whose failing input is unreachable is the defect class this exists for.
#
# Two documented traps are handled explicitly:
#   (a) a size-preserving mutation restored inside the same second can run the
#       OTHER version's .pyc and reverse the verdict both ways -- so every
#       __pycache__ is removed before every run and PYTHONDONTWRITEBYTECODE=1
#       is set for all of them;
#   (b) the expected result of a probe is never printed before the probe
#       returns -- this script reports what happened, it does not assert it.
#
# Usage: ops/mutation-proof-closeout-discipline.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY="${OCBRAIN_PYTHON:-$ROOT/../../../.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT/src"

clear_bytecode() {
  find "$ROOT/src" "$ROOT/tests" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
}

run_test() {
  clear_bytecode
  "$PY" -m pytest -q -p no:cacheprovider "$1" >/tmp/mutproof.$$ 2>&1
  local rc=$?
  tail -3 /tmp/mutproof.$$ | tr '\n' ' '
  rm -f /tmp/mutproof.$$
  return $rc
}

# mutate FILE FROM TO TEST LABEL
mutate() {
  local file="$1" from="$2" to="$3" test="$4" label="$5"
  local backup
  backup="$(mktemp)"
  cp "$file" "$backup"
  "$PY" - "$file" "$from" "$to" <<'EOF'
import pathlib, sys
path, old, new = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
if text.count(old) != 1:
    print(f"MUTATION ANCHOR NOT UNIQUE ({text.count(old)}) in {path}", file=sys.stderr)
    raise SystemExit(2)
path.write_text(text.replace(old, new))
EOF
  if [ $? -ne 0 ]; then
    printf '%-58s ANCHOR MISSING\n' "$label"
    cp "$backup" "$file"; rm -f "$backup"; return 1
  fi
  local out
  out="$(run_test "$test")"
  local mutated_rc=$?
  cp "$backup" "$file"; rm -f "$backup"
  local restored_out
  restored_out="$(run_test "$test")"
  local restored_rc=$?
  printf '%-58s mutated=%s restored=%s\n' "$label" \
    "$([ $mutated_rc -ne 0 ] && echo FAILS || echo PASSES)" \
    "$([ $restored_rc -eq 0 ] && echo PASSES || echo FAILS)"
  printf '    mutated : %s\n    restored: %s\n' "$out" "$restored_out"
}

C=src/ocbrain/closeout.py
T=tests/test_closeout_discipline.py

echo "== defect 1: session shape gate =="
mutate "$C" \
  'RUNTIME_SESSION_SHAPES = frozenset({"runtime_uuid", "runtime_hex"})' \
  'RUNTIME_SESSION_SHAPES = frozenset({"runtime_uuid", "runtime_hex", "slug", "date_like", "filesystem_path", "contains_space"})' \
  "$T::test_a_hand_written_session_id_is_refused_and_the_error_says_where_to_get_one" \
  "shape gate admits every shape"

mutate "$C" \
  '        if policy == "enforce":
            raise ValueError(_session_id_error(claimed, shape))' \
  '        if policy == "never":
            raise ValueError(_session_id_error(claimed, shape))' \
  "$T::test_a_hand_written_session_id_is_refused_and_the_error_says_where_to_get_one" \
  "enforce branch made unreachable"

mutate "$C" \
  '    if hint is not None and is_runtime_session_id(hint):' \
  '    if False and hint is not None and is_runtime_session_id(hint):' \
  "$T::test_the_harness_attested_hint_outranks_the_model_and_the_disagreement_is_kept" \
  "harness hint no longer outranks the model"

mutate "$C" \
  '    elif observed is not None and observed.server_connection_id:' \
  '    elif False and observed is not None and observed.server_connection_id:' \
  "$T::test_omitting_the_session_is_legal_and_the_server_fills_it_from_its_own_connection" \
  "server-connection fallback removed"

echo
echo "== defect 2: runtime family =="
mutate "$C" \
  '            if segments & tokens:' \
  '            if any(token in folded for token in tokens):' \
  "$T::test_a_normaliser_matching_substrings_invents_data" \
  "segment matching reverted to substring"

mutate "$C" \
  '        if mapped in RUNTIME_FAMILIES:' \
  '        if mapped is not None:' \
  "$T::test_an_operator_alias_can_name_an_install_specific_label" \
  "alias may invent a family"

echo
echo "== defect 3: unresolved gate =="
mutate "$C" \
  '    return status not in CLEAN_SUCCESS_STATUSES or verification_status == "failed"' \
  '    return status not in CLEAN_SUCCESS_STATUSES' \
  "$T::test_the_unresolved_gate_catches_281_of_the_1236_live_closeouts" \
  "verifier trigger dropped (status only)"

mutate "$C" \
  '        problems.append(_unresolved_error(status, verification_status))' \
  '        pass' \
  "$T::test_a_completed_closeout_with_a_failed_verifier_must_say_what_failed" \
  "unresolved refusal removed"

mutate "$C" \
  '        raise ValueError("\n\n".join(problems))' \
  '        raise ValueError(problems[0])' \
  "$T::test_both_gates_report_together_so_one_retry_fixes_both" \
  "only the first refusal is reported"

mutate "$C" \
  'CLEAN_SUCCESS_STATUSES = {"completed"}' \
  'CLEAN_SUCCESS_STATUSES = {"completed", "partial", "blocked", "failed", "cancelled"}' \
  "$T::test_every_non_completion_status_owes_an_explanation" \
  "every status counted as a clean success"

echo
echo "== config fail-open =="
mutate "$C" \
  '    if settings.session_id_policy not in SESSION_ID_POLICIES:
        return replace(settings, session_id_policy=default.session_id_policy)' \
  '    pass' \
  "$T::test_a_misspelled_policy_falls_back_instead_of_taking_the_write_path_down" \
  "typo policy takes the write path down"

echo
echo "== migration =="
mutate src/ocbrain/core_v1.py \
  '    ("task_closeouts", "session_id_source", "TEXT"),' \
  '' \
  "$T::test_an_existing_core_gains_the_columns_before_the_first_closeout_lands" \
  "session_id_source column not migrated"

mutate src/ocbrain/db.py \
  '    for column, decl in _V7_TASK_CLOSEOUT_COLUMNS:
        _ensure_column(conn, "task_closeouts", column, decl)' \
  '    pass' \
  "$T::test_the_legacy_initializer_also_migrates_an_existing_database" \
  "legacy db.py migration skipped"
