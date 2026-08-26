# Scheduled maintenance (operator opt-in)

OCBrain installs no scheduler. The core is on-demand, and nothing in this
repository loads a timer, watchdog, or recurring job — see `CONTRIBUTING.md`.

Two shell scripts exist for operators who *do* want continuous operation. They
are documented here so the opt-in is explicit and reversible; installing them is
a local decision, not part of the published contract.

## Why two scripts

The loop has two halves, and running only the first is the failure mode this
document exists to prevent:

| Script | What it does | Leaves the machine? |
|---|---|---|
| `scripts/brain-sync.sh` | Harvests local agent transcripts and memory files into the ledger as **evidence only** | No |
| `scripts/brain-promote.sh` | Compiles evidence into servable beliefs, retires stale ones, rebuilds the wiki and the dense index | Yes — step 1 only |

`brain-sync.sh` never promotes anything. It passes `--evidence-only`, and there
is no automatic promotion path at all — the `automatic_activation` flag that
once existed is deleted, having produced 239 `auto_compiled` beliefs that were
all later retracted. So a brain running only the harvester accumulates evidence
that no retrieval can ever return. That is the state a real deployment was found
in: a healthy write path, ~1,900 evidence objects, and a serving corpus frozen
for two weeks at whatever was last curated by hand.

## brain-sync.sh — harvest

Fingerprint-gated, so unchanged transcripts are skipped and a run costs little.
Safe under concurrency (SQLite WAL, a `mkdir` lock, stable/deduped evidence ids).

Environment:

| Variable | Default | Notes |
|---|---|---|
| `OCBRAIN_DB` | `data/active-core.path`, else `~/.ocbrain/ocbrain.sqlite` | Must be absolute |
| `OCBRAIN_SYNC_PROJECT` | `workspace` | Project scope for harvested evidence |
| `OCBRAIN_SYNC_BUDGET_SECONDS` | `2700` | Hard ceiling on the `import-history` stage |
| `OCBRAIN_SYNC_MAX_EVENTS` | `200000` | `sync` refuses past its bound rather than doing partial work; a cold harvest exceeds the 1000 default |

A 15-minute interval suits a machine in daily use.

## brain-promote.sh — promote and retire

A daily snapshot, then five ordered stages: curate → hygiene → rematerialize
wiki → lint → rebuild vectors, plus an opt-in sixth. Each continues on failure so
one bad stage cannot strand the rest. The snapshot runs once per UTC day rather than hourly, through
the SQLite online-backup API so it is safe against the live WAL, and a failed
snapshot aborts the cycle before anything can retire or rewrite a belief.

There used to be a sixth stage, `ocbrain deslop`, between hygiene and the wiki
rebuild. It is gone. Across 155 consecutive hourly runs it reported
`actionable: 0, repairs: [], judged: false` every time, because the same rules
already fire one layer earlier as the curator's write-time gate. See
[DESLOP.md](DESLOP.md) for where those rules live now.

**The curate step is the only hosted call**, and it is digest-gated per project:
a project whose eligible evidence has not changed since the last run is skipped
without contacting the provider, and a cycle where nothing moved is therefore
free. That is what makes an hourly schedule reasonable across many project
scopes.

Which projects get curated is `curator.projects` in your config, not a pin in
the script. Set it to the scopes you actually work in:

```json
"curator": {
  "projects": ["coframe", "coframe-personalization", "workspace"],
  "min_evidence_per_project": 3
}
```

A project with fewer than `min_evidence_per_project` eligible objects is skipped
and reported as `skipped_thin_project` rather than billed. Every project the run
considered emits one JSON line naming its status, followed by a
`wiki-curate-rollup` line, so the promote log stays greppable.

Environment:

| Variable | Default | Notes |
|---|---|---|
| `OCBRAIN_PROMOTE_PROVIDER` | `anthropic` | Also `openai`, `moonshot` |
| `OCBRAIN_PROMOTE_PROJECT` | unset | Set it to curate exactly one scope for a one-off run; unset uses `curator.projects` |
| `OCBRAIN_PROMOTE_MAX_BELIEFS` | `24` | Upper bound per run; fewer is better |
| `OCBRAIN_PROMOTE_MAX_TOKENS` | provider-aware | Completion-token budget. Defaults to `8000` for `moonshot` and `16000` for `anthropic` or `openai`; an explicit value overrides the provider default. |
| `OCBRAIN_HYGIENE_CLASSES` | `--class expired --class redundant` | Both surviving classes. Narrow it to one, e.g. `--class expired` |
| `OCBRAIN_HYGIENE_APPLY` | `0` (report only) | `1` lets the sweep retire beliefs |
| `OCBRAIN_PROMOTE_BUDGET_SECONDS` | `1800` | Ceiling on the curate stage |
| `OCBRAIN_PROMOTE_BACKUP` | `1` | `0` skips the daily snapshot |
| `OCBRAIN_BACKUP_DIR` | `~/.ocbrain/backups` | Snapshot destination |
| `OCBRAIN_BACKUP_KEEP` | `7` | Auto snapshots retained; hand-made `pre-*` backups are never rotated |
| `OCBRAIN_WIKI_DIR` | `<db dir>/wiki` | Materialization target |

`OCBRAIN_HYGIENE_CLASSES` defaulting to both classes matters. `redundant` was
once missing from the default, and duplicate wiki facts accumulated to a quarter
of the serving corpus while the loop reported clean every hour.

The API key is read from the environment, falling back to `~/.common`. Only the
variable *name* is ever configured; the value is never persisted by OCBrain.

An hourly interval is ample — evidence arrives at human pace.

### What the curator sends

Only evidence with `internal` visibility and `hosted_ok` egress policy, in a
configured project scope, bounded to 4,000 characters per body. The list used to
read `public`/`internal`; `public` visibility is gone from `VISIBILITIES`
entirely, because it was published for two years and never once written. A scope
matches by canonical spelling, so evidence a client stored as `Coframe Brain` is
reached by the project named `coframe-brain`; widening only ever adds spellings
of a project already on the list. Raw transcripts are
structurally ineligible: their kind is not in the eligible set. Confidential,
secret, local-only, and prohibited objects never qualify. `--allow-hosted-egress`
additionally admits `approval_required` objects and nothing else.

Every returned claim is verified locally before it can become a belief: key,
title, body, category, lifecycle, and confidence are range-checked, and each
supporting quote must appear **verbatim** in the evidence it cites. A model that
invents a citation produces no belief.

### What the curator does when a fact changes

A claim on a key the corpus already serves, carrying a different statement, is a
**correction**. It routes through the same supersession transaction a runtime
`brain.supersede` uses: the old copy is era-closed with `superseded_by`, the
replacement is minted under its own id keeping the same key, the confidence is
capped at `min(old, claim, 0.7)`, and a paired `correction_recorded` event says
the fact changed. An unchanged body is still a free no-op, and a claim that
merely rewords an existing fact still updates it in place.

Because it is the same transaction, it inherits the same routing. A doctrine
target and a pinned target become undecided proposals in the pending ledger
instead of landing, and `brain.digest` reports the depth as
`pending_corrections`.

The per-caller rate cap (`supersede.direct_cap`, `OCBRAIN_SUPERSEDE_DIRECT_CAP`,
default 8 per caller per 24 hours) does **not** apply to the curator, under
`supersede.curator_direct` (default on). That cap is sized for a runtime agent
and was the wrong instrument here. Its first unattended night on the live core
is the whole argument: past the eighth correction the curator pended everything,
and because a pending proposal does not change the input that produced it, the
next hourly cycle re-derived the same claims and pended them again — **283
undecided proposals against 33 beliefs in eighteen hours**, one pair carrying
twelve identical copies, growing at roughly 17/hour with nothing to stop it.
Setting `curator_direct` to false restores the all-pending behaviour exactly.

Two guards bound the curator instead, and neither can be configured away:

- A claim more than 0.05 below the confidence of the belief it would retire is
  deferred rather than enacted. Arriving later is not the same as being right.
- A claim whose newest supporting evidence predates the newest content
  correction on its target is **blocked**. A scheduled curator reads a window of
  evidence, not a diff, so Monday's sources come back around every cycle; without
  this, Wednesday's run quietly restores what a human corrected on Tuesday.

A supersession the ledger already carries **undecided** is not proposed a second
time. The successor's id is content-and-scope addressed, so an identical
re-derivation produces an identical `(target, successor)` pair and is a no-op —
nothing is written, not even the rationale evidence row — while a genuinely
different replacement body for the same belief is a different pair and still
mints, because two people disagreeing about one fact is something an operator
has to see. Those no-ops are reported as `pending_deduped`, separately from
`deferred`, so a loop standing still and a run that has quietly stopped
proposing anything do not look the same in the promote log.

When the curator refreshes **its own fact** — the successor keeps the
predecessor's `key` — the successor inherits the predecessor's confidence
instead of the `min(old, claim, 0.7)` ceiling. That ceiling is right for a
contested correction, where a replacement must not gain authority by replacing,
and wrong for the same claim restated from better evidence: approving the live
core's 33 pending proposals as-proposed would have dropped confidence on 30 of
them, mean −0.09, every one landing on 0.65 or 0.70. Run hourly, that walks the
whole corpus to 0.7. Inheritance is no-gain as well as no-loss — a more
confident claim still does not raise the fact. Cross-key curator supersessions
and every agent-issued supersession keep the ceiling.

Claims about something the corpus already covers but does not contradict are
marked rather than merged: both beliefs keep serving and each records the other
in `attributes.contradicts`, which is the field the context packet's
`contradictions` array reads.

### What the sweep retires

Two classes, each separately counted so a run reports *why* it acted:

- `expired` — past `valid_until`, or marked `superseded_by`. Unambiguous, and the
  only class permitted to retire a curated wiki fact.
- `redundant` — a same-scope restatement of a fact a newer belief already
  carries, above `--restatement-threshold` token overlap. Keeps the newest.

Two more classes, `unused` and `unhelpful`, were removed. Neither selected a
single belief across 155 scheduled runs. `unused` retired anything absent from
`retrieval_items` after 30 days, which punishes a correct fact for being rarely
needed; `unhelpful` refused to act at all until an operator set a feedback
watermark, and no operator ever set one, so the whole watermark subsystem
existed to keep a class permanently disabled. `ocbrain hygiene --set-watermark`,
`--min-age-days`, `--min-feedback-observations`, and `--unhelpful-threshold` are
gone with them; `--apply`, `--restore`, `--batch-cap`, `--restatement-threshold`,
and `--supersede` all remain.

Retirement is always a **soft** retraction, undoable with
`ocbrain hygiene --restore <belief_id>`. A hard retraction would permanently
block the belief id, and because compiled belief ids are content-addressed that
would block all future identical content. Pinned beliefs and curated wiki facts
are never touched outside the `expired` class, and `--batch-cap` bounds a run
while reporting the remainder rather than silently dropping it.

### Keeping what is promoted well-written

The slop rules still run, but not from this script. They are a write-time gate
inside the curator's `validate_claims`, a `slop_findings` block in every closeout
receipt, and a check in `scripts/wiki-lint.py` at stage 4 above. Nothing in the
promote loop needs to sweep for them. [DESLOP.md](DESLOP.md) has the rule table
and the measurements.

## Installing on macOS (launchd)

Write a plist per script under `~/Library/LaunchAgents`, then load it. Use
`StartInterval` (launchd will not start a second instance while one is alive) and
point stdout/stderr somewhere you will actually read.

```xml
<key>ProgramArguments</key>
<array>
  <string>/bin/bash</string>
  <string>-lc</string>
  <string>$HOME/Developer/ocbrain/scripts/brain-promote.sh</string>
</array>
<key>StartInterval</key><integer>3600</integer>
<key>RunAtLoad</key><false/>
```

```bash
launchctl enable "gui/$(id -u)/ai.example.ocbrain-promote"
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/ai.example.ocbrain-promote.plist
```

`launchctl enable` matters separately from `bootstrap`: a *disabled* label stays
disabled across reboots and bootstraps silently do nothing. A harvester found
dead for nine days was disabled, not merely unloaded, and `launchctl list` showed
nothing to explain it — `launchctl print-disabled "gui/$(id -u)"` did.

To remove:

```bash
launchctl bootout "gui/$(id -u)/ai.example.ocbrain-promote"
```

## Verifying it is working

```bash
# Both agents present
launchctl list | grep ocbrain

# Serving corpus is growing, not frozen
sqlite3 "$OCBRAIN_DB" \
  "SELECT COUNT(*) FROM current_beliefs WHERE serve=1 AND status='current';"

# Promotion is actually happening (not just harvesting)
sqlite3 "$OCBRAIN_DB" \
  "SELECT MAX(ts) FROM brain_events WHERE kind='compilation_decided';"

# The dense index is not stale, or retrieval silently degrades to lexical
ocbrain --db "$OCBRAIN_DB" vector-status | python3 -m json.tool | grep -E 'healthy|fresh'
```

If `compilation_decided` has no recent timestamp while `evidence_recorded` does,
the brain is in the write-only state described at the top of this document.

## The procmine stage — off unless you turn it on twice

A sixth stage runs after the vector rebuild and does nothing at all unless
`OCBRAIN_PROCMINE=1`. It extracts every runtime's tool-call history, refreshes
the procedure atlas and the episodes artifact, and mints mined gotchas as
beliefs.

Two switches, because the two risks differ:

| Variable | Default | What it unlocks |
|---|---|---|
| `OCBRAIN_PROCMINE` | `0` | The stage runs at all: extract, atlas, and a report-only mint |
| `OCBRAIN_PROCMINE_APPLY` | `0` | The mint may write beliefs |
| `OCBRAIN_PROCMINE_DIR` | `~/.ocbrain/procmine` | Where state, segments, and artifacts live |
| `OCBRAIN_PROCMINE_BUDGET_SECONDS` | `1800` | Wall-clock cap on extract and atlas |

Extraction reads every agent transcript on the machine, which is a wider read
than anything else in this script; minting adds rows to the serving corpus. An
operator may reasonably want the atlas refreshed without the second, so the
default with `OCBRAIN_PROCMINE=1` alone is a mint that prints what it *would*
write and writes nothing.

Nothing here leaves the machine. There is no model call in the mining path: the
gotcha wording is generated from the counts, so it cannot drift from the
evidence, and the mint is deterministic.

The stage is affordable on an hourly loop because extraction is incremental. Each
source file is fingerprinted by `(mtime_ns, size)` and unchanged files replay
from cached JSONL segments under `$OCBRAIN_PROCMINE_DIR/cache`. On this corpus a
cold walk is about 80 seconds and a quiet cycle is about one. A change to the
normalizer bumps `procmine.normalize.NORMALIZER_VERSION` and discards the whole
cache, because a source file's fingerprint cannot notice that the redaction rules
moved.

Writes are capped: at most twelve gotchas per run, each under a belief id derived
from `(signature, scope_id)`, so a re-mint replaces the previous row rather than
adding one. A gotcha carries `valid_until` at +45 days and is retired by the
ordinary `expired` hygiene class if the miner stops re-confirming it.

## What is deliberately not here

No autopilot, no hosted judge, no training, no stale-marking daemon, and no
promotion inside the MCP surface. The procmine stage above is not an exception:
it ships disabled, OCBrain still installs no scheduler, and an operator opts in
by loading a launchd agent exactly as for the rest of this script. Promotion stays out of the client-facing tool
set: `decide_proposal_v1` is admin-only, and these two scripts are the only
unattended writers.

The `packages/ops` maintenance commands that used to be mentioned here
(`prune_knowledge`, `archive_unreferenced_catalog`, and the `prune` / `heal` /
`liveness-check` subcommands) are deleted. They operated on the legacy
`knowledge` table and raised `no such table: knowledge` against a v1 core, so
the only correct advice was already "use `ocbrain hygiene`" — which is now the
only option.
