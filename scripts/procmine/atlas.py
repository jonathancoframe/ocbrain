"""Stage 4: assemble the atlas.

Everything printed here carries a count and, where it names a workflow, the
episode ids behind it. The report is generated, not written, so a rerun after
more closeouts lands produces the same document with new numbers.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .dag import mine_families, mine_gotchas, mine_motifs, mine_repairs, step_reliability
from .episodes import GRADE_ORDER, Episode, join_episodes, load_episodes, mining_set

_BAD = {"error", "refused", "timeout"}


def corpus_stats(traces: list[dict[str, Any]]) -> dict[str, Any]:
    by_runtime: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"traces": 0, "events": 0, "failures": 0}
    )
    result_classes: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    for trace in traces:
        family = trace["runtime"].split(":")[0]
        bucket = by_runtime[family]
        bucket["traces"] += 1
        for event in trace.get("events") or []:
            bucket["events"] += 1
            result_classes[str(event["result_class"])] += 1
            tools[str(event["tool"])] += 1
            if event["result_class"] in _BAD:
                bucket["failures"] += 1
    for bucket in by_runtime.values():
        bucket["failure_rate"] = (
            round(bucket["failures"] / bucket["events"], 3) if bucket["events"] else 0.0
        )
    return {
        "n_traces": len(traces),
        "n_events": sum(bucket["events"] for bucket in by_runtime.values()),
        "by_runtime": dict(sorted(by_runtime.items(), key=lambda kv: -kv[1]["events"])),
        "result_classes": dict(result_classes.most_common()),
        "top_tools": dict(tools.most_common(20)),
    }


def join_report(episodes: list[Episode]) -> dict[str, Any]:
    per_runtime: dict[str, Counter[str]] = defaultdict(Counter)
    for episode in episodes:
        per_runtime[episode.runtime][episode.join_tier] += 1
    rows = {}
    for runtime, tiers in sorted(per_runtime.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(tiers.values())
        joined = total - tiers.get("unjoined", 0)
        rows[runtime] = {
            "closeouts": total,
            "joined": joined,
            "join_rate": round(joined / total, 3) if total else 0.0,
            "identity": tiers.get("exact", 0) + tiers.get("uuid", 0),
            "temporal": tiers.get("temporal", 0),
            "temporal_context": tiers.get("temporal-context", 0),
            "ambiguous": tiers.get("temporal-ambiguous", 0),
            "unjoined": tiers.get("unjoined", 0),
        }
    return rows


def grade_report(episodes: list[Episode]) -> dict[str, Any]:
    grades = Counter(episode.grade for episode in episodes)
    joined_grades = Counter(
        episode.grade for episode in episodes if episode.join_tier != "unjoined"
    )
    return {
        "all": {grade: grades.get(grade, 0) for grade in reversed(GRADE_ORDER)},
        "joined_only": {grade: joined_grades.get(grade, 0) for grade in reversed(GRADE_ORDER)},
    }


def runtime_comparison(episodes: list[Episode]) -> dict[str, Any]:
    """Per-runtime shape of a traced episode: length, failure rate, tool mix."""
    rows: dict[str, Any] = {}
    grouped: dict[str, list[Episode]] = defaultdict(list)
    for episode in episodes:
        if episode.events and episode.trace_runtime:
            grouped[episode.trace_runtime.split(":")[0]].append(episode)
    for runtime, group in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        steps = sorted(len(episode.events) for episode in group)
        events = [event for episode in group for event in episode.events]
        failures = sum(1 for event in events if event["result_class"] in _BAD)
        signatures = Counter(str(event["arg_signature"]) for event in events)
        successes = sum(
            1
            for episode in group
            if episode.grade in {"verifier-receipted", "verifier-claimed", "artifact-linked"}
        )
        rows[runtime] = {
            "episodes": len(group),
            "median_steps": steps[len(steps) // 2] if steps else 0,
            "p90_steps": steps[int(len(steps) * 0.9)] if steps else 0,
            "events": len(events),
            "failure_rate": round(failures / len(events), 3) if events else 0.0,
            "receipted_or_better": successes,
            "receipted_rate": round(successes / len(group), 3) if group else 0.0,
            "top_steps": dict(signatures.most_common(8)),
        }
    return rows


def corpus_runtime_comparison(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Cross-runtime shape over *all* sessions, label-free.

    The labeled comparison in :func:`runtime_comparison` is honest but thin. This
    one has the whole corpus behind it and answers "does codex work differently
    from claude-code" even where no closeout was ever filed.
    """
    from .normalize import step_class

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trace in traces:
        grouped[trace["runtime"].split(":")[0]].append(trace)
    rows: dict[str, Any] = {}
    for runtime, group in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        lengths = sorted(len(trace.get("events") or []) for trace in group)
        events = [event for trace in group for event in trace.get("events") or []]
        if not events:
            continue
        classes = Counter(step_class(str(event["arg_signature"])) for event in events)
        failures_by_class: Counter[str] = Counter()
        for event in events:
            if event["result_class"] in _BAD:
                failures_by_class[step_class(str(event["arg_signature"]))] += 1
        rows[runtime] = {
            "sessions": len(group),
            "events": len(events),
            "median_steps": lengths[len(lengths) // 2],
            "p90_steps": lengths[int(len(lengths) * 0.9)],
            "max_steps": lengths[-1],
            "failure_rate": round(
                sum(1 for event in events if event["result_class"] in _BAD) / len(events), 3
            ),
            "step_mix": {
                name: round(count / len(events), 3) for name, count in classes.most_common(8)
            },
            "worst_step_class": max(
                (
                    (name, round(failures_by_class[name] / count, 3), count)
                    for name, count in classes.items()
                    if count >= 100
                ),
                key=lambda item: item[1],
                default=None,
            ),
        }
    return rows


def build(
    trace_cache: Path,
    *,
    brain_db: Path | None = None,
) -> dict[str, Any]:
    from .extract import read_cache

    traces = read_cache(trace_cache)
    episodes = load_episodes(brain_db)
    join_counts = join_episodes(episodes, traces)

    mining_episodes, attrition = mining_set(episodes)
    families = mine_families(mining_episodes)
    repairs = mine_repairs(traces)
    reliability = step_reliability(traces)
    motifs = mine_motifs(traces)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": corpus_stats(traces),
        "closeouts": {
            "n": len(episodes),
            "earliest": min((e.closed_at for e in episodes), default=None),
            "latest": max((e.closed_at for e in episodes), default=None),
            "distinct_runtime_strings": len({e.runtime_raw or "" for e in episodes}),
            "grades": grade_report(episodes),
            "join_tiers": join_counts,
            "join_by_runtime": join_report(episodes),
            "mining_attrition": attrition,
        },
        "runtime_comparison": runtime_comparison(mining_episodes),
        "corpus_runtime_comparison": corpus_runtime_comparison(traces),
        "families": families,
        "repairs": repairs,
        "gotchas": mine_gotchas(reliability, repairs),
        "step_reliability": reliability[:40],
        "motifs": motifs[:30],
        "adapters": json.loads(
            (trace_cache.parent / "extract_summary.json").read_text()
        ) if (trace_cache.parent / "extract_summary.json").exists() else {},
        "episodes": [episode.as_dict() for episode in episodes],
    }


# --- rendering ------------------------------------------------------------


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(out)


def _mermaid(procedure: dict[str, Any], *, max_edges: int = 14) -> str:
    ids: dict[str, str] = {}

    def node_id(step: str) -> str:
        if step not in ids:
            ids[step] = f"S{len(ids)}"
        return ids[step]

    lines = ["```mermaid", "graph TD"]
    edges = procedure.get("edges", [])[:max_edges]
    if not edges:
        return ""
    labels: set[str] = set()
    for edge in edges:
        left, right = node_id(edge["from"]), node_id(edge["to"])
        for step, ident in ((edge["from"], left), (edge["to"], right)):
            if ident not in labels:
                labels.add(ident)
                lines.append(f'    {ident}["{step.replace(chr(34), "")}"]')
        lines.append(f"    {left} -->|{edge['count']}| {right}")
    lines.append("```")
    return "\n".join(lines)


def render_markdown(data: dict[str, Any]) -> str:
    corpus = data["corpus"]
    closeouts = data["closeouts"]
    out: list[str] = []
    add = out.append

    add("# Procedure Atlas")
    add("")
    add(
        f"Generated {data['generated_at']} by `scripts/procmine`. "
        "Every number below is computed from the live corpus at generation time; "
        "rerun the miner to refresh it."
    )
    add("")
    add("## 1. Corpus")
    add("")
    add(
        f"**{corpus['n_events']:,} normalized tool calls** across "
        f"**{corpus['n_traces']:,} sessions**, and "
        f"**{closeouts['n']:,} closeouts** from {closeouts['earliest']} to "
        f"{closeouts['latest']}."
    )
    add("")
    add(
        _table(
            ["runtime", "sessions", "tool calls", "failed calls", "failure rate"],
            [
                [name, row["traces"], f"{row['events']:,}", row["failures"], row["failure_rate"]]
                for name, row in corpus["by_runtime"].items()
            ],
        )
    )
    add("")
    add("Result classes across all calls:")
    add("")
    add(
        _table(
            ["result_class", "calls", "share"],
            [
                [name, f"{count:,}", f"{count / max(corpus['n_events'], 1):.1%}"]
                for name, count in corpus["result_classes"].items()
            ],
        )
    )
    add("")

    add("## 2. Join yield: how much of the label set actually reaches a trace")
    add("")
    tiers = closeouts["join_tiers"]
    total = closeouts["n"]
    joined = total - tiers.get("unjoined", 0)
    add(
        f"**{joined} of {total} closeouts ({joined / total:.0%}) resolve to a trace.** "
        f"Only {tiers.get('exact', 0) + tiers.get('uuid', 0)} do so by identity; the rest "
        f"are temporal matches within the runtime family."
    )
    add("")
    add(
        _table(
            ["join tier", "closeouts", "what it means"],
            [
                ["exact", tiers.get("exact", 0), "`session_id` equals a trace id"],
                ["uuid", tiers.get("uuid", 0), "a UUID inside `session_id` equals a trace id"],
                [
                    "temporal",
                    tiers.get("temporal", 0),
                    "same runtime family, `closed_at` inside exactly one session window +/-45min",
                ],
                [
                    "temporal-context",
                    tiers.get("temporal-context", 0),
                    "several sessions were open; exactly one worked in a directory the "
                    "closeout names",
                ],
                [
                    "temporal-ambiguous",
                    tiers.get("temporal-ambiguous", 0),
                    "several sessions were open and nothing distinguishes them; attached "
                    "but excluded from mining",
                ],
                ["unjoined", tiers.get("unjoined", 0), "no trace found"],
            ],
        )
    )
    add("")
    attrition = closeouts["mining_attrition"]
    add(
        "Joining is not the same as being usable. Two independence filters run before "
        "any procedure is mined:"
    )
    add("")
    add(
        _table(
            ["filter", "episodes"],
            [
                ["joined to a trace", attrition["joined"]],
                ["- dropped: ambiguous temporal match", attrition["dropped_ambiguous"]],
                ["= eligible", attrition["eligible"]],
                [
                    "of those, sharing a session with another closeout (time-segmented)",
                    attrition["shared_a_trace"],
                ],
                [
                    "- dropped: segment contained no tool calls",
                    attrition["dropped_empty_segment"],
                ],
                [
                    "- dropped: fewer than 3 tool calls (cannot describe a procedure)",
                    attrition["dropped_too_short"],
                ],
                ["**= mining set**", f"**{attrition['mining_episodes']}**"],
            ],
        )
    )
    add("")
    add(
        "Segmentation matters more than it sounds. Long-running sessions file many "
        "closeouts, so without slicing, one codex heartbeat rollout would present "
        "itself as dozens of independent confirmations of the same procedure and every "
        "support count downstream would be inflated by its length. Slicing on "
        "`closed_at` gives each closeout the disjoint span of tool calls it is "
        "plausibly a receipt for."
    )
    add("")
    add("Per normalized runtime:")
    add("")
    add(
        _table(
            [
                "runtime", "closeouts", "joined", "join rate", "by identity",
                "temporal", "temporal+context", "ambiguous",
            ],
            [
                [
                    name, row["closeouts"], row["joined"], row["join_rate"],
                    row["identity"], row["temporal"], row["temporal_context"],
                    row["ambiguous"],
                ]
                for name, row in closeouts["join_by_runtime"].items()
            ],
        )
    )
    add("")

    add("## 3. Label grades")
    add("")
    completed_grades = (
        "self-reported-completed", "verifier-receipted", "verifier-claimed", "artifact-linked",
    )
    n_completed = sum(closeouts["grades"]["all"].get(name, 0) for name in completed_grades)
    add(
        f"`status` does not discriminate: {n_completed} of the closeouts say completed. "
        "The grade ladder turns instead on whether the receipt can still be checked today."
    )
    add("")
    add(
        _table(
            ["grade", "all closeouts", "closeouts with a trace"],
            [
                [
                    grade,
                    closeouts["grades"]["all"][grade],
                    closeouts["grades"]["joined_only"][grade],
                ]
                for grade in closeouts["grades"]["all"]
            ],
        )
    )
    add("")

    add("## 4. Procedures")
    add("")
    families = data["families"]
    procedures = families["procedures"]
    add(
        f"{families['n_families']} task families were formed from the traced episodes. "
        f"**{len(procedures)} cleared the support floor; {len(families['abstained'])} abstained.** "
        f"Thresholds: {families['thresholds']}."
    )
    add("")
    if not procedures:
        add(
            "> **No family cleared the floor.** This is the honest result, not a bug: "
            "see section 8 for why the labeled subset is too thin to support a procedure "
            "claim, and sections 5-6 for what the corpus *can* support."
        )
        add("")
    for procedure in procedures:
        stats = procedure["stats"]
        add(f"### {procedure['label']}")
        add("")
        add(
            f"{stats['n']} traced episodes, {stats['success_n']} at artifact-linked or better "
            f"({stats['success_rate']:.0%}); median {stats['median_steps']} steps."
        )
        add("")
        add(f"- By grade: `{stats['by_grade']}`")
        add(f"- By trace runtime: `{stats['by_trace_runtime']}`")
        receipts = procedure["receipts"]
        extra = f" (+{len(receipts) - 12} more)" if len(receipts) > 12 else ""
        add(f"- Receipts: `{', '.join(receipts[:12])}`{extra}")
        add("")
        if procedure["patterns"]:
            top = procedure["patterns"][0]
            add(f"Most-supported subsequence ({top['support']}/{stats['n']} episodes):")
            add("")
            for index, step in enumerate(top["steps"], 1):
                add(f"{index}. `{step}`")
            add("")
        diagram = _mermaid(procedure)
        if diagram:
            add(diagram)
            add("")
        if procedure["failure_branches"]:
            add(
                "**Failure branches** — steps over-represented in episodes that did not "
                "reach a receipt:"
            )
            add("")
            add(
                _table(
                    ["step", "in failed", "in succeeded", "presence lift", "step error rate"],
                    [
                        [
                            f"`{branch['step']}`",
                            branch["in_failed_episodes"],
                            branch["in_successful_episodes"],
                            branch["presence_lift"],
                            branch["step_error_rate"],
                        ]
                        for branch in procedure["failure_branches"]
                    ],
                )
            )
            add("")

    if families["abstained"]:
        add("### Abstentions")
        add("")
        add("Families the miner refused to turn into a procedure:")
        add("")
        add(
            _table(
                ["family", "traced episodes", "reason"],
                [
                    [item["label"], item["n_traced_episodes"], item["reason"]]
                    for item in families["abstained"][:15]
                ],
            )
        )
        if len(families["abstained"]) > 15:
            add("")
            add(f"...and {len(families['abstained']) - 15} more.")
        add("")

    add("## 5. Mined gotchas: recurring failure/repair pairs")
    add("")
    repairs = data["repairs"]
    add(
        f"Label-free and corpus-wide. A pair qualifies at >= {repairs['thresholds']['min_pairs']} "
        f"occurrences across >= {repairs['thresholds']['min_distinct_sessions']} distinct sessions "
        f"and >= {repairs['thresholds']['min_lift']}x the base rate of that follow-up step. "
        f"{len(repairs['repairs'])} pairs qualified; "
        f"{repairs['rejected_below_threshold']} were rejected."
    )
    add("")
    if repairs["repairs"]:
        add(
            _table(
                [
                    "when this fails", "the next step is", "times", "sessions",
                    "next step worked", "lift",
                ],
                [
                    [
                        f"`{row['failing_step']}`",
                        f"`{row['repair_step']}`",
                        row["pairs"],
                        row["distinct_sessions"],
                        f"{row['repair_success_rate']:.0%}",
                        row["lift_over_base_rate"],
                    ]
                    for row in repairs["repairs"][:20]
                ],
            )
        )
        add("")
    if repairs["retries"]:
        add(
            "Plain retries are separated out — the agent reissued the same step. "
            "The interesting column is whether it worked, because a step that "
            "succeeds on immediate retry is flaky, not broken:"
        )
        add("")
        add(
            _table(
                ["step", "retries after a failure", "sessions", "retry succeeded"],
                [
                    [
                        f"`{row['failing_step']}`",
                        row["pairs"],
                        row["distinct_sessions"],
                        f"{row['repair_success_rate']:.0%}",
                    ]
                    for row in repairs["retries"][:12]
                ],
            )
        )
        add("")

    add("## 6. Step reliability")
    add("")
    add("Signatures with >= 50 calls, ranked by failure rate. This is the raw gotcha material.")
    add("")
    add(
        _table(
            ["step", "calls", "sessions", "failure rate", "classes"],
            [
                [
                    f"`{row['step']}`", f"{row['calls']:,}", row["sessions"],
                    f"{row['failure_rate']:.0%}", f"`{row['by_result_class']}`",
                ]
                for row in data["step_reliability"][:25]
            ],
        )
    )
    add("")

    add("## 7. Cross-runtime comparison")
    add("")
    add("### 7a. Whole corpus (label-free)")
    add("")
    add(
        "Every session, whether or not a closeout was ever filed against it. This is "
        "the comparison with real N behind it."
    )
    add("")
    add(
        _table(
            [
                "runtime", "sessions", "tool calls", "median steps", "p90", "longest",
                "call failure rate",
            ],
            [
                [
                    name, row["sessions"], f"{row['events']:,}", row["median_steps"],
                    row["p90_steps"], row["max_steps"], f"{row['failure_rate']:.1%}",
                ]
                for name, row in data["corpus_runtime_comparison"].items()
            ],
        )
    )
    add("")
    add("Step mix (share of that runtime's calls, abstract step classes):")
    add("")
    for name, row in data["corpus_runtime_comparison"].items():
        mix = ", ".join(f"`{step}` {share:.0%}" for step, share in row["step_mix"].items())
        add(f"- **{name}**: {mix}")
        worst = row["worst_step_class"]
        if worst:
            add(
                f"  - worst step class (>=100 calls): `{worst[0]}` fails "
                f"{worst[1]:.0%} of {worst[2]:,} calls"
            )
    add("")
    add("### 7b. Labeled episodes only")
    add("")
    add(
        "Restricted to the mining set. Reported for completeness; the per-runtime "
        "counts outside codex are too small to support a comparison, and are labeled "
        "as such rather than dressed up."
    )
    add("")
    add(
        _table(
            ["runtime", "episodes", "median steps", "p90 steps", "call failure rate", "receipted+"],
            [
                [
                    name, row["episodes"], row["median_steps"], row["p90_steps"],
                    f"{row['failure_rate']:.0%}",
                    f"{row['receipted_rate']:.0%}" if row["episodes"] >= 10 else "n too small",
                ]
                for name, row in data["runtime_comparison"].items()
            ],
        )
    )
    add("")

    add("## 8. Corpus-wide motifs")
    add("")
    add(
        f"Frequent contiguous step sequences across all {corpus['n_traces']:,} sessions, "
        f"label-free, minimum 20 sessions. Structure without outcome."
    )
    add("")
    add(
        _table(
            ["sequence", "sessions"],
            [
                [" -> ".join(f"`{step}`" for step in row["steps"]), row["traces"]]
                for row in data["motifs"][:20]
            ],
        )
    )
    add("")

    add("## 9. The mined gotchas, stated as claims")
    add("")
    add(
        "Generated from the counts above rather than written, so the wording cannot "
        "drift from the evidence. Threshold: >= 50 calls and >= 20% failure rate."
    )
    add("")
    for index, gotcha in enumerate(data["gotchas"], 1):
        add(f"{index}. {gotcha['claim']}")
        add(
            f"   - seen on: `{gotcha['by_runtime']}`; "
            f"sessions to read: `{', '.join(gotcha['receipt_sessions'][:3])}`"
        )
    add("")

    add("## 10. What this corpus cannot tell us")
    add("")
    adapters = data.get("adapters", {})
    shortfall = adapters.get("cursor_shortfall", {})
    n_cursor_closeouts = closeouts["join_by_runtime"].get("cursor", {}).get("closeouts", 0)
    add(
        "- **Cursor is invisible.** `scripts/export-cursor-chats.py` exports "
        f"{shortfall.get('messages', 0)} messages across {shortfall.get('files', 0)} "
        f"workspaces with {shortfall.get('records_with_tool_field', 0)} tool fields "
        f"between them, so all {n_cursor_closeouts} "
        "cursor closeouts have no trace and cursor cannot appear in any comparison."
    )
    add(
        f"- **{tiers.get('unjoined', 0)} closeouts have no trace at all.** They are not "
        "missing at random: they concentrate in runtimes whose `session_id` was a "
        "human slug, so what is absent is exactly the work done outside the "
        "identity-carrying clients."
    )
    add(
        f"- **{tiers.get('temporal-ambiguous', 0)} joins are ambiguous** because several "
        "sessions were open at once. Parallel sessions are normal here, so this is a "
        "permanent property of the setup, not a backlog to clear."
    )
    add(
        "- **Timing is not attribution.** Even an unambiguous temporal join assumes the "
        "closeout describes the session that was open. Segmentation makes that "
        "assumption sharper, not true."
    )
    add(
        "- **Grades measure receipts, not correctness.** `verifier-receipted` means a "
        "passed verifier whose file still exists. It does not mean the work was right, "
        "and a deleted artifact demotes a good episode."
    )
    add(
        "- **Failure rates are per call, not per intent.** A step that fails and is "
        "immediately retried successfully counts once as a failure. That is the right "
        "measure for reliability and the wrong one for whether the agent got stuck."
    )
    add(
        "- **hermes-legacy signatures are tool-only.** The legacy export never stored "
        "arguments, so its steps carry a `toolonly:` prefix and cannot be compared with "
        "the argument-shaped signatures from the other adapters at the fine level. "
        "Abstract step classes are comparable; raw signatures are not."
    )
    add(
        "- **No counterfactual.** The corpus records what was done, never what would "
        "have happened otherwise, so nothing here supports a claim that one procedure "
        "*causes* a better outcome than another."
    )
    add("")

    add("## 11. Adapter status")
    add("")
    add(
        _table(
            ["runtime", "status"],
            [[name, status] for name, status in adapters.get("adapter_status", {}).items()],
        )
    )
    add("")
    return "\n".join(out)


def write_outputs(data: dict[str, Any], *, docs_dir: Path, json_path: Path) -> None:
    docs_dir.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    machine = {key: value for key, value in data.items() if key != "episodes"}
    json_path.write_text(json.dumps(machine, indent=2, sort_keys=True) + "\n")
