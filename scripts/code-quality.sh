#!/bin/bash
# code-quality.sh — report-only code-quality pass over production code.
#
# The project gate in CONTRIBUTING.md is the contract: ruff's configured rules,
# the test suite, compileall, a whitespace check, and the public-safety scan.
# This script is the *aspirational* layer on top — extra ruff categories, plus
# complexity, maintainability, duplication, and expression-level "slop" checks
# that a linter's default rules do not cover.
#
# REPORT ONLY. It never edits, never commits, and exits 0 even with findings, so
# it can be read as advice rather than obeyed as a gate. Findings are judgement
# calls: some are real defects, some are the cost of a deliberate design, and a
# few are the tool being wrong. Read them, don't auto-apply them — blindly
# fixing `unused-noqa` under a narrowed rule selection deletes suppressions the
# wider selection still needs, along with the comments explaining why.
#
# Optional tools are skipped with a note when absent; nothing here is required
# to develop or ship OCBrain.
#
#   scripts/code-quality.sh            # src/ocbrain + packages + scripts
#   scripts/code-quality.sh src/ocbrain/curator.py
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${OCBRAIN_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
cd "$REPO" || exit 2
RUFF="${RUFF:-$REPO/.venv/bin/ruff}"

# Production code only: tests, fixtures, and conftest are held to a different
# standard on purpose (a test may repeat itself to stay readable in isolation).
if (( $# > 0 )); then
  TARGETS=("$@")
else
  TARGETS=(src/ocbrain packages scripts)
fi
EXCLUDES=(--exclude tests --exclude '**/test_*' --exclude '**/*_test.py'
          --exclude '**/conftest.py' --exclude '**/fixtures' --exclude '**/mocks')

section() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
missing() { printf '   skipped: %s not installed\n' "$1"; }

section "ruff — configured project gate (this one is the contract)"
"$RUFF" check "${TARGETS[@]}" || true

section "ruff — extended categories (advisory)"
# Beyond the configured set: SIM/C4/PIE/RET/PERF/RUF are mostly actionable;
# C90 (mccabe) and S (bandit) are reported separately below because they carry
# a large standing count that is not incrementally fixable.
"$RUFF" check --select SIM,C4,PIE,RET,PERF,RUF "${EXCLUDES[@]}" --statistics "${TARGETS[@]}" || true

section "ruff — complexity and security (standing debt, not a gate)"
"$RUFF" check --select C90,S --config "lint.mccabe.max-complexity=10" \
  "${EXCLUDES[@]}" --statistics "${TARGETS[@]}" || true

section "lizard — function size and complexity"
if command -v uvx >/dev/null 2>&1; then
  uvx lizard -l python -C 10 -a 5 -L 80 -T nloc=60 \
    -x 'tests/*' -x '*/tests/*' -x '*/test_*' -x '*_test.py' -x '*/conftest.py' \
    "${TARGETS[@]}" 2>/dev/null | tail -25 || true
else
  missing uvx
fi

section "radon — maintainability index below grade A"
if command -v uvx >/dev/null 2>&1; then
  uvx radon mi -s -n B \
    --exclude 'tests/*,*/tests/*,test_*,*_test.py,conftest.py' \
    "${TARGETS[@]}" 2>/dev/null || true
else
  missing uvx
fi

section "jscpd — duplication"
if command -v npx >/dev/null 2>&1; then
  npx --yes jscpd "${TARGETS[@]}" \
    --min-lines 5 --min-tokens 40 --threshold 100 --mode strict \
    --max-lines 5000 --max-size 500kb --gitignore \
    --ignore-pattern '\b(import|from)\b' \
    --ignore '**/tests/**,**/test_*,**/*_test.py,**/conftest.py,**/fixtures/**' \
    --reporters consoleFull --format python 2>/dev/null | tail -25 || true
else
  missing npx
fi

section "opengrep — expression-level slop"
# Rules live outside this repo (see docs/CODE_QUALITY.md); set OPENGREP_SLOP to
# the wrapper if you have it. Catches shapes complexity metrics miss: empty
# except, log-and-drop, nested ternaries, type suppressions, mutable defaults.
if [[ -n "${OPENGREP_SLOP:-}" && -f "${OPENGREP_SLOP}" ]] && command -v opengrep >/dev/null 2>&1; then
  # shellcheck disable=SC2046
  python3 "$OPENGREP_SLOP" --rules python \
    $(find "${TARGETS[@]}" -name '*.py' -not -path '*/tests/*' -not -name 'conftest.py' 2>/dev/null) \
    2>/dev/null | tail -30 || true
else
  printf '   skipped: set OPENGREP_SLOP to the opengrep_slop.py wrapper (and install opengrep)\n'
fi

printf '\n\033[1mReport only — nothing was changed.\033[0m\n'
