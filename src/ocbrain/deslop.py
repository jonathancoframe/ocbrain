"""Detect slop in stored knowledge, at the moment it is written.

A linter catches code that is mechanically legal but should have been written
differently. This is the same idea for a knowledge store: a belief can be a
well-formed sentence, pass every schema check, and still fail to function as
knowledge. Those beliefs cost a retrieval slot each and drag unrelated material
into answers.

Rules are deterministic, free, and testable. They fire on shape -- several facts
fused into one body, a durable claim written in the present tense, a
present-state claim with no expiry.

These rules are a write-time gate, not a sweep. They run inside
:func:`ocbrain.curator.validate_claims` before a claim is stored, inside
``closeout_v1`` on every closeout summary, and in ``scripts/wiki-lint.py`` over
the materialized tree. That is where they earn their keep: 34 unverified
quotes, 8 fused-claims and 7 temporal-in-durable rejections came from the
curator gate, while the post-hoc sweep that re-ran them over the served corpus
reported `actionable: 0` for 155 consecutive hourly runs and was deleted.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ocbrain.history_window import HISTORY_HEAD_CHARS

# The curator's own prompt asks for "1-3 short sentences", so three is compliant
# and only more than that is fusion. A stricter bar here would flag beliefs for
# meeting the contract they were written to. Semicolons are the stronger signal:
# they join independent clauses, which retrieval can only return or withhold as a
# block -- so a question about session caps drags in approval configuration.
MAX_SENTENCES = 3
MAX_SEMICOLONS = 1

_SENTENCE_END_RE = re.compile(r"[.!?](?:\s|$)")
_TEMPORAL_RE = re.compile(
    r"\b(?:now|currently|recently|just\s+(?:landed|shipped|added|fixed)|"
    r"today|yesterday|as\s+of\s+(?:now|today))\b",
    re.IGNORECASE,
)
_PRESENT_COMPLETION_RE = re.compile(
    r"\b(?:is|are|has\s+been|have\s+been)\s+"
    r"(?:implemented|verified|completed|deployed|fixed|added|shipped)\b",
    re.IGNORECASE,
)
# A date, a version, or an explicit as-of clause is enough to anchor a number.
_ANCHOR_RE = re.compile(
    r"(?:\b\d{4}-\d{2}-\d{2}\b|\bas\s+of\b|\bv\d+(?:\.\d+)+\b|\bsince\b)",
    re.IGNORECASE,
)
# Something a reader could look up or act on: a path, a dotted/underscored
# identifier, a flag, a number, an acronym, or a proper noun. The proper-noun
# arm matters -- a preference or doctrine belief ("prefer the gcloud CLI on
# Coframe prod") is fully actionable while naming no path or figure, and without
# it this rule would reject exactly the beliefs a reader most wants.
_CHECKABLE_RE = re.compile(
    r"(?:[~/][\w./-]+|\w+[._-]\w+|--\w+|\b\d+\b|\b[A-Z]{2,}\b|(?<=[a-z] )[A-Z]\w+)"
)


@dataclass(frozen=True)
class SlopFinding:
    """One rule firing on one belief."""

    rule: str
    repair: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "repair": self.repair, "detail": self.detail}


@dataclass(frozen=True)
class SlopRule:
    id: str
    repair: str
    description: str
    detect: Callable[[str, dict[str, Any]], str | None]
    # Enforced rules gate writes and may be repaired without a human reading the
    # finding first, so a rule earns `enforced` only by being precise enough that
    # a firing is a defect rather than a judgement call. Advisory rules report.
    enforced: bool = True


def _sentence_count(body: str) -> int:
    return max(1, len(_SENTENCE_END_RE.findall(body.strip())))


def _detect_fused_claims(body: str, _attributes: dict[str, Any]) -> str | None:
    sentences = _sentence_count(body)
    semicolons = body.count(";")
    if semicolons > MAX_SEMICOLONS or sentences > MAX_SENTENCES:
        return f"{sentences} sentences, {semicolons} semicolons"
    return None


def _detect_temporal_in_durable(body: str, attributes: dict[str, Any]) -> str | None:
    if str(attributes.get("lifecycle") or "").strip() != "durable":
        return None
    if match := _TEMPORAL_RE.search(body):
        return f"durable belief says {match.group(0)!r}"
    if match := _PRESENT_COMPLETION_RE.search(body):
        return f"durable belief says {match.group(0)!r}"
    return None


def _detect_current_without_expiry(body: str, attributes: dict[str, Any]) -> str | None:
    """A present-state claim that cannot age out.

    An earlier version of this rule tried to flag any precise figure lacking an
    as-of date. It could not tell a stable configured value from a measured count
    -- a 600GB budget does not rot, "542 experiments" does -- so it fired on both.
    The precise, mechanical version of the same concern is lifecycle metadata:
    a belief declared `current` with no expiry has no way to ever be retired.
    """
    if str(attributes.get("lifecycle") or "").strip() != "current":
        return None
    if str(attributes.get("valid_until") or "").strip():
        return None
    anchored = "" if _ANCHOR_RE.search(body) else " and no as-of date in the body"
    return f"lifecycle is current but no valid_until is set{anchored}"


def _detect_no_checkable_content(body: str, _attributes: dict[str, Any]) -> str | None:
    if _CHECKABLE_RE.search(body):
        return None
    return "no path, identifier, flag, figure, or named entity a reader could act on"


RULES: tuple[SlopRule, ...] = (
    SlopRule(
        id="fused-claims",
        repair="split",
        description=(
            "Several independent facts in one body. Retrieval can only return or "
            "withhold the whole block, and half of it cannot be superseded."
        ),
        detect=_detect_fused_claims,
    ),
    SlopRule(
        id="temporal-in-durable",
        repair="rewrite",
        description=(
            "A durable belief written in the present tense is stale by "
            "construction. Either state the timeless rule or mark it current."
        ),
        detect=_detect_temporal_in_durable,
    ),
    SlopRule(
        id="current-without-expiry",
        # Metadata, not prose: the body is fine, the lifecycle bookkeeping is
        # missing. Sending it to a model to be rewritten would spend a hosted call
        # to change the one thing that is not wrong.
        repair="stamp",
        description=(
            "A belief declared `current` with no valid_until can never age out, "
            "so a present-state claim silently becomes a permanent one."
        ),
        detect=_detect_current_without_expiry,
    ),
    SlopRule(
        id="no-checkable-content",
        repair="drop",
        description=(
            "Nothing a reader could verify or act on. A belief that names no path, "
            "identifier, flag, or figure is not knowledge."
        ),
        detect=_detect_no_checkable_content,
        # Advisory, not enforced. A capitalised proper noun at the start of a
        # sentence is indistinguishable from a common word by pattern alone, so
        # "Jonathan wants short direct answers" -- a genuinely actionable
        # preference -- fires the same way vague prose does. Precise enough to
        # put in front of a reader, not precise enough to reject a write or
        # retire a belief unattended.
        enforced=False,
    ),
)

RULE_IDS: tuple[str, ...] = tuple(rule.id for rule in RULES)
ENFORCED_RULE_IDS: tuple[str, ...] = tuple(rule.id for rule in RULES if rule.enforced)
def find_slop(
    body: str,
    attributes: dict[str, Any] | None = None,
    *,
    rules: tuple[str, ...] | None = None,
) -> list[SlopFinding]:
    """Run the mechanical rules over one belief body."""
    resolved_attributes = attributes or {}
    selected = set(rules) if rules else set(RULE_IDS)
    findings: list[SlopFinding] = []
    for rule in RULES:
        if rule.id not in selected:
            continue
        if (detail := rule.detect(body, resolved_attributes)) is not None:
            findings.append(SlopFinding(rule=rule.id, repair=rule.repair, detail=detail))
    return findings


# Long enough that a byte-identical match means the same transcript, short enough
# to stay inside the head half of every window the importer produces. Measured
# against the live corpus: at 2,000 characters the gate suppresses exactly the
# re-windowed rows (102 -> 1, 89 -> 1) and keeps all 476 genuine head changes.
# Same value as the excerpt a pointer row keeps, and imported rather than
# repeated so the gate below cannot drift away from the column it reads.
REWINDOW_HEAD_CHARS = HISTORY_HEAD_CHARS


def rewindowed_evidence_id(
    conn: sqlite3.Connection,
    *,
    source_uri: str,
    kind: str,
    text: str,
    head_chars: int = REWINDOW_HEAD_CHARS,
) -> str | None:
    """The newest evidence row for this source whose head matches ``text``.

    A match means the candidate is the same transcript re-windowed, so recording
    it again would add a near-duplicate no reader ever sees: the curator already
    selects only the newest evidence per source. Returns ``None`` when the head
    differs, which is the signal that the file was rotated or rewritten and the
    new content is genuinely new.

    A pointer row's ``body`` is empty and its excerpt lives in ``body_head``, so
    the comparison reads whichever the row has. Matching on ``body`` alone would
    make every pointer row's head compare equal to the empty string and collapse
    unrelated transcripts onto one evidence id.
    """
    if len(text) < head_chars:
        # Too short to have been windowed at all -- the body IS the file, so a
        # difference anywhere is a real difference.
        return None
    row = conn.execute(
        """
        SELECT evidence_id
        FROM evidence_objects
        WHERE source_uri=? AND kind=?
          AND substr(COALESCE(NULLIF(body, ''), body_head), 1, ?) = ?
        ORDER BY recorded_at DESC
        LIMIT 1
        """,
        (source_uri, kind, head_chars, text[:head_chars]),
    ).fetchone()
    return str(row["evidence_id"]) if row is not None else None
