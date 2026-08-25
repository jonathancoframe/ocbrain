# Public golden fixtures

`golden_context_v1.json` is a deterministic synthetic contract fixture for the
core Shared Context runtime. It is not harvested user data, an evaluation of a
person, or input to any training workflow.

The golden cases exercise real MCP `brain.context` and `brain.source` calls.
They intentionally assert semantic outputs—eligible IDs, scope and delivery
counts, contradictions, source hashes, and denial boundaries—without freezing
scores, timestamps, receipt IDs, latency, or entire packets.

Every case runs on the shipped default configuration. Nothing here is certified
by switching a feature off inside the test.

For **local** delivery scope ranks rather than filters. `local-ranked-ordering`
is the case that pins this: two equally-relevant beliefs, one in the caller's
project and one in a neighbour's, must both be served and the caller's own must
come first. The same case pins the boundary that did not move — a *confidential*
belief in that neighbouring project is absent from the packet entirely.

For **hosted** delivery scope is still a filter, and the isolation cases say so.
`hosted-scope-isolation` and `hosted-cross-scope-param-ignored` both return
empty for a foreign belief; the second also pins that the deprecated
`cross_scope` argument no longer widens anything. `hosted-private-exclusion` and
`hosted-local-only-never-egresses` prove the egress gates: confidential and
`local_only` material never reaches a hosted model.

`coverage.scope_mix` reports what was actually served, by scope. It replaced
`excluded_scope_count`, which counted rows a filter dropped and would have read
zero forever once the filter was gone.

An issued source handle carries no authority of its own. Hosted expansion still
requires context matching the source's project; local expansion of a
neighbouring project's handle succeeds, because the caller was already served
the item it belongs to.

Run the focused gate with:

```bash
PYTHONPATH=src .venv/bin/pytest -q tests/test_golden_context_v1.py
```
