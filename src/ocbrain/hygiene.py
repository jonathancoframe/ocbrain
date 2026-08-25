"""Retire beliefs that have stopped being worth serving.

The compiler only ever grows the corpus. Without a retirement pass, a brain
accumulates facts that expired or restate a fact it already carries, and
precision decays until someone runs a one-off sweep by hand.

Two independent classes, each separately counted so a run says *why* it acted:

``expired``
    Past its ``valid_until``, or explicitly marked ``superseded_by`` another
    belief. Unambiguous, and the only class that may retire a curated wiki fact.

``redundant``
    An older curator restatement of a fact a newer wiki belief already carries in
    the same delivery scope. The compiler keys a belief by the topic name a model
    chose, so a later run that rewords the same fact under a new key mints a second
    belief instead of updating the first -- exact-body dedup never sees it, and
    every scheduled run adds a phrasing.

There were two more, ``unused`` and ``unhelpful``, and they are gone. Across 155
consecutive scheduled runs neither ever selected a belief. ``unhelpful`` also
refused to act at all until an operator set a feedback watermark, and no
operator ever did -- so the whole watermark subsystem existed to make a class
safe that never fired.

Every retirement is a **soft** retraction, and :func:`restore` undoes one. That
pairing is what makes an unattended sweep defensible: a wrongly retired fact is
one command from serving again. A *hard* retraction would instead block the
belief id permanently, and because compiled ids are content-addressed it would
block all future identical content -- turning a routine cleanup into a permanent
content ban. Tombstoned and hard-corrected beliefs are not restorable;
those were deliberate, permanent decisions.

Nothing here deletes anything. The event ledger is append-only by trigger; a
retirement is a ``correction_recorded`` event that the projector folds in, and so
is its undo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ocbrain.core_v1 import (
    append_core_event,
    is_core_v1,
    project_core_v1,
)
from ocbrain.text import DEFAULT_RESTATEMENT_SIMILARITY, is_restatement

HYGIENE_VERSION = "belief-hygiene-v2"
WRITER = f"maintenance:{HYGIENE_VERSION}"

DEFAULT_BATCH_CAP = 200
# Token overlap above which two served beliefs are treated as one fact restated.
# Conservative on purpose: this runs unattended, and under-retiring leaves a
# little redundancy while over-retiring loses knowledge.
DEFAULT_RESTATEMENT_THRESHOLD = DEFAULT_RESTATEMENT_SIMILARITY

CLASSES = ("expired", "redundant")


def _expired_targets(conn: sqlite3.Connection, *, now: datetime) -> list[dict[str, str]]:
    stamp = now.isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT belief_id,
               json_extract(attributes_json, '$.valid_until') AS valid_until,
               json_extract(attributes_json, '$.superseded_by') AS superseded_by
        FROM current_beliefs
        WHERE status='current' AND serve=1
        ORDER BY belief_id
        """
    ).fetchall()
    targets: list[dict[str, str]] = []
    for row in rows:
        valid_until = str(row["valid_until"] or "").strip()
        superseded_by = str(row["superseded_by"] or "").strip()
        if superseded_by:
            targets.append(
                {
                    "belief_id": str(row["belief_id"]),
                    "reason": "superseded",
                    "detail": f"superseded by {superseded_by}",
                }
            )
        elif valid_until and valid_until < stamp:
            targets.append(
                {
                    "belief_id": str(row["belief_id"]),
                    "reason": "expired",
                    "detail": f"past valid_until {valid_until}",
                }
            )
    return targets


def _redundant_targets(
    conn: sqlite3.Connection, *, threshold: float
) -> list[dict[str, str]]:
    """Retire older wiki restatements within one exact delivery scope.

    The compiler keys a belief by the topic name a model chose, so a later run
    that rewords the same fact under a new key mints a second belief rather than
    updating the first. Exact-body dedup never sees it. Left alone, every
    scheduled run adds another phrasing and each copy costs a retrieval slot.
    """
    rows = list(
        conn.execute(
            """
            SELECT belief_id, body, last_compiled_at,
                   scope_type, scope_id, visibility, egress_policy
            FROM current_beliefs
            WHERE status='current' AND serve=1 AND pinned=0
              AND belief_type='wiki_fact'
            ORDER BY scope_type, scope_id, visibility, egress_policy,
                     last_compiled_at DESC, belief_id
            """
        )
    )
    targets: list[dict[str, str]] = []
    kept_by_scope: dict[tuple[str, str, str, str], list[tuple[str, str]]] = {}
    # Rows arrive newest-first, so the first member of a cluster is the keeper
    # and everything matching it afterwards is an older restatement. Scope and
    # delivery policy are part of the cluster identity: equivalent text in two
    # projects, or under two visibility/egress policies, remains two beliefs.
    for row in rows:
        belief_id = str(row["belief_id"])
        body = str(row["body"])
        scope_key = (
            str(row["scope_type"]),
            str(row["scope_id"]),
            str(row["visibility"]),
            str(row["egress_policy"]),
        )
        kept = kept_by_scope.setdefault(scope_key, [])
        keeper = next(
            (kid for kid, kbody in kept if is_restatement(kbody, body, threshold=threshold)),
            None,
        )
        if keeper is None:
            kept.append((belief_id, body))
            continue
        targets.append(
            {
                "belief_id": belief_id,
                "reason": "redundant",
                "detail": f"restates {keeper}",
            }
        )
    return targets


def plan_retirements(
    conn: sqlite3.Connection,
    *,
    classes: tuple[str, ...] = CLASSES,
    now: datetime | None = None,
    restatement_threshold: float = DEFAULT_RESTATEMENT_THRESHOLD,
    batch_cap: int = DEFAULT_BATCH_CAP,
) -> dict[str, Any]:
    """Select what a run would retire, without writing anything."""
    if not is_core_v1(conn):
        raise ValueError("belief hygiene requires an OCBrain v1 core")
    unknown = sorted(set(classes) - set(CLASSES))
    if unknown:
        raise ValueError(f"unknown hygiene classes: {', '.join(unknown)}")
    resolved_now = now or datetime.now(UTC)

    candidates: list[dict[str, str]] = []
    if "expired" in classes:
        candidates += _expired_targets(conn, now=resolved_now)
    if "redundant" in classes:
        candidates += _redundant_targets(conn, threshold=restatement_threshold)

    # One belief can qualify twice; keep the first (most explicit) reason.
    deduped: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        deduped.setdefault(candidate["belief_id"], candidate)
    ordered = sorted(deduped.values(), key=lambda item: (item["reason"], item["belief_id"]))
    capped = ordered[: max(0, batch_cap)]

    by_reason: dict[str, int] = {}
    for candidate in capped:
        by_reason[candidate["reason"]] = by_reason.get(candidate["reason"], 0) + 1
    return {
        "hygiene_version": HYGIENE_VERSION,
        "classes": sorted(classes),
        "at": resolved_now.isoformat(timespec="seconds"),
        "restatement_threshold": restatement_threshold,
        "batch_cap": batch_cap,
        "eligible_total": len(ordered),
        "selected_total": len(capped),
        # Deferred work is stated rather than silently dropped: a run that hits
        # the cap must not read as "nothing left to do".
        "deferred_by_cap": max(0, len(ordered) - len(capped)),
        "targets_by_reason": by_reason,
        "targets": capped,
    }


def apply_retirements(conn: sqlite3.Connection, plan: dict[str, Any]) -> dict[str, Any]:
    """Soft-retract every belief in ``plan``, then reproject once."""
    targets = list(plan.get("targets") or [])
    if not targets:
        return dict(plan) | {"applied": 0, "applied_belief_ids": []}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for target in targets:
            append_core_event(
                conn,
                "correction_recorded",
                {
                    "schema_version": "ocbrain.correction.v1",
                    "subject": {"kind": "belief", "id": target["belief_id"]},
                    "target_id": target["belief_id"],
                    "target_layer": "belief",
                    "op": "retract",
                    "author": WRITER,
                    "body": f"retired by {HYGIENE_VERSION}: {target['detail']}",
                    # Soft: a hard retraction of a content-addressed id would
                    # permanently block all future identical content.
                    "hard": False,
                },
                writer=WRITER,
                project=False,
            )
        # One projection pass for the whole batch; per-event projection would be
        # quadratic over a large sweep.
        project_core_v1(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(plan) | {
        "applied": len(targets),
        "applied_belief_ids": [target["belief_id"] for target in targets],
    }


def verify_serving_invariants(conn: sqlite3.Connection) -> dict[str, int]:
    """Confirm nothing unserved is still reachable through the search index."""
    leaked = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM search_documents sd
            JOIN current_beliefs cb ON cb.belief_id = sd.doc_id
            WHERE cb.serve = 0 OR cb.status != 'current'
            """
        ).fetchone()[0]
    )
    serving = int(
        conn.execute(
            "SELECT COUNT(*) FROM current_beliefs WHERE serve=1 AND status='current'"
        ).fetchone()[0]
    )
    return {"serving": serving, "unserved_in_search_index": leaked}


def restore(
    conn: sqlite3.Connection,
    *,
    belief_id: str,
    actor: str = WRITER,
    reason: str = "restored by operator",
) -> dict[str, Any]:
    """Undo a soft retraction, putting a belief back into service.

    This is what makes an unattended sweep safe to run: a wrongly retired fact is
    one command away from serving again. Tombstoned and hard-corrected beliefs
    stay terminal -- those were deliberate, permanent decisions.
    """
    from ocbrain.core_v1 import _restore_blocked, get_core_v1_belief

    current = get_core_v1_belief(conn, belief_id)
    if current is None:
        raise ValueError(f"belief not found: {belief_id}")
    if (blocked := _restore_blocked(conn, belief_id)) is not None:
        raise PermissionError(f"cannot restore: belief is {blocked}: {belief_id}")
    if current.get("status") == "current" and current.get("serve"):
        return {"belief_id": belief_id, "status": "current", "changed": False}
    append_core_event(
        conn,
        "correction_recorded",
        {
            "schema_version": "ocbrain.correction.v1",
            "subject": {"kind": "belief", "id": belief_id},
            "target_id": belief_id,
            "target_layer": "belief",
            "op": "restore",
            "author": actor,
            "body": reason,
            "hard": False,
        },
        writer=actor,
        project=True,
    )
    conn.commit()
    return {"belief_id": belief_id, "status": "current", "changed": True}


def supersede(
    conn: sqlite3.Connection,
    *,
    belief_id: str,
    successor_id: str,
    actor: str = WRITER,
) -> dict[str, Any]:
    """Mark ``belief_id`` as superseded by ``successor_id``.

    Recorded as an attribute on the still-serving belief so the wiki can render a
    stale marker and the ``expired`` class can retire it on the next sweep. Goes
    through a fresh proposal rather than a bespoke correction op, so the existing
    approval and block checks apply unchanged.
    """
    from ocbrain.core_v1 import get_core_v1_belief
    from ocbrain.mcp_v1 import decide_proposal_v1

    if belief_id == successor_id:
        raise ValueError("a belief cannot supersede itself")
    current = get_core_v1_belief(conn, belief_id)
    if current is None:
        raise ValueError(f"belief not found: {belief_id}")
    if current.get("status") != "current" or not current.get("serve"):
        raise ValueError(f"belief is not currently served: {belief_id}")
    if get_core_v1_belief(conn, successor_id) is None:
        raise ValueError(f"successor belief not found: {successor_id}")

    attributes = dict(current.get("attributes") or {})
    attributes["superseded_by"] = successor_id
    proposal_id = append_core_event(
        conn,
        "compilation_proposed",
        {
            "schema_version": "ocbrain.compilation.v1",
            "subject": {"kind": "belief", "id": belief_id},
            "belief_id": belief_id,
            "belief_type": current.get("belief_type"),
            "body": current.get("body"),
            "evidence_ids": current.get("evidence_ids") or [],
            "scope": current.get("scope"),
            "confidence": current.get("confidence"),
            "attributes": attributes,
        },
        writer=actor,
    )
    decide_proposal_v1(
        conn,
        proposal_event_id=proposal_id,
        decision="approve",
        actor=actor,
        edited_body=None,
        reason=f"marked superseded by {successor_id}",
    )
    conn.commit()
    return {
        "belief_id": belief_id,
        "superseded_by": successor_id,
        "attributes": json.loads(json.dumps(attributes, sort_keys=True)),
    }


__all__ = [
    "CLASSES",
    "DEFAULT_BATCH_CAP",
    "HYGIENE_VERSION",
    "apply_retirements",
    "plan_retirements",
    "restore",
    "supersede",
    "verify_serving_invariants",
]
