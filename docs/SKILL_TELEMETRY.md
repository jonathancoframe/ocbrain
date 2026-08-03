# Skill-Usage Telemetry Envelope

Status: convention. Code constants and the validator live in
`src/ocbrain/events.py` (`SKILL_TELEMETRY_*`, `validate_skill_telemetry`).

Agents that build, install, load, and retire *skills* (reusable procedural
packages) should report lifecycle observations to OCBrain so later sessions
can answer "which skills were used, from which exact source, and did they
work?" without trusting prose.

## Rules

1. **Metadata only.** The envelope carries hashes, URIs, and ids — never
   skill bodies, prompts, transcripts, messages, or tool output. The
   validator rejects forbidden content fields outright.
2. **Evidence, not belief.** Telemetry rides `brain.ingest` with
   `kind` set to one of the telemetry kinds below and `body` set to the
   canonical JSON envelope. It lands in the append-only evidence ledger; it
   is never auto-promoted to current truth.
3. **Locator required.** Every event names the skill by `skill_id` plus at
   least one content locator (`source_commit`, `tree_sha256`, or
   `skill_uri`) so the exact artifact is reproducible.
4. `brain.ingest` validates telemetry again at the trust boundary. Producers
   should also call `validate_skill_telemetry(envelope)` before ingest for an
   earlier error; it accepts a dict or a JSON string and raises `ValueError`
   on violation.

## Envelope

`schema_version`: `ocbrain.skill_telemetry.v1`

| field | required | meaning |
| --- | --- | --- |
| `schema_version` | yes | `ocbrain.skill_telemetry.v1` |
| `kind` | yes | one of the kinds below |
| `skill_id` | yes | stable skill identifier (e.g. `ocbrain-ops`) |
| `source_commit` | locator* | git commit the skill was built/loaded from |
| `tree_sha256` | locator* | sha256 over the skill's file tree (canonical order) |
| `skill_uri` | locator* | resolvable URI for the exact artifact |
| `skill_version` | no | publisher version string |
| `runtime` | no | reporting runtime (codex, claude, cursor, hermes, ...) |
| `session_id` | no | reporting session |
| `task_ref` | no | task the skill was used for |
| `outcome` | no | outcome note for `skill_outcome` (e.g. `success`, `failed`, `abandoned`) |
| `evidence_id` | no | related OCBrain evidence id |
| `parent_event_id` | no | related ledger event id |
| `artifact_uri` / `artifact_sha256` | no | built artifact locator |
| `superseded_by` | no | successor skill id (retirement/correction) |
| `reason_code` | no | short machine-readable reason (no prose) |

\* at least one locator field is required.

## Kinds

- `skill_build` — a skill artifact was produced.
- `skill_install` — a skill artifact was installed into a runtime.
- `skill_load` — a runtime loaded a skill into a session.
- `skill_outcome` — the task using the skill finished; record `outcome`.
- `skill_correction_candidate` — evidence suggests the skill is wrong or
  stale and should be reviewed.
- `skill_retirement` — a skill was retired; record `superseded_by` when a
  replacement exists.

## Example

```json
{
  "schema_version": "ocbrain.skill_telemetry.v1",
  "kind": "skill_outcome",
  "skill_id": "ocbrain-ops",
  "source_commit": "a5b35db",
  "tree_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "runtime": "hermes",
  "task_ref": "mission-exact-lookup",
  "outcome": "success"
}
```

```python
from ocbrain.events import validate_skill_telemetry

envelope = validate_skill_telemetry(payload)  # raises ValueError if invalid
# then: brain.ingest(kind=envelope["kind"], body=canonical_json(envelope))
```
