# OCBrain harness snippet

Paste into `AGENTS.md`, a Codex `AGENTS.md`, or a Hermes profile `SOUL.md`.
Keep it short: this competes for the same window as the briefing it describes.

---

## OCBrain: how to start, and how to not repeat yourself

**First call of every session, before anything else:** `brain.briefing` with
your project scope. No query argument — it does not take one. It returns the
same bytes for the same scope and corpus state, so it is safe to lean on: open
goals, what is verified done, what was attempted and failed, the latest closeout
chain, standing gotchas. Under 1500 characters. Assume you were interrupted.

**Before building anything that might already exist:** `brain.ledger`. Pass
`task_ref` for one task's full attempt chain. A task marked `verified_done` is
done — do not rebuild it. A task with `failed_attempts` has already been tried;
read the summaries before trying the same approach again. `in_flight` means
somebody claimed completion without a passing verifier, so check before trusting
it.

**Goals.** `brain.goal_open` takes an objective, an executable `finish_line` (a
command or test path), and a `source_path` pointing at the spec **in the repo**.
The spec lives in git and stays human-reviewable; OCBrain pins the pointer and
never becomes the place you edit requirements. Close with `brain.goal_close`,
naming the verifier evidence — a closure with no evidence is not a closure.

**Then, and only then,** `brain.context` for "what do I know about X". Briefing
answers "where was I"; context answers "what do I know". Do not use one for the
other.

**Finish with `brain.closeout`,** linking retrievals, artifacts, and verifiers.
Every verifier needs a `uri` as well as a status. Put the runtime's own session
id in `context.session`, not a hand-written slug. A failed attempt is worth
closing out — the ledger reads it, and it is what stops the next session
repeating your afternoon.

The brain owns no loop, no queue, and no scheduler. It answers questions; your
harness decides what to do about the answers.
