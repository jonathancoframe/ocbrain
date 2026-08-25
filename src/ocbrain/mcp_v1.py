"""MCP-facing operations for the event-authoritative v1 core.

This module is deliberately separate from the legacy compatibility dispatcher.
It never queries a legacy relational knowledge table or a companion store.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from ocbrain.closeout import record_closeout
from ocbrain.config import load_config
from ocbrain.core_v1 import (
    CORE_V1_SCHEMA_VERSION,
    append_core_event,
    automatic_activation_enabled,
    canonical_json,
    compilation_block_reason,
    get_core_v1_belief,
    get_core_v1_evidence,
    is_core_v1,
    now_iso,
    record_core_v1_evidence,
    record_core_v1_retrieval,
    resolve_object_id,
    search_core_v1,
    sha256_text,
)
from ocbrain.deslop import ENFORCED_RULE_IDS, find_slop
from ocbrain.events import SKILL_TELEMETRY_KINDS, validate_skill_telemetry
from ocbrain.ids import stable_id
from ocbrain.provenance import Provenance
from ocbrain.scope import (
    HOSTED_MODEL_TARGET,
    LOCAL_MODEL_TARGET,
    ScopeContext,
    ScopeTag,
    egress_allowed,
    normalize_delivery_target,
    resolve_write_scope,
)
from ocbrain.shared_context import issue_source_handles

CONTEXT_SCHEMA_VERSION = "ocbrain.context.v1"
SOURCE_SCHEMA_VERSION = "ocbrain.source.v1"
DIGEST_SCHEMA_VERSION = "ocbrain.digest.v1"
MAX_CONTEXT_PACKET_BYTES = 32_000
MAX_CONTEXT_QUERY_CHARS = 4_000
MAX_ITEM_EXCERPT_CHARS = 1_600
MAX_ITEM_SOURCE_HANDLES = 3
RETRIEVAL_ID_PLACEHOLDER = "ret_0000000000000000"

AUTO_COMPILE_BELIEF_TYPE = "auto_compiled"
AUTO_COMPILE_CONFIDENCE = 0.6
AUTO_COMPILE_TITLE_CHARS = 80


def _auto_compile_title(body: str) -> str:
    line = body.strip().splitlines()[0] if body.strip() else "auto-compiled belief"
    return line[:AUTO_COMPILE_TITLE_CHARS]


def auto_compile_scope(context: ScopeContext) -> ScopeTag:
    """Scope for unattended promotion: broadest shared context, never hosted.

    Continuity across clients means a belief compiled while Claude Code worked
    should be recallable by Codex or Cursor on the same project. So this prefers
    the widest *shared* scope (project, then repo, then client) rather than the
    narrowest one ``resolve_write_scope`` picks. Egress stays ``local_only`` so
    automation can never promote content into hosted-model delivery; visibility
    is ``internal`` so same-instance clients share it. Task/session-only or
    empty contexts fall back to the standard narrow write scope.
    """
    for scope_type, value in (
        ("project", context.project),
        ("repo", context.repo),
        ("client", context.client),
    ):
        if value:
            return ScopeTag(
                scope_type,
                f"{scope_type}:{value}",
                visibility="internal",
                egress_policy="local_only",
                provenance="auto_compiled",
            )
    return resolve_write_scope(context)


def auto_compile_evidence(
    conn: sqlite3.Connection,
    *,
    evidence_id: str,
    body: str,
    scope: ScopeTag,
    actor: str,
    source_kind: str,
) -> str:
    """Promote one just-recorded evidence into a served belief, no human review.

    Called only when ``automatic_activation`` is enabled. The belief inherits
    the evidence scope and visibility verbatim, so unattended promotion can
    never widen egress (confidential/local_only evidence yields a
    confidential/local_only belief). The belief id is content-and-scope stable,
    so re-ingesting identical evidence converges on one belief instead of
    appending duplicate compilation events.
    """
    belief_id = stable_id("belief", "auto", body, scope.scope_id)
    scope_dict = scope.to_dict()
    existing = get_core_v1_belief(conn, belief_id)
    if (
        existing is not None
        and existing.get("status") == "current"
        and bool(existing.get("serve"))
        and str(existing.get("body")) == body
        and existing.get("scope") == scope_dict
    ):
        return belief_id
    proposal_id = append_core_event(
        conn,
        "compilation_proposed",
        {
            "schema_version": "ocbrain.compilation.v1",
            "subject": {"kind": "belief", "id": belief_id},
            "belief_id": belief_id,
            "belief_type": AUTO_COMPILE_BELIEF_TYPE,
            "body": body,
            "evidence_ids": [evidence_id],
            "scope": scope_dict,
            "confidence": AUTO_COMPILE_CONFIDENCE,
            "reward_band": "weak",
            "attributes": {
                "title": _auto_compile_title(body),
                "auto_compiled": True,
                "source_kind": source_kind,
                "source_evidence_id": evidence_id,
                "content_sha256": sha256_text(body),
                "lifecycle": "durable",
            },
        },
        writer=actor,
    )
    decide_proposal_v1(
        conn,
        proposal_event_id=proposal_id,
        decision="approve",
        actor=actor,
        edited_body=None,
        reason="automatic_activation",
    )
    return belief_id


def _scope_fallback_enabled() -> bool:
    """Whether an empty scoped pass may retry across scopes.

    Fails open to the shipped default: a malformed config must not decide a
    serving policy by accident.
    """
    try:
        return bool(load_config().retrieval.scope_fallback_enabled)
    except Exception:  # noqa: BLE001 - config problems must not break serving
        return True


def build_context_v1(
    conn: sqlite3.Connection,
    query: str,
    *,
    context: ScopeContext,
    limit: int,
    cross_scope: bool,
    delivery_target: str = LOCAL_MODEL_TARGET,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build one context packet, retrying across scopes only when it is empty.

    ``cross_scope`` is an opt-in almost no caller knows to send, so a query the
    brain could answer from a neighbouring project abstained instead. When the
    scoped pass returns nothing at all, run it once more across scopes and say so
    in ``coverage.scope_fallback``.

    This is a reach change, not a quality change. The second pass is the same
    primitive with the same floors, redundancy filter, and dedup; it widens which
    rows are candidates and nothing else. There is no merge: a scoped pass that
    returned anything is returned untouched, so scoped results are never diluted.
    When the retry is also empty, the caller gets the scoped packet's own
    accounting rather than the wider pass's, because that is the accounting that
    describes their scope.
    """
    packet, handles = _context_pass(
        conn,
        query,
        context=context,
        limit=limit,
        cross_scope=cross_scope,
        delivery_target=delivery_target,
    )
    if packet["items"] or cross_scope or not _scope_fallback_enabled():
        return _enforce_context_packet_limit(packet, handles)
    coverage = packet["coverage"]
    fallback = {
        "mode": "cross_scope_auto",
        "first_pass_eligible_count": int(coverage["ranking"].get("eligible_count") or 0),
        "first_pass_excluded_scope_count": int(coverage.get("excluded_scope_count") or 0),
    }
    wider, wider_handles = _context_pass(
        conn,
        query,
        context=context,
        limit=limit,
        cross_scope=True,
        delivery_target=delivery_target,
    )
    if wider["items"]:
        # Keep reporting what the CALLER asked for; the retry is coverage detail,
        # not a rewrite of their request.
        wider["cross_scope"] = bool(cross_scope)
        packet, handles = wider, wider_handles
    packet["coverage"]["scope_fallback"] = fallback
    return _enforce_context_packet_limit(packet, handles)


def _context_pass(
    conn: sqlite3.Connection,
    query: str,
    *,
    context: ScopeContext,
    limit: int,
    cross_scope: bool,
    delivery_target: str = LOCAL_MODEL_TARGET,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One retrieval pass, packaged but not yet budgeted.

    The packet-limit enforcement is deliberately left to the caller so a
    fallback marker can be added before the byte accounting is settled.
    """
    _require_v1(conn)
    delivery_target = normalize_delivery_target(delivery_target)
    raw = search_core_v1(
        conn,
        query,
        context=context,
        limit=limit,
        cross_scope=cross_scope,
        delivery_target=delivery_target,
    )
    handles: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    delivery_excluded = 0
    for raw_item in raw["items"]:
        if not _scope_allowed_for_delivery(
            raw_item.get("scope"),
            context=context,
            delivery_target=delivery_target,
            cross_scope=cross_scope,
        ):
            delivery_excluded += 1
            continue
        item_handles = _source_handles_for_belief(
            conn,
            str(raw_item["belief_id"]),
            context=context,
            delivery_target=delivery_target,
            cross_scope=cross_scope,
        )
        item_handles = item_handles[:MAX_ITEM_SOURCE_HANDLES]
        handles.extend(item_handles)
        if not item_handles:
            unavailable.append(
                {"object_id": str(raw_item["belief_id"]), "reason": "no_expandable_source"}
            )
        excerpt, excerpt_truncated = _bounded_excerpt(
            str(raw_item.get("body") or ""), max_chars=MAX_ITEM_EXCERPT_CHARS
        )
        items.append(
            {
                "id": str(raw_item["belief_id"]),
                "kind": "core_v1",
                "excerpt": excerpt,
                "excerpt_truncated": excerpt_truncated,
                "scope": dict(raw_item.get("scope") or {}),
                "score": float(raw_item.get("score") or 0.0),
                "relevance": float(raw_item.get("relevance") or 0.0),
                "confidence": float(raw_item.get("confidence") or 0.0),
                "confidence_band": str(raw_item.get("confidence_band") or "unknown"),
                "status": "current",
                "evidence_ids": _evidence_ids_for_delivery(
                    conn,
                    raw_item.get("evidence_ids") or [],
                    context=context,
                    delivery_target=delivery_target,
                    cross_scope=cross_scope,
                ),
                "sources": [_public_source_handle(value) for value in item_handles],
                "ranking": dict(raw_item.get("ranking") or {}),
            }
        )
    handles = _dedupe_handles(handles)
    packet = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "core_schema": CORE_V1_SCHEMA_VERSION,
        "delivery_target": delivery_target,
        "query": query[:MAX_CONTEXT_QUERY_CHARS],
        "resolved_context": context.to_dict(),
        "cross_scope": bool(cross_scope),
        "at_ts": None,
        "items": items,
        "contradictions": _explicit_contradictions(conn, items),
        "coverage": {
            "requested_limit": limit,
            "returned": len(items),
            "feedback_needed": len(items) > 0,
            "excluded_scope_count": int(raw.get("excluded_count") or 0),
            "excluded_delivery_count": (
                int(raw.get("delivery_excluded_count") or 0) + delivery_excluded
            ),
            "exclusion_count_basis": str(
                raw.get("exclusion_count_basis") or "current_serving_inventory"
            ),
            "excluded_sample": (
                [] if delivery_target != LOCAL_MODEL_TARGET else list(raw.get("excluded") or [])
            ),
            "estimated_tokens": 0,
            "serialized_bytes": 0,
            "hard_packet_limit_bytes": MAX_CONTEXT_PACKET_BYTES,
            "source_handle_count": len(handles),
            "unavailable_sources": unavailable,
            "ranking": dict(raw.get("ranking") or {}),
        },
    }
    return packet, handles


def record_context_v1(
    conn: sqlite3.Connection,
    packet: dict[str, Any],
    handles: list[dict[str, Any]],
    *,
    context: ScopeContext,
    delivery_target: str = LOCAL_MODEL_TARGET,
    provenance: Provenance | None = None,
) -> str:
    delivery_target = normalize_delivery_target(delivery_target)
    retrieval_id = record_core_v1_retrieval(
        conn,
        query=str(packet["query"]),
        context={**context.to_dict(), "delivery_target": delivery_target},
        items=[{"belief_id": item["id"], "score": item["score"]} for item in packet["items"]],
        runtime=context.runtime or "mcp",
        task_ref=context.task or f"brain.context:{packet['query']}",
        session_id=context.session,
        packet_schema=CONTEXT_SCHEMA_VERSION,
        provenance=provenance,
    )
    issue_source_handles(conn, handles, retrieval_use_id=retrieval_id)
    return retrieval_id


def expand_source_v1(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    context: ScopeContext,
    max_chars: int,
    delivery_target: str = LOCAL_MODEL_TARGET,
) -> dict[str, Any]:
    _require_v1(conn)
    delivery_target = normalize_delivery_target(delivery_target)
    row = conn.execute("SELECT * FROM context_source_handles WHERE id=?", (source_id,)).fetchone()
    if row is None:
        raise ValueError(f"source handle not found: {source_id}")
    scope = ScopeTag.from_dict(json.loads(row["scope_json"]))
    _authorize_delivery_scope(
        scope,
        context=context,
        delivery_target=delivery_target,
        scope_error="source scope does not match the supplied context",
    )
    locator = json.loads(row["locator_json"])
    if row["source_kind"] == "core_v1_evidence":
        source = get_core_v1_evidence(conn, str(locator["evidence_id"]))
        if source is None:
            raise ValueError("issued evidence source no longer exists")
        belief = get_core_v1_belief(conn, str(row["object_id"]))
        if belief is None or belief.get("status") != "current" or not belief.get("serve"):
            raise PermissionError("issued source is no longer linked to a current belief")
        _authorize_delivery_scope(
            ScopeTag.from_dict(belief.get("scope")),
            context=context,
            delivery_target=delivery_target,
            scope_error="source belief scope no longer matches the supplied context",
        )
        _authorize_delivery_scope(
            ScopeTag.from_dict(source.get("scope")),
            context=context,
            delivery_target=delivery_target,
            scope_error="source evidence scope no longer matches the supplied context",
        )
        linked = conn.execute(
            "SELECT 1 FROM belief_evidence WHERE belief_id=? AND evidence_id=? "
            "AND relation='supports'",
            (belief["canonical_id"], source["canonical_id"]),
        ).fetchone()
        if linked is None:
            raise PermissionError("issued evidence is no longer current support for this belief")
        content = str(source["body"])
    elif row["source_kind"] == "core_v1_belief":
        source = get_core_v1_belief(conn, str(locator["belief_id"]))
        if source is None:
            raise ValueError("issued belief source no longer exists")
        if source.get("status") != "current" or not source.get("serve"):
            raise PermissionError("issued belief source is no longer current")
        _authorize_delivery_scope(
            ScopeTag.from_dict(source.get("scope")),
            context=context,
            delivery_target=delivery_target,
            scope_error="source belief scope no longer matches the supplied context",
        )
        content = str(source["body"])
    else:
        raise ValueError(f"unsupported v1 source kind: {row['source_kind']}")
    actual_hash = sha256_text(content)
    if actual_hash != row["content_hash"]:
        raise ValueError("source changed after issuance; request a fresh brain.context handle")
    excerpt, truncated = _bounded_excerpt(content, max_chars=max_chars)
    issued_by_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM context_source_handle_issues WHERE source_id=?",
            (source_id,),
        ).fetchone()[0]
    )
    issued_by = [
        str(item["retrieval_use_id"])
        for item in conn.execute(
            "SELECT retrieval_use_id FROM context_source_handle_issues "
            "WHERE source_id=? ORDER BY issued_at DESC, retrieval_use_id DESC LIMIT 8",
            (source_id,),
        )
    ]
    uri = row["uri"]
    if delivery_target == HOSTED_MODEL_TARGET:
        if row["source_kind"] == "core_v1_evidence":
            uri = f"ocbrain://evidence/{locator['evidence_id']}"
        else:
            uri = f"ocbrain://belief/{locator['belief_id']}"
    return {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "core_schema": CORE_V1_SCHEMA_VERSION,
        "delivery_target": delivery_target,
        "id": str(row["id"]),
        "object_id": str(row["object_id"]),
        "kind": str(row["source_kind"]),
        "uri": uri,
        "scope": scope.to_dict(),
        "content_hash": str(row["content_hash"]),
        "hash_verified": True,
        "content": excerpt,
        "truncated": truncated,
        "characters": len(excerpt),
        "issued_at": str(row["issued_at"]),
        "origin_retrieval_use_id": row["retrieval_use_id"],
        "issued_by_count": issued_by_count,
        "issued_by_retrieval_use_ids": issued_by,
    }


EXACT_MATCH_LIMIT = 8
EXACT_MATCH_MAX_QUERY_CHARS = 512
_SHA256_TEXT_RE = re.compile(r"^[0-9a-f]{64}$")
_STABLE_OBJECT_ID_RE = re.compile(r"^(?:evt|evd|belief|close|ret)_[0-9a-f]{16}$")
_URI_REFERENCE_RE = re.compile(r"^[a-z][a-z0-9+.-]*:\S+$", re.IGNORECASE)
_TERMINAL_ARTIFACT_URI_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://\S+|ocbrain-bundle:sha256:[0-9a-f]{64}|"
    r"closeout:close_[0-9a-f]{16})$",
    re.IGNORECASE,
)


def _looks_like_exact_locator(query: str) -> bool:
    text = str(query).strip()
    lowered = text.lower()
    return bool(
        _STABLE_OBJECT_ID_RE.fullmatch(lowered)
        or _SHA256_TEXT_RE.fullmatch(lowered)
        or _TERMINAL_ARTIFACT_URI_RE.fullmatch(text)
    )


def exact_lookup_v1(
    conn: sqlite3.Connection,
    query: str,
    *,
    context: ScopeContext,
    cross_scope: bool = False,
    delivery_target: str = LOCAL_MODEL_TARGET,
    limit: int = EXACT_MATCH_LIMIT,
) -> list[dict[str, Any]]:
    """Exact-locator pre-pass for ``brain.search`` on the v1 core.

    Semantic ranking cannot answer "show me closeout X" or "the artifact with
    hash H": a locator string shares no lexical terms with unrelated belief
    bodies, so stale beliefs outrank the exact record. When the query *is* a
    locator, equality lookups short-circuit ranking. A locator is an event,
    evidence, belief, closeout, or retrieval-use id, an artifact URI or
    SHA-256, or an exact ``task_ref`` on a recorded closeout.

    Matches are metadata-only and scope-gated like any other delivery; expand
    bodies through ``brain.get`` / ``brain.source``. ``retrieval_uses.task_ref``
    is deliberately *not* matched: those refs are auto-derived from past query
    text (``brain.search:<query>``), so matching them would let a repeated
    search hijack itself.
    """
    delivery_target = normalize_delivery_target(delivery_target)
    text = str(query).strip()
    if not text or len(text) > EXACT_MATCH_MAX_QUERY_CHARS:
        return []
    limit = max(int(limit), 1)
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, object_id: str, matched_by: str, **fields: Any) -> None:
        key = (kind, object_id)
        if key in seen:
            return
        seen.add(key)
        matches.append(
            {
                "kind": kind,
                "id": object_id,
                "matched_by": matched_by,
                **{k: v for k, v in fields.items() if v is not None},
            }
        )

    def _evidence_scope_allowed(evidence: dict[str, Any]) -> bool:
        return _scope_allowed_for_delivery(
            evidence.get("scope"),
            context=context,
            delivery_target=delivery_target,
            cross_scope=cross_scope,
        )

    def _stored_context_allowed(raw: Any, *, task_ref: str | None = None) -> bool:
        if delivery_target != LOCAL_MODEL_TARGET:
            return False
        try:
            stored = json.loads(str(raw or "{}"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(stored, dict):
            return False
        if not any(
            (
                context.project,
                context.repo,
                context.client,
                context.task,
                context.session,
            )
        ):
            return False
        if (
            context.session
            and not any((context.project, context.repo, context.client, context.task))
        ):
            return str(stored.get("session") or "") == context.session
        return _closeout_matches_context(
            {"context": stored, "task_ref": task_ref},
            context,
        )

    def _event_scope_allowed(
        event: sqlite3.Row,
        *,
        visited: frozenset[str] = frozenset(),
    ) -> bool:
        event_id = str(event["id"])
        if event_id in visited or len(visited) >= 4:
            return False
        visited = visited | {event_id}
        try:
            body = json.loads(str(event["body_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(body, dict):
            return False
        raw_scope = body.get("scope")
        if isinstance(raw_scope, dict):
            return _scope_allowed_for_delivery(
                raw_scope,
                context=context,
                delivery_target=delivery_target,
                cross_scope=cross_scope,
            )
        subject = body.get("subject")
        if not isinstance(subject, dict):
            return False
        subject_id = str(subject.get("id") or "")
        subject_kind = str(subject.get("kind") or "")
        if subject_kind == "evidence":
            evidence = get_core_v1_evidence(conn, subject_id)
            return evidence is not None and _evidence_scope_allowed(evidence)
        if subject_kind == "belief":
            belief = get_core_v1_belief(conn, subject_id)
            return belief is not None and _scope_allowed_for_delivery(
                belief.get("scope"),
                context=context,
                delivery_target=delivery_target,
                cross_scope=cross_scope,
            )
        if subject_kind in {"event", "proposal"}:
            parent = conn.execute(
                "SELECT id, body_json FROM brain_events WHERE id=?",
                (subject_id,),
            ).fetchone()
            return parent is not None and _event_scope_allowed(parent, visited=visited)
        return False

    def _add_evidence(evidence_id: str, matched_by: str) -> None:
        evidence = get_core_v1_evidence(conn, evidence_id)
        if evidence is None or not _evidence_scope_allowed(evidence):
            return
        scope = evidence.get("scope") or {}
        add(
            "evidence",
            str(evidence["evidence_id"]),
            matched_by,
            evidence_kind=str(evidence["kind"]),
            artifact_uri=(
                evidence.get("artifact_uri")
                if delivery_target == LOCAL_MODEL_TARGET
                else f"ocbrain://evidence/{evidence['evidence_id']}"
            ),
            artifact_hash=evidence.get("artifact_hash"),
            content_hash=evidence.get("content_hash"),
            occurred_at=evidence.get("occurred_at"),
            scope_id=scope.get("scope_id"),
        )

    def _add_closeout(row: sqlite3.Row, matched_by: str) -> None:
        if not _stored_context_allowed(row["context_json"], task_ref=str(row["task_ref"])):
            return
        add(
            "closeout",
            str(row["id"]),
            matched_by,
            task_ref=str(row["task_ref"]),
            status=str(row["status"]),
            closed_at=str(row["closed_at"]),
        )

    # Stable-id equality lookups (primary keys / unique columns only).
    event = conn.execute(
        "SELECT id, ts, kind, writer, body_json FROM brain_events WHERE id=?", (text,)
    ).fetchone()
    if event is not None and _event_scope_allowed(event):
        add(
            "event",
            str(event["id"]),
            "id",
            ts=str(event["ts"]),
            event_kind=str(event["kind"]),
            writer=(
                str(event["writer"])
                if delivery_target == LOCAL_MODEL_TARGET
                else None
            ),
        )
    _add_evidence(text, "id")
    belief = get_core_v1_belief(conn, text)
    if belief is not None and _scope_allowed_for_delivery(
        belief.get("scope"),
        context=context,
        delivery_target=delivery_target,
        cross_scope=cross_scope,
    ):
        add(
            "belief",
            str(belief["belief_id"]),
            "id",
            status=str(belief.get("status") or ""),
            belief_type=belief.get("belief_type"),
            confidence=belief.get("confidence"),
            scope_id=(belief.get("scope") or {}).get("scope_id"),
        )
    closeout = conn.execute(
        "SELECT id, task_ref, status, closed_at, context_json "
        "FROM task_closeouts WHERE id=?",
        (text,),
    ).fetchone()
    if closeout is not None:
        _add_closeout(closeout, "id")
    retrieval = conn.execute(
        "SELECT id, task_ref, outcome, served_at, context_json "
        "FROM retrieval_uses WHERE id=?",
        (text,),
    ).fetchone()
    if retrieval is not None and _stored_context_allowed(
        retrieval["context_json"],
        task_ref=str(retrieval["task_ref"] or ""),
    ):
        add(
            "retrieval_use",
            str(retrieval["id"]),
            "id",
            task_ref=retrieval["task_ref"],
            outcome=str(retrieval["outcome"]),
            served_at=str(retrieval["served_at"]),
        )

    # Exact task_ref on recorded closeouts ("show me closeout X").
    for row in conn.execute(
        "SELECT id, task_ref, status, closed_at, context_json FROM task_closeouts "
        "WHERE task_ref=? ORDER BY closed_at DESC LIMIT ?",
        (text, limit),
    ):
        _add_closeout(row, "task_ref")

    # Artifact URI equality (evidence columns, then closeout artifact refs).
    # The uri columns are unindexed, so only scan them for path- or URI-like
    # queries. The broader URI syntax permits exact matches for stored opaque
    # references, while terminal-miss handling remains limited to known forms.
    if "/" in text or _URI_REFERENCE_RE.fullmatch(text):
        for row in conn.execute(
            "SELECT evidence_id FROM evidence_objects "
            "WHERE artifact_uri=? OR source_uri=? LIMIT ?",
            (text, text, limit),
        ):
            _add_evidence(str(row["evidence_id"]), "artifact_uri")
        for row in conn.execute(
            "SELECT id, task_ref, status, closed_at, context_json, artifact_refs_json "
            "FROM task_closeouts WHERE artifact_refs_json LIKE '%' || ? || '%' LIMIT ?",
            (text, limit * 4),
        ):
            refs = json.loads(row["artifact_refs_json"] or "[]")
            if any(
                isinstance(ref, dict) and str(ref.get("uri") or "") == text for ref in refs
            ):
                _add_closeout(row, "artifact_uri")

    # SHA-256 equality (evidence hashes, closeout receipt hash, artifact refs).
    lowered = text.lower()
    if _SHA256_TEXT_RE.match(lowered):
        for row in conn.execute(
            "SELECT evidence_id, artifact_hash, content_hash FROM evidence_objects "
            "WHERE artifact_hash=? OR content_hash=? LIMIT ?",
            (lowered, lowered, limit),
        ):
            matched = (
                "artifact_sha256"
                if str(row["artifact_hash"] or "") == lowered
                else "content_sha256"
            )
            _add_evidence(str(row["evidence_id"]), matched)
        receipt = conn.execute(
            "SELECT id, task_ref, status, closed_at, context_json FROM task_closeouts "
            "WHERE content_hash=?",
            (lowered,),
        ).fetchone()
        if receipt is not None:
            _add_closeout(receipt, "content_sha256")
        for row in conn.execute(
            "SELECT id, task_ref, status, closed_at, context_json, artifact_refs_json "
            "FROM task_closeouts WHERE artifact_refs_json LIKE '%' || ? || '%' LIMIT ?",
            (lowered, limit * 4),
        ):
            refs = json.loads(row["artifact_refs_json"] or "[]")
            if any(
                isinstance(ref, dict) and str(ref.get("sha256") or "").lower() == lowered
                for ref in refs
            ):
                _add_closeout(row, "artifact_sha256")

    return matches[:limit]


def search_v1(
    conn: sqlite3.Connection,
    query: str,
    *,
    context: ScopeContext,
    limit: int,
    cross_scope: bool,
    delivery_target: str = LOCAL_MODEL_TARGET,
    provenance: Provenance | None = None,
) -> dict[str, Any]:
    exact_matches = exact_lookup_v1(
        conn,
        query,
        context=context,
        cross_scope=cross_scope,
        delivery_target=delivery_target,
        limit=min(limit, EXACT_MATCH_LIMIT),
    )
    if exact_matches or _looks_like_exact_locator(query):
        payload = {
            "schema_version": "ocbrain.search.v1",
            "delivery_target": delivery_target,
            "query": query,
            "resolved_context": context.to_dict(),
            "match_mode": "exact",
            "items": [],
            "exact_matches": exact_matches,
            "contradictions": [],
            "coverage": {
                "requested_limit": limit,
                "returned": len(exact_matches),
                "feedback_needed": bool(exact_matches),
            },
        }
        retrieval_id = record_core_v1_retrieval(
            conn,
            query=str(payload["query"]),
            context={**context.to_dict(), "delivery_target": payload["delivery_target"]},
            items=[
                {
                    "object_id": item["id"],
                    "object_kind": item["kind"],
                    "score": 1.0,
                }
                for item in exact_matches
            ],
            runtime=context.runtime or "mcp",
            task_ref=context.task or f"brain.search:{payload['query']}",
            session_id=context.session,
            packet_schema="ocbrain.search.v1",
            provenance=provenance,
        )
        payload["retrieval_use_id"] = retrieval_id
        payload["retrieval_use_status"] = "recorded"
        return payload
    packet, handles = build_context_v1(
        conn,
        query,
        context=context,
        limit=limit,
        cross_scope=cross_scope,
        delivery_target=delivery_target,
    )
    payload = {
        "schema_version": "ocbrain.search.v1",
        "delivery_target": packet["delivery_target"],
        "query": packet["query"],
        "resolved_context": context.to_dict(),
        "items": packet["items"],
        "contradictions": packet["contradictions"],
        "coverage": packet["coverage"],
    }
    payload, handles = prepare_retrieval_packet_v1(payload, handles)
    retrieval_id = record_core_v1_retrieval(
        conn,
        query=str(payload["query"]),
        context={**context.to_dict(), "delivery_target": payload["delivery_target"]},
        items=[{"belief_id": item["id"], "score": item["score"]} for item in payload["items"]],
        runtime=context.runtime or "mcp",
        task_ref=context.task or f"brain.search:{payload['query']}",
        session_id=context.session,
        packet_schema="ocbrain.search.v1",
        provenance=provenance,
    )
    issue_source_handles(conn, handles, retrieval_use_id=retrieval_id)
    bind_retrieval_id_v1(payload, retrieval_id)
    return payload


def get_v1(
    conn: sqlite3.Connection,
    object_id: str,
    *,
    context: ScopeContext,
    include_candidate: bool = False,
    include_private: bool = False,
    cross_scope: bool = False,
    delivery_target: str = LOCAL_MODEL_TARGET,
) -> dict[str, Any]:
    delivery_target = normalize_delivery_target(delivery_target)
    belief = get_core_v1_belief(conn, object_id)
    if belief is not None:
        _authorize_get_scope(
            belief["scope"],
            context=context,
            include_private=include_private,
            cross_scope=cross_scope,
            delivery_target=delivery_target,
        )
        attributes = belief.get("attributes") or {}
        if attributes.get("quarantine_reason"):
            raise PermissionError("quarantined beliefs are not served by brain.get")
        if belief.get("status") != "current" or not belief.get("serve"):
            if not (include_candidate and belief.get("status") == "candidate"):
                raise PermissionError("non-current beliefs are not served by brain.get")
        public_belief = _belief_for_delivery(belief, delivery_target=delivery_target)
        public_belief["evidence_ids"] = _evidence_ids_for_delivery(
            conn,
            belief.get("evidence_ids") or [],
            context=context,
            delivery_target=delivery_target,
            cross_scope=cross_scope,
        )
        return {
            "schema_version": "ocbrain.object.v1",
            "delivery_target": delivery_target,
            "object_kind": "belief",
            **public_belief,
        }
    evidence = get_core_v1_evidence(conn, object_id)
    if evidence is not None:
        _authorize_get_scope(
            evidence["scope"],
            context=context,
            include_private=include_private,
            cross_scope=cross_scope,
            delivery_target=delivery_target,
        )
        return {
            "schema_version": "ocbrain.object.v1",
            "delivery_target": delivery_target,
            "object_kind": "evidence",
            **_evidence_for_delivery(evidence, delivery_target=delivery_target),
        }
    raise ValueError(f"object not found: {object_id}")


def _authorize_get_scope(
    raw_scope: dict[str, Any],
    *,
    context: ScopeContext,
    include_private: bool,
    cross_scope: bool,
    delivery_target: str,
) -> None:
    scope = ScopeTag.from_dict(raw_scope)
    _authorize_delivery_scope(
        scope,
        context=context,
        delivery_target=delivery_target,
        cross_scope=cross_scope,
        scope_error="object scope does not match the supplied context",
    )
    if scope.confidential and not include_private:
        raise PermissionError("confidential objects require explicit include_private")


def _belief_for_delivery(belief: dict[str, Any], *, delivery_target: str) -> dict[str, Any]:
    if delivery_target == LOCAL_MODEL_TARGET:
        return dict(belief)
    attributes = belief.get("attributes") or {}
    safe_attribute_keys = {
        "title",
        "curated",
        "manifest_schema",
        "curation_sha256",
        "source_quality",
        "lifecycle",
        "content_sha256",
        "contradicts",
        "contradiction_ids",
    }
    safe_attributes = {key: attributes[key] for key in safe_attribute_keys if key in attributes}
    attestations = attributes.get("source_attestations")
    if isinstance(attestations, list):
        safe_attributes["source_attestations"] = [
            {key: value[key] for key in ("ref", "sha256") if key in value}
            for value in attestations
            if isinstance(value, dict)
        ]
    keys = {
        "requested_id",
        "canonical_id",
        "belief_id",
        "body",
        "belief_type",
        "scope",
        "confidence",
        "confidence_band",
        "status",
        "serve",
        "pinned",
        "last_compiled_at",
    }
    return {
        **{key: belief[key] for key in keys if key in belief},
        "attributes": safe_attributes,
    }


def _evidence_for_delivery(evidence: dict[str, Any], *, delivery_target: str) -> dict[str, Any]:
    if delivery_target == LOCAL_MODEL_TARGET:
        return dict(evidence)
    keys = {
        "requested_id",
        "canonical_id",
        "evidence_id",
        "body",
        "kind",
        "content_hash",
        "source_content_hash",
        "verifier_status",
        "occurred_at",
        "recorded_at",
        "scope",
    }
    return {key: evidence[key] for key in keys if key in evidence}


def digest_v1(
    conn: sqlite3.Connection,
    *,
    context: ScopeContext,
    limit: int,
    since: str | None = None,
    delivery_target: str = LOCAL_MODEL_TARGET,
) -> dict[str, Any]:
    _require_v1(conn)
    delivery_target = normalize_delivery_target(delivery_target)
    if delivery_target == LOCAL_MODEL_TARGET:
        counts = {
            name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in (
                "brain_events",
                "evidence_objects",
                "current_beliefs",
                "belief_evidence",
                "retrieval_uses",
                "task_closeouts",
            )
        }
    else:
        compatible = sorted(context.compatible_scope_ids())
        placeholders = ",".join("?" for _ in compatible)
        counts = {
            "eligible_current_beliefs": int(
                conn.execute(
                    f"SELECT COUNT(*) FROM current_beliefs WHERE serve=1 "
                    f"AND status='current' AND egress_policy='hosted_ok' "
                    f"AND visibility NOT IN ('confidential','secret') "
                    f"AND scope_id IN ({placeholders})",  # noqa: S608
                    compatible,
                ).fetchone()[0]
            )
        }
    rows = conn.execute(
        "SELECT * FROM current_beliefs WHERE serve=1 AND status='current' "
        "ORDER BY pinned DESC, last_compiled_at DESC, belief_id LIMIT ?",
        (max(limit * 8, 40),),
    )
    current: list[dict[str, Any]] = []
    excluded = 0
    for row in rows:
        scope = ScopeTag(
            str(row["scope_type"]),
            str(row["scope_id"]),
            visibility=str(row["visibility"]),
            egress_policy=str(row["egress_policy"]),
            provenance=str(row["scope_provenance"]),
        )
        allowed, _reason = egress_allowed(scope, context, delivery_target)
        if not allowed:
            excluded += 1
            continue
        current.append(
            {
                "id": str(row["belief_id"]),
                "body": _bounded_excerpt(str(row["body"]), max_chars=MAX_ITEM_EXCERPT_CHARS)[0],
                "scope": scope.to_dict(),
                "confidence": row["confidence"],
                "evidence_ids": _evidence_ids_for_delivery(
                    conn,
                    json.loads(row["evidence_ids"]),
                    context=context,
                    delivery_target=delivery_target,
                ),
            }
        )
        if len(current) >= limit:
            break
    return {
        "schema_version": DIGEST_SCHEMA_VERSION,
        "core_schema": CORE_V1_SCHEMA_VERSION,
        "delivery_target": delivery_target,
        "resolved_context": context.to_dict(),
        "counts": counts,
        "current": current,
        "recent_closeouts": _recent_closeouts_v1(
            conn,
            context=context,
            limit=min(limit, 5),
            since=since,
            delivery_target=delivery_target,
        ),
        "excluded_scope_count": excluded,
    }


def _recent_closeouts_v1(
    conn: sqlite3.Connection,
    *,
    context: ScopeContext,
    limit: int,
    since: str | None,
    delivery_target: str,
) -> list[dict[str, Any]]:
    """Return a tiny recent-work register without promoting receipts to beliefs.

    Closeouts are execution receipts, not current truth.  They are still the
    best source for "what just happened?" before a curator has promoted a
    durable fact.  Keep this lane deliberately narrow: high-signal receipts
    only, newest receipt per task, at most five.
    """
    if delivery_target != LOCAL_MODEL_TARGET:
        return []
    earliest = _digest_since(since)
    rows = conn.execute(
        """
        SELECT receipt_json
        FROM task_closeouts
        WHERE closed_at >= ?
        ORDER BY closed_at DESC, id DESC
        LIMIT 250
        """,
        (earliest.isoformat(timespec="microseconds"),),
    )
    result: list[dict[str, Any]] = []
    seen_tasks: set[str] = set()
    for row in rows:
        receipt = json.loads(row["receipt_json"])
        task_ref = str(receipt.get("task_ref") or "").strip()
        if not task_ref or task_ref in seen_tasks:
            continue
        if not _closeout_matches_context(receipt, context):
            continue
        if not _high_signal_closeout(receipt):
            continue
        seen_tasks.add(task_ref)
        artifacts = []
        for artifact in receipt.get("artifact_refs") or []:
            if not isinstance(artifact, dict) or not artifact.get("uri"):
                continue
            artifacts.append(
                {
                    key: artifact[key]
                    for key in ("uri", "kind", "label", "sha256")
                    if artifact.get(key)
                }
            )
            if len(artifacts) >= 4:
                break
        decision = receipt.get("decision") if isinstance(receipt.get("decision"), dict) else {}
        result.append(
            {
                "id": receipt.get("id"),
                "task_ref": task_ref,
                "status": receipt.get("status"),
                "summary": _human_excerpt(receipt.get("summary"), 600),
                "closed_at": receipt.get("closed_at"),
                "verification_status": receipt.get("verification_status"),
                "decision_impact": decision.get("impact"),
                "decision_note": _human_excerpt(decision.get("note"), 300),
                "artifacts": artifacts,
                "context": {
                    key: value
                    for key, value in (receipt.get("context") or {}).items()
                    if key in {"project", "repo", "client", "task", "runtime"} and value
                },
            }
        )
        if len(result) >= limit:
            break
    return result


def _digest_since(raw: str | None) -> datetime:
    if raw:
        normalized = raw.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("since must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return datetime.now(UTC) - timedelta(hours=72)


def _closeout_matches_context(receipt: dict[str, Any], context: ScopeContext) -> bool:
    receipt_context = receipt.get("context")
    if not isinstance(receipt_context, dict):
        receipt_context = {}
    scoped = False
    for key in ("project", "repo", "client"):
        expected = getattr(context, key)
        if not expected:
            continue
        scoped = True
        if str(receipt_context.get(key) or "") != expected:
            return False
    if not scoped and context.task:
        return (
            str(receipt.get("task_ref") or "") == context.task
            or str(receipt_context.get("task") or "") == context.task
        )
    return True


def _high_signal_closeout(receipt: dict[str, Any]) -> bool:
    status = str(receipt.get("status") or "")
    if status not in {"completed", "partial", "blocked"}:
        return False
    artifacts = receipt.get("artifact_refs")
    has_artifacts = isinstance(artifacts, list) and any(
        isinstance(item, dict) and item.get("uri") for item in artifacts
    )
    decision = receipt.get("decision")
    impact = str(decision.get("impact") or "") if isinstance(decision, dict) else ""
    verification = str(receipt.get("verification_status") or "")
    return (
        verification == "verified"
        or has_artifacts
        or impact in {"changed", "prevented_error"}
        or (status == "blocked" and bool(receipt.get("awaiting")))
    )


def _human_excerpt(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def feedback_v1(
    conn: sqlite3.Connection,
    retrieval_use_id: str,
    *,
    outcome: str,
    note: str | None,
) -> dict[str, Any]:
    allowed = {"helpful", "used", "irrelevant", "ignored", "harmful"}
    if outcome not in allowed:
        raise ValueError(f"outcome must be one of: {', '.join(sorted(allowed))}")
    updated = conn.execute(
        "UPDATE retrieval_uses SET outcome=?, note=COALESCE(?, note), "
        "feedback_source='runtime_explicit', feedback_at=? WHERE id=?",
        (outcome, note, now_iso(), retrieval_use_id),
    )
    if updated.rowcount == 0:
        raise ValueError(f"retrieval use not found: {retrieval_use_id}")
    return {"retrieval_use_id": retrieval_use_id, "outcome": outcome}


def ingest_v1(
    conn: sqlite3.Connection,
    *,
    body: str,
    kind: str,
    context: ScopeContext,
    writer: str,
    session_id: str | None,
    artifact_ref: str | None,
) -> dict[str, Any]:
    telemetry = kind in SKILL_TELEMETRY_KINDS
    if telemetry:
        envelope = validate_skill_telemetry(body)
        if envelope["kind"] != kind:
            raise ValueError("skill telemetry body kind must match brain.ingest kind")
        body = canonical_json(envelope)
    auto = automatic_activation_enabled(conn) and not telemetry
    # When auto-compiling, the evidence and its belief share one scope so
    # brain.source expansion stays scope-consistent, and that scope is the
    # shared continuity scope rather than the narrowest per-client one.
    scope = auto_compile_scope(context) if auto else resolve_write_scope(context)
    evidence_id, event_id = record_core_v1_evidence(
        conn,
        body=body,
        kind=kind,
        scope=scope,
        writer=writer,
        session_id=session_id,
        artifact_ref=artifact_ref,
    )
    result = {
        "event_id": event_id,
        "evidence_id": evidence_id,
        "kind": "evidence_recorded",
    }
    if auto:
        try:
            result["auto_compiled_belief_id"] = auto_compile_evidence(
                conn,
                evidence_id=evidence_id,
                body=body,
                scope=scope,
                actor=writer,
                source_kind=kind,
            )
        except PermissionError as exc:
            # Auto-belief ids are content-addressed, so re-ingesting text that
            # matches a retracted or tombstoned belief hits compilation_block_reason
            # and raises. Recording the evidence is the caller's actual request;
            # a blocked recompile must not fail the whole write.
            result["auto_compile_blocked"] = str(exc)
        else:
            result["kind"] = "evidence_recorded_and_compiled"
    return result


def closeout_v1(
    conn: sqlite3.Connection,
    *,
    task_ref: str,
    status: str,
    summary: str,
    context: ScopeContext,
    retrieval_use_ids: list[str],
    decision_impact: str,
    decision_note: str | None,
    artifact_refs: list[dict[str, Any]],
    verifier_refs: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    awaiting: str | None,
    actor: str,
    provenance: Provenance | None = None,
) -> dict[str, Any]:
    # Report the writing standard back to whoever wrote the summary. Reporting
    # rather than refusing is the default: the curator gate already stops slop
    # from becoming a served belief, and refusing a closeout throws away the
    # client's work over a style rule. `deslop.reject_closeout_slop` hardens it
    # for an operator who has calibrated the rules against their own corpus --
    # and it is checked here, before anything is written, because a refusal after
    # the receipt exists would leave the closeout recorded and the caller told it
    # failed.
    slop = find_slop(summary, {"lifecycle": "durable"}, rules=ENFORCED_RULE_IDS)
    if slop and load_config().deslop.reject_closeout_slop:
        raise ValueError(
            "closeout summary trips "
            + ", ".join(finding.rule for finding in slop)
            + "; state one durable fact per closeout, or set "
            "deslop.reject_closeout_slop=false to report instead of refuse"
        )
    receipt = record_closeout(
        conn,
        task_ref=task_ref,
        status=status,
        summary=summary,
        context=context,
        retrieval_use_ids=retrieval_use_ids,
        decision_impact=decision_impact,
        decision_note=decision_note,
        artifact_refs=artifact_refs,
        verifier_refs=verifier_refs,
        actions=actions,
        outcomes=outcomes,
        awaiting=awaiting,
        actor=actor,
        provenance=provenance,
    )
    # Always record the summary as scoped evidence, under the shared continuity
    # scope so a closeout written while one client worked is curatable and
    # recallable by the others.
    #
    # This used to sit inside the automatic_activation check, which conflated two
    # different things: *recording evidence* and *promoting it to a served
    # belief*. Turning the flag off to stop unattended promotion therefore also
    # stopped closeout summaries becoming evidence at all -- and closeout
    # summaries are the single largest supply of curator-eligible evidence. One
    # real brain lost 567 of 799 closeouts (71%) that way, silently, for weeks.
    # Recording evidence is not promotion; only the compile step is gated.
    scope = auto_compile_scope(context)
    evidence_id, _event_id = record_core_v1_evidence(
        conn,
        body=summary,
        kind="task_closeout_summary",
        scope=scope,
        writer=actor,
        artifact_ref=f"closeout:{receipt['id']}",
    )
    receipt["evidence_id"] = evidence_id
    if slop:
        receipt["slop_findings"] = [finding.to_dict() for finding in slop]
    if automatic_activation_enabled(conn):
        try:
            receipt["auto_compiled_belief_id"] = auto_compile_evidence(
                conn,
                evidence_id=evidence_id,
                body=summary,
                scope=scope,
                actor=actor,
                source_kind="task_closeout_summary",
            )
        except PermissionError as exc:
            # See ingest_v1: a blocked recompile must not lose the closeout.
            receipt["auto_compile_blocked"] = str(exc)
    return receipt


def correct_v1(
    conn: sqlite3.Connection,
    *,
    layer: str,
    target: str,
    op: str,
    body: str | None,
    actor: str,
    hard: bool,
) -> dict[str, Any]:
    if layer not in {"knowledge", "belief"}:
        raise ValueError("layer must be knowledge or belief; evidence corrections are unsupported")
    event_id = append_core_event(
        conn,
        "correction_recorded",
        {
            "schema_version": "ocbrain.correction.v1",
            "subject": {"kind": layer, "id": resolve_object_id(conn, target)},
            "target_layer": layer,
            "target_id": target,
            "op": op,
            "body": body,
            "author": actor,
            "hard": bool(hard),
        },
        writer=actor,
        project=True,
    )
    return {"event_id": event_id, "kind": "correction_recorded"}


def forget_v1(
    conn: sqlite3.Connection,
    *,
    target: str,
    mode: str,
    reason: str | None,
    actor: str,
) -> dict[str, Any]:
    if mode not in {"soft", "shred"}:
        raise ValueError("mode must be soft or shred")
    event_id = append_core_event(
        conn,
        "tombstone_recorded",
        {
            "schema_version": "ocbrain.tombstone.v1",
            "subject": {"kind": "belief", "id": resolve_object_id(conn, target)},
            "target": target,
            "target_hash": sha256_text(target),
            "mode": mode,
            "reason": reason,
            "approved_by": actor,
        },
        writer=actor,
        project=True,
    )
    return {"event_id": event_id, "kind": "tombstone_recorded"}


def proposals_v1(
    conn: sqlite3.Connection,
    *,
    limit: int,
    include_decided: bool,
) -> dict[str, Any]:
    decided = {
        str(json.loads(row["body_json"]).get("proposal_event_id"))
        for row in conn.execute(
            "SELECT body_json FROM brain_events WHERE kind='compilation_decided'"
        )
    }
    result: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM brain_events WHERE kind='compilation_proposed' ORDER BY rowid DESC LIMIT ?",
        (max(limit * 4, 100),),
    ):
        is_decided = str(row["id"]) in decided
        if is_decided and not include_decided:
            continue
        result.append(
            {
                "proposal_event_id": str(row["id"]),
                "ts": str(row["ts"]),
                "decided": is_decided,
                **json.loads(row["body_json"]),
            }
        )
        if len(result) >= limit:
            break
    return {"schema_version": "ocbrain.proposals.v1", "proposals": result}


def decide_proposal_v1(
    conn: sqlite3.Connection,
    *,
    proposal_event_id: str,
    decision: str,
    actor: str,
    edited_body: str | None,
    reason: str | None,
) -> dict[str, Any]:
    if decision not in {"approve", "reject", "edit", "shadow"}:
        raise ValueError("decision must be approve, reject, edit, or shadow")
    proposal = conn.execute(
        "SELECT event_seq, body_json FROM brain_events WHERE id=? AND kind='compilation_proposed'",
        (proposal_event_id,),
    ).fetchone()
    if proposal is None:
        raise ValueError(f"proposal not found: {proposal_event_id}")
    existing = conn.execute(
        "SELECT 1 FROM brain_events WHERE kind='compilation_decided' "
        "AND json_extract(body_json, '$.proposal_event_id')=?",
        (proposal_event_id,),
    ).fetchone()
    if existing is not None:
        raise ValueError(f"proposal already decided: {proposal_event_id}")
    if decision in {"approve", "edit"}:
        proposal_body = json.loads(proposal["body_json"])
        belief_id = str(proposal_body.get("belief_id") or "")
        reason_blocked = compilation_block_reason(
            conn,
            belief_id,
            proposal_event_seq=int(proposal["event_seq"]),
        )
        if reason_blocked is not None:
            raise PermissionError(f"cannot {decision}: belief is {reason_blocked}: {belief_id}")
    event_id = append_core_event(
        conn,
        "compilation_decided",
        {
            "schema_version": "ocbrain.compilation-decision.v1",
            "subject": {"kind": "proposal", "id": proposal_event_id},
            "proposal_event_id": proposal_event_id,
            "decision": decision,
            "actor": actor,
            "edited_body": edited_body,
            "reason": reason,
        },
        writer=actor,
        project=True,
    )
    return {"event_id": event_id, "kind": "compilation_decided", "decision": decision}


def _scope_allowed_for_delivery(
    raw_scope: dict[str, Any] | None,
    *,
    context: ScopeContext,
    delivery_target: str,
    cross_scope: bool = False,
) -> bool:
    allowed, _reason = egress_allowed(
        ScopeTag.from_dict(raw_scope),
        context,
        delivery_target,
        cross_scope=cross_scope,
    )
    return allowed


def _evidence_ids_for_delivery(
    conn: sqlite3.Connection,
    evidence_ids: list[Any],
    *,
    context: ScopeContext,
    delivery_target: str,
    cross_scope: bool = False,
) -> list[str]:
    values = [str(value) for value in evidence_ids]
    if delivery_target == LOCAL_MODEL_TARGET:
        return values
    result: list[str] = []
    for evidence_id in values:
        evidence = get_core_v1_evidence(conn, evidence_id)
        if evidence is None:
            continue
        if _scope_allowed_for_delivery(
            evidence.get("scope"),
            context=context,
            delivery_target=delivery_target,
            cross_scope=cross_scope,
        ):
            result.append(evidence_id)
    return result


def _authorize_delivery_scope(
    scope: ScopeTag,
    *,
    context: ScopeContext,
    delivery_target: str,
    scope_error: str,
    cross_scope: bool = False,
) -> None:
    allowed, reason = egress_allowed(
        scope,
        context,
        delivery_target,
        cross_scope=cross_scope,
    )
    if allowed:
        return
    if reason == "scope_mismatch":
        raise PermissionError(scope_error)
    raise PermissionError(f"object is not eligible for {delivery_target} delivery ({reason})")


def _source_handles_for_belief(
    conn: sqlite3.Connection,
    belief_id: str,
    *,
    context: ScopeContext,
    delivery_target: str,
    cross_scope: bool = False,
) -> list[dict[str, Any]]:
    canonical_id = resolve_object_id(conn, belief_id)
    handles: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT eo.* FROM belief_evidence be "
        "JOIN evidence_objects eo ON eo.evidence_id=be.evidence_id "
        "WHERE be.belief_id=? ORDER BY be.created_at, eo.evidence_id",
        (canonical_id,),
    )
    for row in rows:
        scope = {
            "scope_type": row["scope_type"],
            "scope_id": row["scope_id"],
            "visibility": row["visibility"],
            "egress_policy": row["egress_policy"],
            "provenance": row["scope_provenance"],
        }
        allowed, _reason = egress_allowed(
            ScopeTag.from_dict(scope),
            context,
            delivery_target,
            cross_scope=cross_scope,
        )
        if not allowed:
            continue
        content = str(row["body"])
        handles.append(
            _make_source_handle(
                object_id=canonical_id,
                source_kind="core_v1_evidence",
                uri=(
                    f"ocbrain://evidence/{row['evidence_id']}"
                    if delivery_target == HOSTED_MODEL_TARGET
                    else row["source_uri"]
                    or row["artifact_uri"]
                    or f"ocbrain://evidence/{row['evidence_id']}"
                ),
                content_hash=sha256_text(content),
                scope=scope,
                locator={"evidence_id": str(row["evidence_id"])},
            )
        )
    if handles:
        return _dedupe_handles(handles)
    belief = get_core_v1_belief(conn, canonical_id)
    if belief is None:
        return []
    scope = dict(belief["scope"])
    allowed, _reason = egress_allowed(
        ScopeTag.from_dict(scope),
        context,
        delivery_target,
        cross_scope=cross_scope,
    )
    if not allowed:
        return []
    return [
        _make_source_handle(
            object_id=canonical_id,
            source_kind="core_v1_belief",
            uri=f"ocbrain://belief/{canonical_id}",
            content_hash=sha256_text(str(belief["body"])),
            scope=scope,
            locator={"belief_id": canonical_id},
        )
    ]


def _make_source_handle(
    *,
    object_id: str,
    source_kind: str,
    uri: str | None,
    content_hash: str,
    scope: dict[str, Any],
    locator: dict[str, Any],
) -> dict[str, Any]:
    source_id = stable_id(
        "src",
        object_id,
        source_kind,
        uri or "",
        content_hash,
        canonical_json(scope),
    )
    return {
        "id": source_id,
        "object_id": object_id,
        "source_kind": source_kind,
        "uri": uri,
        "content_hash": content_hash,
        "scope": scope,
        "locator": locator,
    }


def _public_source_handle(handle: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": handle["id"],
        "kind": handle["source_kind"],
        "uri": handle.get("uri"),
        "content_hash": handle["content_hash"],
    }


def _dedupe_handles(handles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({str(handle["id"]): handle for handle in handles}.values())


def _bounded_excerpt(content: str, *, max_chars: int) -> tuple[str, bool]:
    if len(content) <= max_chars:
        return content, False
    return content[:max_chars], True


def _explicit_contradictions(
    conn: sqlite3.Connection, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Package only curator/compiler-declared conflicts, never lexical guesses."""
    visible = {str(item["id"]): item for item in items}
    result: list[dict[str, Any]] = []
    emitted: set[tuple[str, str]] = set()
    for belief_id, item in visible.items():
        belief = get_core_v1_belief(conn, belief_id)
        attributes = (belief or {}).get("attributes") or {}
        conflicts = attributes.get("contradicts") or attributes.get("contradiction_ids") or []
        if not isinstance(conflicts, list):
            continue
        for raw_other_id in conflicts:
            other_id = resolve_object_id(conn, str(raw_other_id))
            if other_id not in visible or other_id == belief_id:
                continue
            pair = tuple(sorted((belief_id, other_id)))
            if pair in emitted:
                continue
            emitted.add(pair)
            other = visible[other_id]
            result.append(
                {
                    "belief_id": belief_id,
                    "other_belief_id": other_id,
                    "reason": "explicit_compiler_metadata",
                    "evidence_ids": list(
                        dict.fromkeys(
                            [
                                *[str(value) for value in item.get("evidence_ids") or []],
                                *[str(value) for value in other.get("evidence_ids") or []],
                            ]
                        )
                    )[:8],
                }
            )
    return result[:12]


def prepare_retrieval_packet_v1(
    packet: dict[str, Any],
    handles: list[dict[str, Any]],
    *,
    preview: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Reserve the final receipt fields before enforcing the public byte cap."""
    packet["retrieval_use_id"] = RETRIEVAL_ID_PLACEHOLDER
    packet["retrieval_use_status"] = "recorded"
    if preview:
        packet["preview"] = True
    return _enforce_context_packet_limit(packet, handles)


def bind_retrieval_id_v1(packet: dict[str, Any], retrieval_id: str) -> None:
    if len(retrieval_id) != len(RETRIEVAL_ID_PLACEHOLDER):
        raise RuntimeError("retrieval id length changed after packet budgeting")
    packet["retrieval_use_id"] = retrieval_id
    _refresh_packet_accounting(packet)
    if _serialized_bytes(packet) > MAX_CONTEXT_PACKET_BYTES:
        raise RuntimeError("final retrieval packet exceeded the hard serialized limit")


def _enforce_context_packet_limit(
    packet: dict[str, Any], handles: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    coverage = packet["coverage"]
    previously_trimmed = int(coverage.get("trimmed_for_packet_limit") or 0)
    trimmed = 0
    # Leave headroom for the final accounting fields themselves.
    while packet["items"] and _serialized_bytes(packet) > MAX_CONTEXT_PACKET_BYTES - 512:
        packet["items"].pop()
        trimmed += 1
    kept_ids = {str(item["id"]) for item in packet["items"]}
    packet["contradictions"] = [
        conflict
        for conflict in packet["contradictions"]
        if conflict["belief_id"] in kept_ids and conflict["other_belief_id"] in kept_ids
    ]
    kept_source_ids = {
        str(source["id"]) for item in packet["items"] for source in item.get("sources") or []
    }
    handles = [handle for handle in handles if str(handle["id"]) in kept_source_ids]
    coverage["returned"] = len(packet["items"])
    coverage["feedback_needed"] = len(packet["items"]) > 0
    coverage["trimmed_for_packet_limit"] = previously_trimmed + trimmed
    coverage["source_handle_count"] = len(handles)
    coverage["unavailable_sources"] = [
        value for value in coverage["unavailable_sources"] if value["object_id"] in kept_ids
    ]
    _refresh_packet_accounting(packet)
    if coverage["serialized_bytes"] > MAX_CONTEXT_PACKET_BYTES:
        raise RuntimeError("context packet accounting exceeded the hard serialized limit")
    return packet, handles


def _refresh_packet_accounting(packet: dict[str, Any]) -> None:
    coverage = packet["coverage"]
    for _attempt in range(8):
        serialized_bytes = _serialized_bytes(packet)
        estimated_tokens = max((serialized_bytes + 3) // 4, 1)
        if (
            coverage.get("serialized_bytes") == serialized_bytes
            and coverage.get("estimated_tokens") == estimated_tokens
        ):
            return
        coverage["serialized_bytes"] = serialized_bytes
        coverage["estimated_tokens"] = estimated_tokens
    raise RuntimeError("packet accounting did not converge")


def _serialized_bytes(value: dict[str, Any]) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _require_v1(conn: sqlite3.Connection) -> None:
    if not is_core_v1(conn):
        raise ValueError("operation requires an OCBrain v1 core")


__all__ = [
    "auto_compile_evidence",
    "auto_compile_scope",
    "bind_retrieval_id_v1",
    "build_context_v1",
    "closeout_v1",
    "correct_v1",
    "decide_proposal_v1",
    "digest_v1",
    "exact_lookup_v1",
    "expand_source_v1",
    "feedback_v1",
    "forget_v1",
    "get_v1",
    "ingest_v1",
    "prepare_retrieval_packet_v1",
    "proposals_v1",
    "record_context_v1",
    "search_v1",
]
