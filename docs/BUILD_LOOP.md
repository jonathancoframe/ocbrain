# ocbrain Long-Running Build Loop

Date opened: 2026-06-21 15:50 PDT
Status: **closed.** Loops 0-4 shipped and became the v1 core. Loops 5 and 6 were
built and then deleted; the record of what they were and why they went is kept
below rather than erased, because "we tried this and measured it" is the useful
part.

## Why This Exists

The first `ocbrain` pass produced a working local prototype too quickly to count as the
assignment being complete. Treat commit `b554799` as seed code and baseline evidence,
not as the finished OpenClawBrain.

This loop turns the prototype into a real consolidation governor through repeated
measure-build-check cycles. Each cycle must leave a small artifact, runnable evidence,
and a next-step pointer that survives context compaction.

## Operating Rules

- Work in loops, not one giant closeout.
- Keep `TASKS.md` as the principal truth for whether the build is active.
- Keep this file as the repo-local loop contract.
- Put worker outputs in the operator-owned task-artifact directory outside this repo.
- Put compact machine-readable status in the operator-owned task-status directory.
- Prefer dry-run and proposal output until review quality is proven.
- Do not create cron jobs, mutate live memory/wiki/skills/policy, or publish remotely without Jonathan's explicit approval.
- Each loop must end with evidence: tests, corpus stats, sample audits, or integration proof.

## Loop Cadence

1. Check in with Jonathan at the start of each major loop or when blocked.
2. Run a bounded audit or build step.
3. Write an artifact with findings, evidence, and next action.
4. Update the status file.
5. Update `TASKS.md` only at phase boundaries.
6. Commit meaningful repo changes in small commits.

## Phase Gates

### Loop 0: Reopen And Audit

Goal: demote the fast build to a prototype, inspect what exists, and identify the
highest-risk gaps.

Exit evidence:

- active `TASKS.md` entry
- status file initialized
- at least three independent audit artifacts
- repo tests still pass

### Loop 1: Final Core Quality Harness

Goal: keep tests focused on the final evidence/knowledge core: typed values,
identity-spine dedupe, source-backed rendering, privacy gates, and human-gated
capability proposals.

Exit evidence:

- repeatable test/ruff/compileall commands
- tests for evidence/knowledge/link behavior
- tests for human-gated proposal-first behavior
- tests proving legacy table removal

### Loop 2: Knowledge Proposal UX

Goal: make human-gated knowledge practical to inspect before any live writes.

Exit evidence:

- proposal markdown for human-gated knowledge rows
- stale/supersession operations over `knowledge`
- evidence links included in proposals
- no live skill/policy/wiki/memory apply

### Loop 3: Runtime Integration Proof

Goal: prove Codex/OpenClaw/Claude can consume compact native excerpts and MCP search
without bloating context or bypassing native instruction surfaces.

Exit evidence:

- generated AGENTS/CLAUDE/OpenClaw excerpt samples
- MCP smoke with representative queries
- documented install/config path
- no live mutation unless explicitly approved

### Loop 4: Runtime Install And Public Surface

Goal: publish and install the lightweight brain, then update public surfaces to
point at `ocbrain`.

Exit evidence:

- public GitHub repo
- local MCP install for Codex/Claude/OpenClaw
- MCP smoke proof
- public site updates

### Loop 5: Loop-Aware Brain Ingest (built, then deleted)

Goal was to make ocbrain understand autonomous loop result envelopes without
becoming the loop runner: an `ocbrain.loop_result.v1` envelope, a
`brain-loop-ingest` console script with dry-run and `--apply` modes, and a
`family_scores` rollup derived from loop-tagged rows.

It was built and it met its exit evidence. It is gone anyway. The console
script, the `loop-ingest` subcommand, and the `liveness-check` command went with
`packages/ops`, and the reason is that `loop_liveness` and `family_scores` never
held a row. A safety surface with no traffic is not a safety surface, it is
untested code sitting on the audit path.

### Loop 6: Scheduler Readiness (superseded)

Goal was to prepare a scheduled consolidation loop without enabling it. What
actually shipped is smaller and is the current answer: OCBrain installs no
scheduler, and an operator who wants one opts in explicitly to
`scripts/brain-sync.sh` and `scripts/brain-promote.sh`. See
[SCHEDULED_MAINTENANCE.md](SCHEDULED_MAINTENANCE.md). The autopilot and
stallcheck plists that Loop 6 anticipated were written, ran, and were deleted
along with every table they wrote to, all of which were empty.

## Current Next Action

None. The loop is closed. Ongoing work is tracked as ordinary issues and pull
requests against `main`.
