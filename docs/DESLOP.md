# Deslop

A knowledge store accumulates slop the way a codebase does, and it costs more:
every badly-written belief spends a retrieval slot and drags unrelated material
into answers. Deslop is the linter for stored knowledge.

It is a **write-time gate, not a sweep.** There was once an `ocbrain deslop`
command that re-ran these rules over the served corpus and offered to repair
what it found. Across 155 consecutive hourly runs it reported `actionable: 0,
repairs: [], judged: false` — every time. That is not a broken rule set; it is
the rules working one layer earlier. The same rules fired 34 `unverified_quote`,
8 `slop:fused-claims`, and 7 `slop:temporal-in-durable` rejections from inside
the curator, before a bad claim was ever stored. A sweep over an already-gated
corpus finds nothing by construction, so the sweep is gone and the gate stayed.

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

That second row is what the deleted `unactionable` rule existed to catch, and it
is the honest reason it went: answering "would a reader act differently for
knowing this?" needs a model, a model pass needs a corpus to sweep, and the
sweep never had anything to act on.

## The rules

| Rule | Fires on | Enforced |
|---|---|---|
| `fused-claims` | >1 semicolon, or >3 sentences | yes |
| `temporal-in-durable` | "now"/"currently"/"is implemented" with `lifecycle: durable` | yes |
| `current-without-expiry` | `lifecycle: current` with no `valid_until` | yes |
| `no-checkable-content` | no path, identifier, flag, figure, or named entity | **no** |

**Enforced** rules gate writes. A rule earns that only by being precise enough
that a firing is a defect rather than an opinion.

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

## Prevention at write time

Cleanup is the expensive path. The curator prompt *already* forbade fusing facts
and already forbade turning a completion receipt into eternal truth — and nothing
enforced it. A rule that lives only in a prompt is a suggestion. Three callers
turn these rules into gates:

- **Curator:** `validate_claims` runs the enforced rules and rejects a slop claim
  with the rule id as the reason, which the existing `rejected` census reports.
  `current-without-expiry` is excluded there because the expiry is assigned later
  by `claim_valid_until`, so checking it during validation would reject every
  well-formed `current` claim.
- **Closeouts:** `closeout_v1` reports findings in the receipt as
  `slop_findings`. It **reports rather than refuses** by default: a rejected
  closeout throws away the client's work, and closeout summaries are the single
  largest supply of curator-eligible evidence. Set
  `deslop.reject_closeout_slop=true` once the rules are calibrated against your
  own corpus.
- **Wiki:** `scripts/wiki-lint.py` reports `slop` per page, alongside its
  staleness checks. It lints the belief paragraph alone — a page also carries a
  caveat and a sources list, and counting those sentences against the belief made
  every multi-section page look fused. Enforced rules are findings and set the
  exit code; advisory rules print under `advisory:` and do not, because a gate
  that fails on judgement calls is a gate people learn to skip.

## Volume slop

A store can also be slopped by size. Session transcripts are imported as a
windowed excerpt — a fixed head plus a sliding tail — and the tail moves on every
append, so content-addressing mints a fresh evidence id for what is substantially
the same transcript. `file_fingerprint` is path + size + mtime with no content
hash, so any append reopens the gate.

Measured on one real brain: **2,176 history rows across 1,292 files**, with single
files holding 102 and 89 rows that share one identical head — for files no longer
on disk.

`import_source_v1` prevents it. It compares the candidate's first 2,000
characters against the newest evidence for the same `(source_uri, kind)` via
`rewindowed_evidence_id`. On a match it adopts the stored window wholesale — id
*and* body — so the belief also compares unchanged and the import is a true
no-op. Reusing only the id would still re-propose the belief every harvest,
appending the transcript to the ledger a second time. Calibrated against the live
corpus: at 2,000 characters the gate catches exactly the re-windowed rows
(102 → 1, 89 → 1) and keeps all 476 genuine head changes, which are rotated or
rewritten files whose new content is real.

There was also a reversible `deslop --volume --apply` eviction that dropped
superseded projection rows and let `sync --full` refold them. It worked, and it
is gone with the rest of the command: prevention already caps the growth, and
evidence bodies are never indexed — `search_documents` holds beliefs only — so
the residual volume costs disk, not retrieval quality.

**What is not done, and why.** The ledger cannot be compacted. The append-only
triggers, the `prev_hash`/`event_hash` chain, and `body_hash` covering the full
`body_json` all depend on the duplicated transcript text in `evidence_recorded`
staying byte-for-byte where it is: removing or slimming any event invalidates
every event after it and makes the projection unbuildable. Exporting a fresh
ledger without those events would forfeit scope tags, egress policies, every
belief, every correction, and every closeout — and would let previously retracted
beliefs become freshly compilable, because `compilation_block_reason` reads
`brain_events` directly. Not a trade worth making for 41MB.
