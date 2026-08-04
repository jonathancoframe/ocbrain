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

Five ordered stages: curate → hygiene → rematerialize wiki → lint → rebuild
vectors. Each continues on failure so one bad stage cannot strand the rest.

**Step 1 is the only hosted call**, and it is digest-gated: if no eligible
evidence changed since the last run, it exits without contacting the provider. A
quiet cycle is therefore free, which is what makes an hourly schedule reasonable.

Environment:

| Variable | Default | Notes |
|---|---|---|
| `OCBRAIN_PROMOTE_PROVIDER` | `anthropic` | Also `openai`, `moonshot` |
| `OCBRAIN_PROMOTE_PROJECT` | `workspace` | |
| `OCBRAIN_PROMOTE_MAX_BELIEFS` | `24` | Upper bound per run; fewer is better |
| `OCBRAIN_HYGIENE_CLASSES` | all three | e.g. `--class expired` to restrict |
| `OCBRAIN_HYGIENE_APPLY` | `0` (report only) | `1` lets the sweep retire beliefs |
| `OCBRAIN_PROMOTE_BUDGET_SECONDS` | `1800` | Ceiling on the curate stage |

The API key is read from the environment, falling back to `~/.common`. Only the
variable *name* is ever configured; the value is never persisted by OCBrain.

An hourly interval is ample — evidence arrives at human pace.

### What the curator sends

Only evidence with `public`/`internal` visibility and `hosted_ok` egress policy,
in one project scope, bounded to 4,000 characters per body. Raw transcripts are
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
