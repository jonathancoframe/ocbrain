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

`brain-sync.sh` never promotes anything. It passes `--evidence-only`, and
`automatic_activation` is off by default, so a brain running only the harvester
accumulates evidence that no retrieval can ever return. That is the state a real
deployment was found in: a healthy write path, ~1,900 evidence objects, and a
serving corpus frozen for two weeks at whatever was last curated by hand.

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

Six ordered stages: curate → hygiene → deslop → rematerialize wiki → lint →
rebuild vectors. Each continues on failure so one bad stage cannot strand the
rest.

**Step 1 is the only hosted call by default**, and it is digest-gated per
project: a project whose eligible evidence has not changed since the last run is
skipped without contacting the provider, and a cycle where nothing moved is
therefore free. That is what makes an hourly schedule reasonable across many
project scopes. Deslop runs mechanical-only unless you opt in, so it stays free
and its findings are reproducible across runs.

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
| `OCBRAIN_HYGIENE_CLASSES` | all three | e.g. `--class expired` to restrict |
| `OCBRAIN_HYGIENE_APPLY` | `0` (report only) | `1` lets the sweep retire beliefs |
| `OCBRAIN_DESLOP_JUDGE` | `0` | `1` adds the actionability pass — one hosted call per cycle |
| `OCBRAIN_DESLOP_APPLY` | `0` (report only) | `1` lets it repair — one hosted call per repaired belief |
| `OCBRAIN_PROMOTE_BUDGET_SECONDS` | `1800` | Ceiling on the curate stage |

The API key is read from the environment, falling back to `~/.common`. Only the
variable *name* is ever configured; the value is never persisted by OCBrain.

An hourly interval is ample — evidence arrives at human pace.

### What the curator sends

Only evidence with `public`/`internal` visibility and `hosted_ok` egress policy,
in a configured project scope, bounded to 4,000 characters per body. A scope
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

### What the sweep retires

Three classes, each separately counted so a run reports *why* it acted:

- `expired` — past `valid_until`, or marked `superseded_by`. Unambiguous, and the
  only class permitted to retire a curated wiki fact.
- `unused` — never returned by any retrieval, older than `--min-age-days`.
- `unhelpful` — net-negative retrieval feedback. **Refuses to run until a
  watermark is set** (`ocbrain hygiene --set-watermark`), and then counts only
  feedback recorded after it. Verdicts collected while a ranker was serving a
  belief for unrelated queries describe the ranker, not the belief; acting on
  them retires good facts for the ranker's mistakes. Set the watermark after any
  ranking change.

### What deslop reports

Two things, and neither is a deletion:

- **Belief findings.** The mechanical rules (`fused-claims`,
  `temporal-in-durable`, `current-without-expiry`, `no-checkable-content`) plus,
  with `OCBRAIN_DESLOP_JUDGE=1`, the model-judged `unactionable` rule. With
  `OCBRAIN_DESLOP_APPLY=1` findings are repaired by rewriting or splitting; a
  repair may only subtract or reorganize the original's own words, and one that
  adds a token is rejected before anything is written.
  `deslop.max_repairs_per_run` caps an unattended run.
- **Volume.** Session transcripts are imported as a sliding window, so an append
  mints a fresh evidence row for the same transcript. The report names how many
  projection rows and megabytes that costs. `deslop --volume --apply` evicts
  them, and `ocbrain sync --full` refolds every row from the ledger — a cache
  eviction, not a deletion. See [DESLOP.md](DESLOP.md).

Only *enforced* rules and the judged verdict may act unattended. Advisory
findings report and wait for a human, because a rule that cannot distinguish a
defect from a judgement call must not retire a belief on its own.

Retirement is always a **soft** retraction, undoable with
`ocbrain hygiene --restore <belief_id>`. A hard retraction would permanently
block the belief id, and because auto-compiled ids are content-addressed that
would block all future identical content. Pinned beliefs and curated wiki facts
are never touched outside the `expired` class, and `--batch-cap` bounds a run
while reporting the remainder rather than silently dropping it.

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

## What is deliberately not here

No autopilot, no hosted judge, no training, no stale-marking daemon, and no
promotion inside the MCP surface. Promotion stays out of the client-facing tool
set: `decide_proposal_v1` is admin-only, and these scripts are the only
unattended writers. `packages/ops` maintenance (`prune_knowledge`,
`archive_unreferenced_catalog`) operates on the **legacy** `knowledge` table and
raises `no such table: knowledge` against a v1 core — use `ocbrain hygiene`.
