"""Stage 3: induce procedures, and refuse to when the evidence is thin.

Three miners, deliberately separate because they answer different questions and
have very different support:

:func:`mine_families`
    Groups *labeled* episodes into task families, aligns their event streams, and
    emits a procedure DAG with success statistics and failure branches. Support
    is limited by the closeout-to-trace join, so most families abstain.

:func:`mine_repairs`
    Corpus-wide, label-free. For every step signature that fails, what did the
    agent do next, and did that work? Recurring ``(failure -> repair)`` pairs are
    the highest-support, most actionable thing in the corpus, and they need no
    closeout at all.

:func:`mine_motifs`
    Corpus-wide frequent contiguous n-grams. Structure without outcome.

Abstention is the design point. The brain's standing failure mode is a corpus
full of low-value entries, and a procedure table full of two-episode "workflows"
would recreate it in a new place. Every miner returns its rejects with a reason
so the atlas can report what was refused and why.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from .episodes import GRADE_RANK, Episode
from .normalize import step_class

# --- thresholds -----------------------------------------------------------
# Chosen to be defensible rather than tuned: a procedure needs enough episodes
# that a single odd session cannot mint it, and a repair needs enough distinct
# sessions that one bad afternoon cannot either.
MIN_FAMILY_EPISODES = 5
MIN_PATTERN_SUPPORT = 4
MIN_PATTERN_COVERAGE = 0.5
MIN_PATTERN_LENGTH = 3
MAX_PATTERN_LENGTH = 6
MIN_NODE_SUPPORT = 3

MIN_REPAIR_PAIRS = 8
MIN_REPAIR_SESSIONS = 4
MIN_REPAIR_LIFT = 2.0

MIN_MOTIF_TRACES = 20
MOTIF_MAX_LEN = 5

FAMILY_SIMILARITY = 0.34

_SUCCESS_GRADES = {"verifier-receipted", "verifier-claimed", "artifact-linked"}
_BAD = {"error", "refused", "timeout"}

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "to", "of", "in", "on", "at",
    "by", "from", "is", "are", "was", "were", "be", "been", "it", "this", "that",
    "as", "into", "via", "per", "plus", "not", "no", "new", "all", "any", "one",
    "two", "task", "run", "ran", "use", "used", "using", "add", "added", "now",
    "then", "also", "after", "before", "over", "under", "out", "up", "down",
    "local", "macos", "mac", "codex", "hermes", "cursor", "claude", "20260",
    "root", "user", "jonathancoframe", "current", "default", "session", "chat",
}
_TOKEN = re.compile(r"[a-z][a-z0-9]{2,}")
_DATEISH = re.compile(r"\b(?:20\d{6}|20\d{2}-\d{2}-\d{2}|t\d{4}z?)\b")
_HEXISH = re.compile(r"\b[0-9a-f]{8,}\b")


def _tokens(text: str | None, *, weight: int = 1) -> Counter[str]:
    if not text:
        return Counter()
    lowered = _HEXISH.sub(" ", _DATEISH.sub(" ", text.lower()))
    found = [t for t in _TOKEN.findall(lowered) if t not in _STOPWORDS]
    counts: Counter[str] = Counter()
    for token in found:
        counts[token] += weight
    return counts


def episode_features(episode: Episode) -> Counter[str]:
    """Task ref counts double: it is the closest thing to a declared intent."""
    features = _tokens(episode.task_ref, weight=2)
    features.update(_tokens(episode.project, weight=2))
    features.update(_tokens(episode.summary, weight=1))
    return features


def _idf(documents: list[Counter[str]]) -> dict[str, float]:
    total = max(len(documents), 1)
    seen: Counter[str] = Counter()
    for document in documents:
        seen.update(document.keys())
    return {term: math.log(1.0 + total / count) for term, count in seen.items()}


def _weighted(document: Counter[str], idf: dict[str, float]) -> dict[str, float]:
    vector = {term: count * idf.get(term, 0.0) for term, count in document.items()}
    norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
    return {term: value / norm for term, value in vector.items()}


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(term, 0.0) for term, value in left.items())


@dataclass(slots=True)
class Family:
    family_id: str
    label: str
    episodes: list[Episode] = field(default_factory=list)


def cluster_families(
    episodes: list[Episode], *, threshold: float = FAMILY_SIMILARITY
) -> list[Family]:
    """Greedy leader clustering on IDF-weighted task/summary tokens.

    Greedy rather than agglomerative on purpose: single-linkage over this corpus
    chains everything into one blob because "coframe" and "personalization" show
    up everywhere. A leader/centroid rule keeps clusters tight and is
    order-deterministic given the date sort.
    """
    if not episodes:
        return []
    documents = [episode_features(episode) for episode in episodes]
    idf = _idf(documents)
    vectors = [_weighted(document, idf) for document in documents]

    leaders: list[dict[str, float]] = []
    members: list[list[int]] = []
    for index, vector in enumerate(vectors):
        best = -1.0
        best_slot = -1
        for slot, leader in enumerate(leaders):
            score = _cosine(vector, leader)
            if score > best:
                best = score
                best_slot = slot
        if best >= threshold and best_slot >= 0:
            members[best_slot].append(index)
            leader = leaders[best_slot]
            size = len(members[best_slot])
            merged = dict(leader)
            for term, value in vector.items():
                merged[term] = merged.get(term, 0.0) + (value - merged.get(term, 0.0)) / size
            norm = math.sqrt(sum(v * v for v in merged.values())) or 1.0
            leaders[best_slot] = {t: v / norm for t, v in merged.items()}
        else:
            leaders.append(dict(vector))
            members.append([index])

    families: list[Family] = []
    for slot, indices in enumerate(members):
        combined: Counter[str] = Counter()
        for index in indices:
            combined.update(documents[index])
        top = [
            term
            for term, _ in sorted(
                combined.items(), key=lambda kv: (-kv[1] * idf.get(kv[0], 0.0), kv[0])
            )[:4]
        ]
        families.append(
            Family(
                family_id=f"fam_{slot:03d}",
                label=" / ".join(top) if top else f"family {slot}",
                episodes=[episodes[index] for index in indices],
            )
        )
    families.sort(key=lambda fam: (-len(fam.episodes), fam.family_id))
    return families


def collapse_runs(signatures: list[str]) -> list[tuple[str, int]]:
    """Run-length encode. Eight consecutive reads are one step, not eight."""
    out: list[tuple[str, int]] = []
    for signature in signatures:
        if out and out[-1][0] == signature:
            out[-1] = (signature, out[-1][1] + 1)
        else:
            out.append((signature, 1))
    return out


def _episode_sequence(episode: Episode) -> list[str]:
    """Abstract steps, not raw signatures — see ``normalize.step_class``."""
    return [step_class(str(event["arg_signature"])) for event in episode.events]


def _is_success(episode: Episode) -> bool:
    return episode.grade in _SUCCESS_GRADES


def _ngrams(sequence: list[str], length: int) -> set[tuple[str, ...]]:
    return {tuple(sequence[i : i + length]) for i in range(len(sequence) - length + 1)}


def first_occurrence_order(sequence: list[str]) -> list[str]:
    """Reduce a sequence to the order its distinct steps first appeared.

    A procedure is "these steps, in this relative order", not "these calls back
    to back". Real sessions interleave — read, search, read, edit, read — so
    contiguous n-grams find almost nothing while the underlying order is stable.
    """
    seen: set[str] = set()
    out: list[str] = []
    for step in sequence:
        if step not in seen:
            seen.add(step)
            out.append(step)
    return out


def is_subsequence(pattern: tuple[str, ...], sequence: list[str]) -> bool:
    cursor = 0
    for step in sequence:
        if step == pattern[cursor]:
            cursor += 1
            if cursor == len(pattern):
                return True
    return False


def frequent_subsequences(
    sequences: dict[str, list[str]], *, min_support: int, max_len: int
) -> dict[tuple[str, ...], set[str]]:
    """Apriori over order-preserving subsequences, pruned at every level."""
    items: dict[str, set[str]] = defaultdict(set)
    for key, sequence in sequences.items():
        for step in set(sequence):
            items[step].add(key)
    frequent: dict[tuple[str, ...], set[str]] = {
        (step,): ids for step, ids in items.items() if len(ids) >= min_support
    }
    alphabet = sorted(step for (step,) in frequent)
    level = dict(frequent)
    for _ in range(max_len - 1):
        nxt: dict[tuple[str, ...], set[str]] = {}
        for pattern, ids in level.items():
            for step in alphabet:
                candidate = (*pattern, step)
                support = {key for key in ids if is_subsequence(candidate, sequences[key])}
                if len(support) >= min_support:
                    nxt[candidate] = support
        if not nxt:
            break
        frequent.update(nxt)
        level = nxt
    return frequent


def mine_family(family: Family) -> dict[str, Any]:
    """Turn one family into a procedure, or into a refusal with a reason."""
    traced = [episode for episode in family.episodes if episode.events]
    result: dict[str, Any] = {
        "family_id": family.family_id,
        "label": family.label,
        "n_episodes": len(family.episodes),
        "n_traced_episodes": len(traced),
    }
    if len(traced) < MIN_FAMILY_EPISODES:
        result["abstained"] = (
            f"only {len(traced)} traced episodes; threshold is {MIN_FAMILY_EPISODES}"
        )
        return result

    sequences = {
        episode.closeout_id: [sig for sig, _ in collapse_runs(_episode_sequence(episode))]
        for episode in traced
    }

    ordered = {
        closeout_id: first_occurrence_order(sequence)
        for closeout_id, sequence in sequences.items()
    }
    floor = max(MIN_PATTERN_SUPPORT, math.ceil(MIN_PATTERN_COVERAGE * len(traced)))
    frequent = {
        pattern: ids
        for pattern, ids in frequent_subsequences(
            ordered, min_support=floor, max_len=MAX_PATTERN_LENGTH
        ).items()
        if len(pattern) >= MIN_PATTERN_LENGTH
    }
    if not frequent:
        result["abstained"] = (
            f"no {MIN_PATTERN_LENGTH}+-step ordered subsequence reaches {floor} of "
            f"{len(traced)} traced episodes"
        )
        return result
    # Keep maximal patterns: a 3-step prefix of a 5-step pattern with the same
    # support says nothing the longer one does not.
    maximal = [
        (pattern, ids)
        for pattern, ids in frequent.items()
        if not any(
            other != pattern
            and len(other) > len(pattern)
            and is_subsequence(pattern, list(other))
            and len(frequent[other]) >= len(ids)
            for other in frequent
        )
    ]
    maximal.sort(key=lambda item: (-len(item[1]), -len(item[0]), item[0]))

    # DAG over steps that clear the node floor.
    node_support: dict[str, set[str]] = defaultdict(set)
    for closeout_id, sequence in sequences.items():
        for signature in sequence:
            node_support[signature].add(closeout_id)
    nodes = {sig for sig, ids in node_support.items() if len(ids) >= MIN_NODE_SUPPORT}
    edges: Counter[tuple[str, str]] = Counter()
    for sequence in sequences.values():
        filtered = [sig for sig in sequence if sig in nodes]
        for left, right in zip(filtered, filtered[1:], strict=False):
            if left != right:
                edges[(left, right)] += 1

    successes = [episode for episode in traced if _is_success(episode)]
    failures = [episode for episode in traced if not _is_success(episode)]
    result.update(
        {
            "patterns": [
                {
                    "steps": list(pattern),
                    "support_episodes": sorted(ids),
                    "support": len(ids),
                    "coverage": round(len(ids) / len(traced), 2),
                }
                for pattern, ids in maximal[:5]
            ],
            "nodes": [
                {"step": sig, "support": len(node_support[sig])}
                for sig in sorted(nodes, key=lambda s: (-len(node_support[s]), s))
            ],
            "edges": [
                {"from": left, "to": right, "count": count}
                for (left, right), count in edges.most_common(40)
            ],
            "stats": _family_stats(traced),
            "failure_branches": _failure_branches(traced, successes, failures),
            "receipts": sorted(episode.closeout_id for episode in traced),
        }
    )
    return result


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    span = len(needle)
    return any(haystack[i : i + span] == needle for i in range(len(haystack) - span + 1))


def _family_stats(episodes: list[Episode]) -> dict[str, Any]:
    grades = Counter(episode.grade for episode in episodes)
    runtimes = Counter(episode.runtime for episode in episodes)
    trace_runtimes = Counter(episode.trace_runtime or "?" for episode in episodes)
    steps = sorted(len(episode.events) for episode in episodes)
    successes = sum(1 for episode in episodes if _is_success(episode))
    return {
        "n": len(episodes),
        "success_n": successes,
        "success_rate": round(successes / len(episodes), 3) if episodes else 0.0,
        "by_grade": dict(grades.most_common()),
        "by_closeout_runtime": dict(runtimes.most_common()),
        "by_trace_runtime": dict(trace_runtimes.most_common()),
        "median_steps": steps[len(steps) // 2] if steps else 0,
        "min_grade_rank": min(episode.grade_rank for episode in episodes),
        "max_grade_rank": max(episode.grade_rank for episode in episodes),
    }


def _failure_branches(
    traced: list[Episode], successes: list[Episode], failures: list[Episode]
) -> list[dict[str, Any]]:
    """Where do unsuccessful runs stop looking like successful ones?

    Two signals per step: how much more often it appears in unsuccessful runs
    (presence lift), and how often the step itself errors.
    """
    if not failures or not successes:
        return []
    branches: list[dict[str, Any]] = []
    in_success: Counter[str] = Counter()
    in_failure: Counter[str] = Counter()
    error_at: Counter[str] = Counter()
    seen_at: Counter[str] = Counter()
    for episode in successes:
        in_success.update(set(_episode_sequence(episode)))
    for episode in failures:
        in_failure.update(set(_episode_sequence(episode)))
    for episode in traced:
        for event in episode.events:
            signature = step_class(str(event["arg_signature"]))
            seen_at[signature] += 1
            if event["result_class"] in _BAD:
                error_at[signature] += 1

    for signature, failure_count in in_failure.items():
        if failure_count < 2:
            continue
        failure_rate = failure_count / len(failures)
        success_rate = in_success.get(signature, 0) / len(successes)
        lift = failure_rate / success_rate if success_rate else float("inf")
        if lift < 1.5:
            continue
        branches.append(
            {
                "step": signature,
                "in_failed_episodes": failure_count,
                "in_successful_episodes": in_success.get(signature, 0),
                "presence_lift": round(lift, 2) if lift != float("inf") else None,
                "step_error_rate": (
                    round(error_at[signature] / seen_at[signature], 3)
                    if seen_at[signature]
                    else 0.0
                ),
                "step_calls": seen_at[signature],
            }
        )
    branches.sort(key=lambda item: (-(item["presence_lift"] or 99), -item["in_failed_episodes"]))
    return branches[:8]


def mine_families(episodes: list[Episode]) -> dict[str, Any]:
    families = cluster_families([e for e in episodes if e.events])
    mined = [mine_family(family) for family in families]
    return {
        "n_families": len(families),
        "procedures": [item for item in mined if "abstained" not in item],
        "abstained": [
            {
                "family_id": item["family_id"],
                "label": item["label"],
                "n_traced_episodes": item["n_traced_episodes"],
                "reason": item["abstained"],
            }
            for item in mined
            if "abstained" in item
        ],
        "thresholds": {
            "min_family_episodes": MIN_FAMILY_EPISODES,
            "min_pattern_support": MIN_PATTERN_SUPPORT,
            "min_pattern_coverage": MIN_PATTERN_COVERAGE,
            "min_pattern_length": MIN_PATTERN_LENGTH,
            "min_node_support": MIN_NODE_SUPPORT,
            "family_similarity": FAMILY_SIMILARITY,
        },
    }


# --- corpus-wide, label-free miners ---------------------------------------


def mine_repairs(traces: list[dict[str, Any]]) -> dict[str, Any]:
    """Recurring ``failing step -> next step`` pairs, ranked by lift.

    A repair is only interesting if the follow-up is *unusual* — anything can
    follow anything once. So each candidate is scored against the base rate of
    that follow-up step across the whole corpus, and a pair has to beat it by
    ``MIN_REPAIR_LIFT`` on top of raw-count and distinct-session floors.
    """
    after_failure: Counter[tuple[str, str]] = Counter()
    after_failure_sessions: dict[tuple[str, str], set[str]] = defaultdict(set)
    failure_count: Counter[str] = Counter()
    call_count: Counter[str] = Counter()
    error_count: Counter[str] = Counter()
    successor_total: Counter[str] = Counter()
    transitions = 0
    repair_worked: Counter[tuple[str, str]] = Counter()

    for trace in traces:
        events = trace.get("events") or []
        trace_id = str(trace.get("trace_id"))
        for index, event in enumerate(events):
            signature = str(event["arg_signature"])
            call_count[signature] += 1
            if event["result_class"] in _BAD:
                error_count[signature] += 1
            if index + 1 >= len(events):
                continue
            following = str(events[index + 1]["arg_signature"])
            successor_total[following] += 1
            transitions += 1
            if event["result_class"] in _BAD:
                failure_count[signature] += 1
                pair = (signature, following)
                after_failure[pair] += 1
                after_failure_sessions[pair].add(trace_id)
                if events[index + 1]["result_class"] == "ok":
                    repair_worked[pair] += 1

    repairs: list[dict[str, Any]] = []
    rejected = 0
    for (failing, following), count in after_failure.items():
        sessions = len(after_failure_sessions[(failing, following)])
        if count < MIN_REPAIR_PAIRS or sessions < MIN_REPAIR_SESSIONS:
            rejected += 1
            continue
        base = successor_total[following] / transitions if transitions else 0.0
        observed = count / failure_count[failing] if failure_count[failing] else 0.0
        lift = observed / base if base else float("inf")
        if lift < MIN_REPAIR_LIFT:
            rejected += 1
            continue
        repairs.append(
            {
                "failing_step": failing,
                "repair_step": following,
                "pairs": count,
                "distinct_sessions": sessions,
                "failing_step_calls": call_count[failing],
                "failing_step_error_rate": (
                    round(error_count[failing] / call_count[failing], 3)
                    if call_count[failing]
                    else 0.0
                ),
                "repair_succeeded": repair_worked[(failing, following)],
                "repair_success_rate": round(
                    repair_worked[(failing, following)] / count, 3
                ),
                "lift_over_base_rate": round(lift, 1) if lift != float("inf") else None,
            }
        )
    # A retry (same step again) is a different animal from a repair (a different
    # step that unsticks it), and retries would otherwise fill the whole table.
    for row in repairs:
        row["kind"] = "retry" if row["failing_step"] == row["repair_step"] else "repair"
    repairs.sort(key=lambda item: (-item["pairs"], -(item["lift_over_base_rate"] or 0)))
    return {
        "repairs": [row for row in repairs if row["kind"] == "repair"],
        "retries": [row for row in repairs if row["kind"] == "retry"],
        "rejected_below_threshold": rejected,
        "thresholds": {
            "min_pairs": MIN_REPAIR_PAIRS,
            "min_distinct_sessions": MIN_REPAIR_SESSIONS,
            "min_lift": MIN_REPAIR_LIFT,
        },
    }


def step_reliability(traces: list[dict[str, Any]], *, min_calls: int = 50) -> list[dict[str, Any]]:
    """Per-signature failure rates. The raw material for a gotcha."""
    calls: Counter[str] = Counter()
    bad: Counter[str] = Counter()
    by_class: dict[str, Counter[str]] = defaultdict(Counter)
    sessions: dict[str, set[str]] = defaultdict(set)
    failing_sessions: dict[str, list[str]] = defaultdict(list)
    runtimes: dict[str, Counter[str]] = defaultdict(Counter)
    fingerprints: dict[str, Counter[str]] = defaultdict(Counter)
    for trace in traces:
        trace_id = str(trace.get("trace_id"))
        runtime = str(trace.get("runtime", "?")).split(":")[0]
        for event in trace.get("events") or []:
            signature = str(event["arg_signature"])
            calls[signature] += 1
            sessions[signature].add(trace_id)
            runtimes[signature][runtime] += 1
            by_class[signature][str(event["result_class"])] += 1
            if event["result_class"] in _BAD:
                bad[signature] += 1
                if event.get("error"):
                    fingerprints[signature][str(event["error"])] += 1
                if trace_id not in failing_sessions[signature]:
                    failing_sessions[signature].append(trace_id)
    rows = [
        {
            "step": signature,
            "calls": count,
            "sessions": len(sessions[signature]),
            "failures": bad[signature],
            "failure_rate": round(bad[signature] / count, 3),
            "by_result_class": dict(by_class[signature].most_common()),
            "by_runtime": dict(runtimes[signature].most_common(3)),
            "top_errors": [
                {"message": message, "count": count}
                for message, count in fingerprints[signature].most_common(3)
            ],
            "receipt_sessions": failing_sessions[signature][:5],
        }
        for signature, count in calls.items()
        if count >= min_calls
    ]
    rows.sort(key=lambda row: (-row["failure_rate"], -row["calls"]))
    return rows


MIN_GOTCHA_CALLS = 50
MIN_GOTCHA_FAILURE_RATE = 0.20


def mine_gotchas(
    reliability: list[dict[str, Any]], repairs: dict[str, Any], *, limit: int = 12
) -> list[dict[str, Any]]:
    """State the worst steps as claims a reader could act on.

    Generated rather than written, so the wording cannot drift from the counts.
    Each claim carries its denominator, its dominant failure mode, the runtimes
    it was seen on, and the sessions to go read.
    """
    repair_by_step: dict[str, dict[str, Any]] = {}
    for row in repairs.get("repairs", []):
        repair_by_step.setdefault(row["failing_step"], row)
    retry_by_step = {row["failing_step"]: row for row in repairs.get("retries", [])}

    claims: list[dict[str, Any]] = []
    for row in reliability:
        if row["calls"] < MIN_GOTCHA_CALLS or row["failure_rate"] < MIN_GOTCHA_FAILURE_RATE:
            continue
        classes = {k: v for k, v in row["by_result_class"].items() if k in _BAD}
        dominant = max(classes.items(), key=lambda kv: kv[1], default=("error", 0))
        repair = repair_by_step.get(row["step"])
        retry = retry_by_step.get(row["step"])
        claim = (
            f"`{row['step']}` fails {row['failures']} of {row['calls']} calls "
            f"({row['failure_rate']:.0%}) across {row['sessions']} sessions; "
            f"the dominant failure class is `{dominant[0]}` ({dominant[1]})."
        )
        if row["top_errors"]:
            worst = row["top_errors"][0]
            claim += f' Most common message ({worst["count"]}x): "{worst["message"]}".'

        if repair:
            claim += (
                f" The recurring next move is `{repair['repair_step']}` "
                f"({repair['pairs']} times, succeeding {repair['repair_success_rate']:.0%})."
            )
        elif retry:
            claim += (
                f" No distinct repair recurs; the agent retries the same step "
                f"{retry['pairs']} times and it works {retry['repair_success_rate']:.0%} "
                "of the time."
            )
        claims.append(
            {
                "claim": claim,
                "step": row["step"],
                "calls": row["calls"],
                "failures": row["failures"],
                "failure_rate": row["failure_rate"],
                "sessions": row["sessions"],
                "dominant_failure_class": dominant[0],
                "top_errors": row["top_errors"],
                "by_runtime": row["by_runtime"],
                "receipt_sessions": row["receipt_sessions"],
            }
        )
    claims.sort(key=lambda item: -(item["failure_rate"] * math.log(item["calls"])))
    return claims[:limit]


def mine_motifs(
    traces: list[dict[str, Any]], *, max_len: int = MOTIF_MAX_LEN
) -> list[dict[str, Any]]:
    """Frequent contiguous abstract-step sequences across the whole corpus.

    Abstract rather than raw, so a hermes ``read_file`` and a claude ``Read`` land
    on the same node; otherwise the thin hermes-legacy adapter's tool-only tokens
    simply outvote every other runtime.
    """
    support: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for trace in traces:
        trace_id = str(trace.get("trace_id"))
        sequence = [sig for sig, _ in collapse_runs(
            [step_class(str(e["arg_signature"])) for e in trace.get("events") or []]
        )]
        for length in range(2, max_len + 1):
            for gram in _ngrams(sequence, length):
                support[gram].add(trace_id)
    rows = [
        {"steps": list(gram), "length": len(gram), "traces": len(ids)}
        for gram, ids in support.items()
        if len(ids) >= MIN_MOTIF_TRACES
    ]
    # Keep maximal motifs: a 3-gram whose superset holds the same traces is noise.
    keep: list[dict[str, Any]] = []
    by_gram = {tuple(row["steps"]): row["traces"] for row in rows}
    for row in rows:
        gram = tuple(row["steps"])
        if any(
            other != gram
            and len(other) > len(gram)
            and _contains(other, gram)
            and count >= row["traces"]
            for other, count in by_gram.items()
        ):
            continue
        keep.append(row)
    keep.sort(key=lambda row: (-row["traces"], -row["length"]))
    return keep


def _grade_sort_key(episode: Episode) -> int:
    return GRADE_RANK.get(episode.grade, 0)
