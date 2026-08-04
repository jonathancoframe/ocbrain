# Deslop

A knowledge store accumulates slop the way a codebase does, and it costs more:
every badly-written belief spends a retrieval slot and drags unrelated material
into answers. `ocbrain deslop` is the linter for stored knowledge.

The structure is borrowed from code linting deliberately — **a gate that sits at
zero**, so a finding is a regression, plus **an advisory pass whose findings are
judgement calls**, so nobody auto-applies them.

```bash
ocbrain --db ~/.ocbrain/ocbrain.sqlite deslop --mechanical-only   # free, deterministic
ocbrain --db ~/.ocbrain/ocbrain.sqlite deslop                     # adds the judged rule
ocbrain --db ~/.ocbrain/ocbrain.sqlite deslop --apply             # repair the findings
ocbrain --db ~/.ocbrain/ocbrain.sqlite deslop --volume            # re-windowed evidence
```

## What counts as slop

`shape` is not the signal. The first version of this design assumed
receipt-shaped beliefs ("Built…", "Completed…") were the problem. Retrieval
feedback on a real brain says otherwise: receipt-shaped beliefs ran **274 good :
28 bad**, fact-shaped **964 : 191**. The discriminator is whether a reader could
*act* on the belief.

Two beliefs, both receipt-shaped, illustrate it:

| Belief | Feedback | Why |
|---|---|---|
| "The durable task-DAG kernel is implemented and verified: one task is registered and exclusively leased…" | fine | states a durable system property |
| "Built, visually verified, and evidence-sealed an 18-page founder report…" | **21 good / 33 bad**, worst in the corpus | records that a thing happened once |

## The rules

| Rule | Fires on | Repair | Enforced |
|---|---|---|---|
| `fused-claims` | >1 semicolon, or >3 sentences | split | yes |
| `temporal-in-durable` | "now"/"currently"/"is implemented" with `lifecycle: durable` | rewrite | yes |
| `current-without-expiry` | `lifecycle: current` with no `valid_until` | rewrite | yes |
| `no-checkable-content` | no path, identifier, flag, figure, or named entity | drop | **no** |
| `unactionable` | model judgement: would a reader act differently? | drop | judged |

**Enforced** rules gate writes and may be repaired unattended. A rule earns that
only by being precise enough that a firing is a defect rather than an opinion.

`no-checkable-content` deliberately does not qualify. A capitalised proper noun
at the start of a sentence is indistinguishable from a common word by pattern
alone, so *"Jonathan wants short direct answers"* — a genuinely actionable
preference — fires exactly as vague prose does. It reports, and waits for a
human.

Two calibration notes, both from being wrong first:

- `MAX_SENTENCES` is **3**, not 2. The curator prompt asks for "1-3 short
  sentences"; a stricter bar flags beliefs for meeting the contract they were
  written to.
- An earlier rule flagged any precise figure lacking an as-of date. It could not
  tell a stable configured value from a rotting measurement — a 600GB budget does
  not age, "542 experiments" does — so it fired on both. `current-without-expiry`
  is the precise version of the same concern: it reads lifecycle metadata rather
  than guessing from prose.

## Repair, not deletion

A fused belief holds several real facts badly packaged. Splitting it keeps the
knowledge and fixes the packaging. The safety rule is mechanical:

> **A repair may only subtract or reorganize, never add.**

Every significant token in the repaired body — or the union of the split bodies —
must already appear in the original. A repair that introduces a token is rejected
before anything is written. This is what makes rewriting safe *without*
re-verifying the evidence the original cited: a repair cannot introduce a claim
the original did not make. The curator's verbatim-quote gate cannot apply here,
because a repaired belief derives from a belief rather than from evidence.

Each action reuses machinery that already exists, so nothing invents new event
semantics and every outcome is reversible:

- **rewrite** re-proposes the same key, updating the belief in place.
- **split** mints one belief per fact — each inheriting the original's
  `evidence_ids`, so `brain.source` keeps working — then supersedes the original.
  The existing `expired` hygiene class retires it on the next sweep, so a bad
  split is visible next to its source before the original goes away.
- **drop** soft-retracts, exactly as hygiene does.

`ocbrain hygiene --restore <belief_id>` undoes any of them.

## Prevention at write time

Cleanup is the expensive path. The curator prompt *already* forbade fusing facts
and already forbade turning a completion receipt into eternal truth — and nothing
enforced it. A rule that lives only in a prompt is a suggestion.

- **Curator:** `validate_claims` runs the enforced rules and rejects a slop claim
  with the rule id as the reason, which the existing `rejected` census reports.
  `current-without-expiry` is excluded there because the expiry is assigned later.
- **Closeouts:** `closeout_v1` reports findings in the receipt as
  `slop_findings`. It **reports rather than refuses** by default: a rejected
  closeout throws away the client's work, and closeout summaries are the single
  largest supply of curator-eligible evidence. Set
  `deslop.reject_closeout_slop=true` once the rules are calibrated against your
  own corpus.
- **Wiki:** `scripts/wiki-lint.py` reports `slop` per page, alongside its
  staleness checks.

## Volume slop

A store can also be slopped by size. Session transcripts are imported as a
windowed excerpt — a fixed head plus a sliding tail — and the tail moves on every
append, so content-addressing mints a fresh evidence id for what is substantially
the same transcript. `file_fingerprint` is path + size + mtime with no content
hash, so any append reopens the gate.

Measured on one real brain: **2,176 history rows across 1,292 files**, with single
files holding 102 and 89 rows that share one identical head — for files no longer
on disk.

**Prevention.** `import_source_v1` compares the candidate's first 2,000
characters against the newest evidence for the same `(source_uri, kind)`. On a
match it adopts the stored window wholesale — id *and* body — so the belief also
compares unchanged and the import is a true no-op. Reusing only the id would
still re-propose the belief every harvest, appending the transcript to the ledger
a second time. Calibrated against the live corpus: at 2,000 characters the gate
catches exactly the re-windowed rows (102 → 1, 89 → 1) and keeps all 476 genuine
head changes, which are rotated or rewritten files whose new content is real.

**Reversible eviction.** `deslop --volume --apply` drops projection rows that are
all three of: not the newest for their `(source_uri, kind)`, not named by an
issued context handle, and not cited by a belief. On the live brain that is 892
rows / 14.0MB. This is a **cache eviction, not a deletion** — `evidence_objects`
is derived, and `ocbrain sync --full` refolds every row from the ledger. Verified
end to end: 2,578 rows → evict 892 → 1,686 → `sync --full` → 2,578 exactly, with
handle expansion unchanged at every step.

An issued handle names its evidence inside `locator_json`, not in `object_id` —
`object_id` holds the belief the evidence supports. Matching the wrong column
makes the exemption silently protect nothing.

**What is not done, and why.** The ledger cannot be compacted. The append-only
triggers, the `prev_hash`/`event_hash` chain, and `body_hash` covering the full
`body_json` make the duplicated transcript text in `evidence_recorded` load
bearing: removing or slimming any event invalidates every event after it and
makes the projection unbuildable. Exporting a fresh ledger without those events
would forfeit scope tags, egress policies, every belief, every correction, and
every closeout — and would let previously retracted beliefs become freshly
compilable, because `compilation_block_reason` reads `brain_events` directly.
Not a trade worth making for 41MB.

Worth stating plainly: **evidence bodies are never indexed.** `search_documents`
holds beliefs only. That volume costs disk, not retrieval quality, which is why
prevention plus reversible eviction is proportionate and projection surgery is
not.

## In the scheduled loop

`scripts/brain-promote.sh` runs deslop mechanically after hygiene, so a scheduled
cycle stays free and its findings are reproducible. Opt in further with:

```bash
OCBRAIN_DESLOP_JUDGE=1   # add the actionability pass (one hosted call per cycle)
OCBRAIN_DESLOP_APPLY=1   # repair findings (one hosted call per repaired belief)
```

`deslop.max_repairs_per_run` caps an unattended run, so a rule that starts
over-firing damages a handful of beliefs rather than the corpus.
