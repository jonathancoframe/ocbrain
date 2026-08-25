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

- **`C90` (mccabe complexity).** At `max-complexity=5` the repo reports 138
  violations; at 10 it still reports 39. Several are irreducible without
  redesigning the projector and the ranker — `search_core_v1` and
  `project_core_v1` are genuinely branchy because the domain is.
- **`S` (bandit).** 39 findings, nearly all deliberate: parameterised SQL built
  from fixed local constants, `urllib` calls that exist so the core stays
  dependency-free, `subprocess` in the doctor.

A gate nobody can pass is a gate people learn to skip. Those two are reported as
advice instead.

## The advisory pass (report only)

```bash
scripts/code-quality.sh                      # src/ocbrain + scripts
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

The v2 deletion took two whole packages out of the tree, so these counts moved.
Re-measured against the current `src/ocbrain` and `scripts`, the extended ruff
pass reports 44 findings:

| Finding | Count | Disposition |
|---|---|---|
| `RUF100` unused-noqa | 29 | **Not real.** These are the `# noqa: S310` / `# noqa: BLE001` suppressions that only look unused because `S` and `BLE` are not in this narrowed selection. See the warning above |
| `PERF401` manual-list-comprehension | 6 | Real; each needs a rewrite with test coverage |
| `RUF022`, `SIM102`, `SIM103`, `SIM114`, `SIM118`, `RUF007` | 8 total | Judgement calls; read each one |
| `RUF002` ambiguous unicode | 1 | An em dash in a docstring. Correct as written |

So fifteen findings are worth reading and twenty-nine are an artifact of the
narrowed selection.

The `opengrep` slop counts are not re-measured here — the tool is optional and
external, and its ruleset lives outside the repo. Run
`scripts/code-quality.sh` yourself for a current number; the previous reading of
~66 was taken before the deletion and covered `packages/` as well.

Fixing these is welcome. Doing it in one sweep is not — the value is in the
reading, and a large mechanical diff destroys the review.
