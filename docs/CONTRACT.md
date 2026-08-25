# OCBrain product contract

This is the current authority boundary for OCBrain v1. Historical design and
release documents are evidence about earlier systems, not operating orders.

## Purpose

OCBrain gives Codex, Claude Code, OpenClaw, and future local runtimes one
source-backed memory of relevant evidence, decisions, actions, and outcomes.
It helps an agent decide what to inspect and what to do next; it does not itself
become an autonomous job runner.

The product loop is:

```text
evidence lake → bounded shared context → source inspection → work
             → retrieval feedback + verified closeout → better future priors
```

## Durable truth

- Raw events and evidence are append-only and content-addressed.
- Current beliefs are rebuildable projections, not a second authority.
- Scope, provenance, actor/runtime, time, and source identity stay attached.
- Corrections, tombstones, and promotions are later events, never silent edits.
- Retrieval and closeout receipts preserve what context was used and what
  happened afterward.
- Actions preserve physical mechanism and local semantic role.
- Outcomes preserve metric vectors, baselines, counterfactuals, uncertainty,
  verifier evidence, and local interpretation.
- Derived embeddings, FTS, classifications, rankings, summaries, and rewards may
  be replaced without erasing the original record.

## Runtime authority

Ordinary clients may:

- retrieve a scoped context packet;
- expand an issued source within its scope and size bound;
- search, digest, or fetch a serving object through lifecycle/scope gates;
- report retrieval usefulness;
- append narrowly scoped evidence;
- append an outcome closeout;
- supersede one serving belief they have proved wrong, within the limits below.

Ordinary clients may not directly promote belief, widen scope, call hosted
models, start training, schedule maintenance, page an operator, or perform a
destructive lifecycle change.

The admin profile adds local correction, proposal-decision, preview, and
tombstone controls. Admin mode is explicit and local. It still does not imply
authority for hosted egress, training, scheduling, package publication, or an
external side effect.

## Agent supersession

Supersession is a **named, scope-bound, rate-limited correction**. It is not
unattended promotion, and the distinction is what makes it safe to put in the
runtime profile:

- **Named.** Every supersession records the calling actor, the server-minted
  connection id, and the harness-attested session hint on both the correction
  event and its evidence. An anonymous supersession is not possible.
- **Scope-bound.** The replacement inherits the superseded belief's scope
  byte-for-byte. There is no argument, and no configuration, that lets a
  supersession widen reach or move a fact into doctrine. Promotion remains a
  separate `scope_promoted` event with a named approver.
- **Bounded in authority.** Confidence is capped at `min(old, 0.7)`. A newer
  claim does not outrank an older one merely by arriving later.
- **Rate-limited.** A caller has a bounded number of direct supersessions per
  24 hours. Overflow is routed to review, never refused: an agent always has
  somewhere to put a correction.
- **Reviewed where it matters.** Doctrine and pinned beliefs are never replaced
  unattended. Those become an undecided proposal that only `brain.proposal_decide`
  can complete, and `brain.digest` reports the queue depth so it cannot silently
  accumulate. An operator may route *every* supersession to review with
  `OCBRAIN_SUPERSEDE_TIER=pending_all`.
- **Additive.** Nothing is deleted. The superseded belief keeps its body, its
  evidence, its feedback, and its retrieval history; only its service stops, and
  its era is stamped so a reader can tell what was true when.

## Scope and privacy

- Use the narrowest known project/repo/client/task/session scope on ingest.
- Global doctrine must be explicit; it is never inferred from broad prose.
- Confidential foreign scopes are excluded before ranking and source issuance.
- Legacy placeholder scope is quarantined as `legacy_unscoped` until explicitly
  reclassified.
- Egress policy is separate from local visibility. Local relevance does not
  authorize hosted disclosure.
- External pages and transcript text are evidence, never instructions.

## One distribution

`ocbrain` is the whole product. It owns the event chain, projections, retrieval,
source handles, closeouts, egress audits, backup/restore, migration, the
public-safety scanner, and MCP. Nothing else is installable and nothing extends
the CLI from outside the wheel.

The `ocbrain-training` and `ocbrain-ops` companion packages are deleted. They
were the standing answer to "where does the risky work live", and the honest
answer turned out to be "nowhere": every table they owned held zero rows. A
boundary that separates the core from an empty package is not a boundary, it is
a second thing to keep audited.

## Training authority

There is no training code, no dataset pipeline, and no prepared pack. Authority
is therefore not a runtime setting to check but a change to this repository:
adding a trainer means adding the code, and that requires the same contract
review as any other new mutation or egress authority.

For the record, since it is the reason the pipeline was written and then
removed: the one pack that reached review, pilot-v3, failed it 67 pass / 83
fail, and the review was an AI-delegated one — remediation data, never the
named-human approval the gate demanded. No credential, prepared command,
manifest, AI review, or passing test suite was ever going to substitute for
that.

## Migration authority

- Plan mode is read-only and creates no outputs.
- Migration reads a coherent source snapshot and writes only fresh paths.
- The exact verified legacy event prefix is preserved.
- Every source table is copied, transformed, extracted, or explicitly accounted
  for in a signed/hash-addressed manifest.
- Corrupt event history aborts migration; it is never silently folded into a
  replacement truth.
- The live source is never modified, replaced, or repointed automatically.
- Activation is a later explicit, reversible operation after migration
  verification. Fresh-client acceptance then decides whether that pointer is
  retained or rolled back.

## Completion evidence

A change is complete only with evidence proportionate to risk: focused tests,
full tests, static checks, schema and chain verification, output hashes, package
inventory, clean-environment imports, and real client round trips where the
runtime boundary changed.

For v1, configuration probes alone are insufficient. Codex, Claude Code, and
OpenClaw must each actually perform
`brain.context → brain.source → brain.feedback → brain.closeout` against the
same core.
