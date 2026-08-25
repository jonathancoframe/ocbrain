# Procedural memory in the core

Status: **design, not shipped.** Nothing in this document is implemented. The
prototype that motivates it is `scripts/procmine/`, which reads the corpus
read-only and writes nothing back; its findings are in
[PROCEDURE-ATLAS-20260824.md](PROCEDURE-ATLAS-20260824.md).

A belief answers *what is true*. A procedure answers *how this gets done here,
and how often that works*. The corpus already contains the second kind of
knowledge — 190,015 tool calls with outcomes attached — and the brain currently
throws all of it away except the one-line summary an agent writes at closeout.

The whole design constraint is this: a procedure must ride the machinery that
already exists. A parallel table with its own scope rules, its own ranker, and
its own decay would be a second brain to keep honest, and the first one is
already hard enough.

---

## 1. Shape: a belief, not a new table

`current_beliefs.belief_type` is a free-text column with no `CHECK`
(`src/ocbrain/core_v1.py:181`). Existing values in the wild are `wiki_fact`,
`curated_fact`, and `auto_compiled`. **A `procedure` needs no DDL change at
all**, and inherits scope, FTS, hybrid ranking, feedback, and hygiene for free.

The alternative costs more than it looks. `CORE_V1_TABLES`
(`src/ocbrain/core_v1.py:84`) is a closed allow-list and
`assert_core_v1_inventory` raises `RuntimeError` on any unexpected *or* missing
table, from inside `init_core_v1`. A new `procedures` table therefore means
editing the DDL *and* the allow-list, or every startup fails — and there is no
column-migration mechanism for v1 at all, so the shape is hard to change later.

So: `belief_type = "procedure"`, body is the human-readable recipe, and the
structure lives in `attributes_json`, which is already the established extension
point (`title`, `source_quality`, `valid_until`, `superseded_by`, `lifecycle`
all live there; see `src/ocbrain/hygiene.py:104` and
`src/ocbrain/mcp_v1.py:135`).

```jsonc
{
  "kind": "procedure.v1",
  "steps": [                          // abstract steps, ordered, not literal commands
    {"step": "search", "optional": false},
    {"step": "read", "optional": false},
    {"step": "edit", "optional": false},
    {"step": "test", "optional": false}
  ],
  "edges": [{"from": "edit", "to": "test", "count": 41}],
  "failure_branches": [
    {"step": "remote", "presence_lift": 3.1, "step_error_rate": 0.46,
     "note": "sessions that reached for ssh did not reach a receipt"}
  ],
  "support": {
    "episodes": 23,
    "by_grade": {"verifier-receipted": 11, "partial": 7, "blocked-or-failed": 5},
    "by_runtime": {"codex": 18, "claude-code": 5},
    "median_steps": 40
  },
  "receipts": ["close_40365bcc...", "close_4b0e1451..."],
  "mined_at": "2026-08-24T19:00:00+00:00",
  "miner_version": "procmine/0.1.0"
}
```

Two properties of that blob matter.

**Steps are abstract, never literal.** `procmine.normalize.step_class` collapses
`bash:rg -n <path:repo:coframe>` to `search`. A procedure that stored the literal
command would be a snippet library that rots the moment a path changes; a
procedure that stores `search -> read -> edit -> test` is still true next month.
Literal signatures belong in the gotcha layer (§6), where specificity is the
point.

**Receipts are closeout ids, not prose.** Every number in the blob is
recomputable from `task_closeouts` plus the traces, which is what makes a
procedure auditable in the way a hand-written belief is not.

---

## 2. Scope: unchanged, because the existing rule already fits

Scope is decided by `scope_match` (`src/ocbrain/scope.py:174`) against the set
from `ScopeContext.compatible_scope_ids` (`src/ocbrain/scope.py:93`). In-scope
scores `1.25`, cross-scope `0.15`, confidential never widens, `global` is always
`1.0`. A procedure is scored by exactly that function with no new branch.

Write scope should use `resolve_write_scope` (`src/ocbrain/scope.py:135`) with
one deviation, and it is the deviation `auto_compile_scope`
(`src/ocbrain/mcp_v1.py:66`) already makes for unattended promotion: prefer the
*broadest shared* scope — project, then repo, then client — rather than the
narrowest. The reason is stronger for procedures than for beliefs. A procedure is
mined *from* several sessions across several runtimes; scoping it to the session
that happened to be last would hide it from everyone it was learned from.

A procedure is never `global:doctrine`. Doctrine is promoted out-of-band by the
curator, and a mined artifact must not walk itself up the ladder.

---

## 3. Retrieval: when a procedure should beat a belief

Ranking is `search_core_v1` (`src/ocbrain/core_v1.py:1590`), scoring at
`src/ocbrain/core_v1.py:1814`:

```
score = rrf * scope_weight
      * (0.85 + 0.15*confidence) * (0.85 + 0.15*quality) * (0.99 + 0.01*recency)
      * (1 + feedback_boost) * (1 + exact_boost)
```

No new term is needed. Two existing dials carry the procedure signal:

- **`confidence`** = the mined success rate at artifact-linked-or-better,
  shrunk toward 0.5 by support. A procedure with 23 episodes and a 78% receipt
  rate earns a higher confidence than one with 6 episodes and the same rate, and
  the existing `confidence_band` derivation (`src/ocbrain/core_v1.py:2246`)
  labels it without change.
- **`attributes_json.source_quality`**, read by `_source_quality`
  (`src/ocbrain/core_v1.py:2130`), carries *how the labels were obtained* —
  identity-joined episodes score higher than temporally-joined ones. The atlas
  shows why that distinction is not academic: only 38 of 1,105 closeouts join by
  identity.

**When a procedure should win.** A belief answers a question; a procedure answers
a request to act. The query terms differ enough ("what is the sticky bucketing
era" vs "how do I ship an SDK PR") that the existing lexical + dense arms already
separate them, and the `kind` column in the FTS index is weighted `0.25` against
body's `1.0` (`src/ocbrain/core_v1.py:1649`), so a query naming the kind gets a
nudge without a new code path.

The one thing worth adding is a **retrieval-time cap**: at most one procedure per
packet unless the query explicitly asks for more. A procedure body is long, and
two of them would crowd out the beliefs that give them context.

**Abstention needs no new rule and that is the point.** The floors at
`src/ocbrain/core_v1.py:1784` — dense-only candidates need cosine ≥ 0.55, lexical
hits need ≥ 0.30 unless the query names the belief — already refuse to serve a
weak match, and the comment at `src/ocbrain/core_v1.py:37` states the intent
("an honest empty packet over same-scope filler"). A mined procedure is subject
to the same gates.

There is one hazard. Those floors stand down when the dense arm is unhealthy
(`dense_arm_healthy`, `src/ocbrain/core_v1.py:1733`), so with the embedder down a
procedure could be served on a single shared token. Procedures should be
**excluded entirely in degraded mode** rather than served speculatively: a wrong
belief is a wrong sentence, a wrong procedure is a wrong afternoon.

The miner's own abstention is upstream of all of this and is the more important
gate. `procmine.dag` refuses to emit a procedure below 5 episodes, below a 3-step
shared subsequence, or below 50% family coverage, and reports every refusal with
its reason. On the current corpus that means **1 procedure and 73 abstentions**.
A design that produced 74 would be the failure case, not the success case.

---

## 4. Statistics: the one genuinely new thing

This is where the design touches something the core has never done.

**Nothing in OCBrain maintains a mutable statistic on an object row.** Every
aggregate is derived at read time by joining `retrieval_items` to
`retrieval_uses` — see `_retrieval_feedback_scores`
(`src/ocbrain/core_v1.py:2149`) and the duplicated CASE expression in
`_unhelpful_targets` (`src/ocbrain/hygiene.py:163`). `current_beliefs` has no
use-count, no success-count, no rolling anything.

Two options, and the second is right.

**Option A — derive on read.** Compute procedure stats by joining closeouts at
query time. Consistent with existing practice, no new mutation class, and no
staleness. But the join is closeouts → traces → mined patterns, which is the
expensive half of `procmine` and not a query-time operation.

**Option B — recompute out-of-band, write the snapshot.** The miner runs on a
schedule (like the curator and `ocbrain hygiene` already do), recomputes support
from scratch, and republishes the whole `attributes_json` blob. Statistics are
never incremented, only replaced, so there is no drift and no partial-update
class of bug, and it stays consistent with the rule that
`current_beliefs` is a rebuildable projection of the append-only event log
(`src/ocbrain/core_v1.py:1`). The write goes through the existing
`correction_recorded` event path, not a bespoke UPDATE.

**Closing the loop through `brain.closeout`.** `record_closeout`
(`src/ocbrain/closeout.py:20`) already walks the retrievals it was linked to
and writes back `affected_decision` at `src/ocbrain/closeout.py:118`-127. That
column is written and **never read by ranking** — a dangling signal, and the
closest existing thing to a procedure-outcome hook.

The proposal is to extend exactly that loop, not to add a parallel one: when a
closeout links a retrieval that served a procedure, record the pairing. That
gives the miner a *direct* label — this procedure was retrieved, and here is what
happened next — instead of the reconstructed temporal join the prototype has to
use. The atlas quantifies what that would be worth: reconstruction currently
yields 108 usable episodes out of 1,105 closeouts. A served-procedure link would
be exact.

Note that a procedure's `outcomes` can ride the existing
`ocbrain.outcome.v1` envelope (`src/ocbrain/closeout.py:219`), which already
keeps metric, value, baseline, and uncertainty as separate components rather than
collapsing to one scalar reward. That is the right shape for per-run procedure
telemetry and it is already portable.

---

## 5. Decay and retirement: mostly free, one exemption needed

Existing machinery covers most of it:

- **Recency** — `_recency_score` (`src/ocbrain/core_v1.py:2138`),
  `exp(-days/365)`, weighted at 1%. Weak, which is correct for a procedure: a
  stable workflow does not get less true in a month.
- **Expiry** — `attributes_json.valid_until`, swept by `_expired_targets`
  (`src/ocbrain/hygiene.py:99`). Procedures should be `lifecycle: current` with
  a TTL, unlike auto-compiled beliefs which hardcode `durable`
  (`src/ocbrain/mcp_v1.py:141`). A procedure describes tooling, and tooling
  changes. If the miner has not re-confirmed it in a cycle, it should expire.
- **Supersession** — `superseded_by`, same sweep. The miner sets it when a new
  mining run replaces a procedure over the same family.

One change is required. `_unused_targets` (`src/ocbrain/hygiene.py:134`) retires
anything never present in `retrieval_items` after 30 days, exempting only
`pinned=0` and `belief_type != 'wiki_fact'` (`src/ocbrain/hygiene.py:143`).
A correct, rarely-needed procedure — the one for a quarterly task — would be
retired for the crime of not coming up. Procedures need the same exemption
`wiki_fact` has, because their support comes from episodes, not from retrievals.

They should stay fully subject to `_unhelpful_targets`
(`src/ocbrain/hygiene.py:163`): a procedure people actively mark unhelpful has
earned retirement.

---

## 6. Gotchas are a separate, lower-ceremony layer

The atlas's most useful output is not the procedures. It is the failure/repair
pairs and step reliability — findings like "`tool:wait_agent` times out on 207 of
357 calls, and the recurring next move is `tool:list_agents`, which works 92% of
the time".

Those are not procedures. They are **step-scoped**, they need no closeout, and
they have three orders of magnitude more support. They fit the existing belief
shape with no new kind at all: a one-sentence claim, a scope, evidence ids, and a
confidence — exactly what `auto_compile_evidence` (`src/ocbrain/mcp_v1.py:135`)
already mints.

So the mining pipeline produces two outputs at different ceremony levels:

| | procedure | gotcha |
|---|---|---|
| unit | task family | one step signature |
| needs closeouts | yes | no |
| support on this corpus | 1 | 12 above threshold |
| storage | `belief_type='procedure'` | ordinary auto-compiled belief |
| decay | TTL + supersession | re-mined each cycle |

Shipping the gotcha layer first is the lower-risk path, and it is the half that
already has the evidence.

---

## 7. MCP surface

Tools are a plain list of dicts in `tool_list` (`src/ocbrain/mcp.py:1321`),
filtered by `RUNTIME_TOOLS` (`src/ocbrain/mcp.py:101`) and dispatched by an
if-chain on name. No new tool is needed for reads: a procedure served by
`brain.context` is just another item in the packet, which is the whole argument
for making it a belief.

A `brain.procedures` listing tool would be a convenience, not a requirement, and
would touch four places (list literal, `RUNTIME_TOOLS`, dispatch chain,
annotations).

Any new field must use the coercion helpers added in `caf8349` —
`string_list`, `object_list`, `bool_arg`, `optional_string`
(`src/ocbrain/mcp.py:1931`-2043) — rather than raw `arguments.get(...)`, or it
reintroduces the `bool("false") is True` class of bug that commit fixed.

**A finding from the trace corpus that bears directly on this.** `brain.closeout`
called from Claude Code fails on **68 of 112 calls (61%)**, and it was 16/16
before 2026-08-18. `caf8349` fixed the server-side half; the residual, dominant
after the fix, is a *client-side* schema rejection:

```
MCP error -32602: Input validation error: Invalid arguments for tool brain.closeout:
  path: ["verifier_refs", 0, "uri"]  expected string, received undefined
```

`provider_safe_schema` (`src/ocbrain/mcp.py:173`) recurses into array items and
sets `required = list(properties)` at every level. Inside the `verifier_refs`
item schema that makes `uri` required, though the tool declares only `status`
required — and `nullable_schema` makes the *type* accept null, which does not
help when the key is absent entirely. Any new nested object in a tool schema
inherits the same defect. This is out of scope for the prototype PR and wants its
own fix.

---

## 8. Privacy

Signatures are shapes, never payloads. `procmine.normalize` runs every free-text
fragment through `redact_secrets` (`src/ocbrain/text.py:227`) and then re-checks
with `find_probable_secret_leaks`, dropping anything still flagged rather than
shipping it — the same redact-then-drop pattern `retrieve.py:331` already uses
for file-sourced snippets. Paths are replaced by classes (`<path:repo:coframe>`),
error messages are reduced to a redacted, id-stripped 160-character fingerprint.

A procedure inherits the belief egress rules unchanged: `egress_allowed`
(`src/ocbrain/scope.py:204`) and the delivery gate at
`src/ocbrain/core_v1.py:2079`. Nothing about a procedure is exportable that a
belief in the same scope would not be.

---

## 9. What would have to be true to ship this

1. **The join has to stop being reconstructed.** ~~44% join yield, 38 identity
   joins out of 1,105.~~ **Landed for new rows.** The server no longer asks the
   model who it is: at `initialize` it mints a `server_connection_id` and reads
   the MCP child's own environment, and `task_closeouts` /`retrieval_uses` carry
   `client_session_hint` beside the model-supplied `session_id`
   (`src/ocbrain/provenance.py`). On Claude Code that hint is byte-identical to
   the transcript filename `claude_code.py:47` keys on, so the identity tier
   joins directly. Three caveats the design keeps visible rather than papering
   over: the hint is harness-attested and never server-verified; its stability
   across `/resume`, `/clear`, and compaction is unverified; and a subagent
   inherits its parent's id, so a stamped hint resolves to the parent
   transcript. Hermes multiplexes every session over one MCP child per gateway
   profile and cannot supply a session id at all — its consolation is an exact
   `client_runtime_key` from `OCBRAIN_CLIENT`. **No backfill**: `task_closeouts`
   is append-only, so the historical corpus still needs the tiered join.
2. **Cursor needs a real exporter.** 57 cursor closeouts, 0 traces, because
   `scripts/export-cursor-chats.py` exports chat bubbles and not the tool log.
3. **A scheduled miner**, alongside the curator and `ocbrain hygiene`, since
   statistics are recomputed rather than incremented.
4. **The `unused` hygiene exemption**, or correct procedures get swept at 30 days.
5. **Gotchas first.** They need none of items 1-4.
