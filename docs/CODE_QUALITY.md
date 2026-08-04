# Code quality

Two layers, deliberately separated.

## The gate (enforced)

`CONTRIBUTING.md` defines the contract, and CI runs the same commands:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_golden_context_v1.py
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src tests
git diff --check
.venv/bin/ocbrain public-safety-check --root "$PWD"
```

The ruff selection is `E, F, I, UP, B, C4, RET, PIE`. Every one of those is at
**zero** across the repository, which is the property that makes it a gate: a new
finding is a regression someone just introduced, not debt someone inherited.

Two categories are deliberately **out**:

- **`C90` (mccabe complexity).** At `max-complexity=5` the repo reports ~212
  violations; at 10 it still reports dozens. Several are irreducible without
  redesigning the projector and the ranker — `search_core_v1` and
  `project_core_v1` are genuinely branchy because the domain is.
- **`S` (bandit).** ~40 findings, nearly all deliberate: parameterised SQL built
  from fixed local constants, `urllib` calls that exist so the core stays
  dependency-free, `subprocess` in the doctor.

A gate nobody can pass is a gate people learn to skip. Those two are reported as
advice instead.

## The advisory pass (report only)

```bash
scripts/code-quality.sh                      # src/ocbrain + packages + scripts
scripts/code-quality.sh src/ocbrain/curator.py
```

It never edits, never commits, and exits 0 even with findings. It reports the
gate, then the extended ruff categories, then complexity (`lizard`),
maintainability (`radon mi`), duplication (`jscpd`), and expression-level slop
(`opengrep`). Optional tools are skipped with a note when absent.

`opengrep` needs an external ruleset; point `OPENGREP_SLOP` at the
`opengrep_slop.py` wrapper:

```bash
OPENGREP_SLOP=/path/to/opengrep/opengrep_slop.py scripts/code-quality.sh
```

### Reading the advisory output

The findings are judgement calls, not a to-do list. Three lessons from the first
pass over this repo, all learned the hard way:

**Never bulk-autofix `RUF100` (unused-noqa) under a narrowed rule selection.**
`--select RUF100,SIM,PERF` makes every `# noqa: S310` and `# noqa: BLE001` look
unused, because `S` and `BLE` are not selected in that run. Autofix then deletes
them *and the comments explaining why they were there*. Those suppressions are
correct under the wider selection, so the deletion is invisible until someone
turns the wider selection on. If you touch `RUF100`, run it under the full
selection and read every hunk.

**`--unsafe-fixes` earns its name.** One run of it here deleted the same
suppressions and broke seven tests. Run it on one rule at a time, with the suite
after each.

**A rule can make code worse.** `SIM114` (if-with-same-arms) collapsed a
three-branch `if`/`elif` in `events.py` into a single 252-character boolean —
fewer branches, unreadable. The right response was to name the three conditions,
which satisfies both the rule and the reader. When a fix makes you squint, the
rule found a real smell and proposed the wrong remedy.

### Standing advisory count

Roughly 30 extended-ruff findings and ~66 slop findings remain, dominated by:

| Finding | Count | Disposition |
|---|---|---|
| `PERF401` manual-list-comprehension | 13 | Real; each needs a rewrite with test coverage |
| `RUF001`/`RUF002` ambiguous unicode | 7 | Em dashes in prose. Correct as written |
| `slop-py-long-comment` | ~36 | Mixed: some narration to compress, some load-bearing rationale that belongs in docs |
| `slop-py-except-swallow-default` | 13 | Mostly deliberate degradation paths that are documented in place |
| `slop-py-section-banner` | 12 | Cheap to remove; a file needing chapters may need splitting |

Fixing these is welcome. Doing it in one sweep is not — the value is in the
reading, and a 300-file mechanical diff destroys the review.
