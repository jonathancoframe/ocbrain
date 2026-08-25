from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ocbrain.db import now_iso
from ocbrain.events import canonical_json
from ocbrain.ids import content_hash, stable_id
from ocbrain.provenance import EMPTY_PROVENANCE, Provenance
from ocbrain.scope import ScopeContext

CLOSEOUT_SCHEMA_VERSION = "ocbrain.closeout.v1"
ACTION_SCHEMA_VERSION = "ocbrain.action.v1"
OUTCOME_SCHEMA_VERSION = "ocbrain.outcome.v1"
CLOSEOUT_STATUSES = {"completed", "partial", "blocked", "failed", "cancelled"}
DECISION_IMPACTS = {"none", "informed", "changed", "prevented_error", "unknown"}
VERIFIER_STATUSES = {"passed", "failed", "unknown", "not_required"}

# Wrapper syntax clients paste in front of an otherwise-fine reference, so that
# `ocbrain:COFASC-292` and `COFASC-292` land in the same chain. Matched
# case-insensitively because the wrapper is punctuation, not identity.
TASK_REF_WRAPPER_PREFIXES: tuple[str, ...] = ("ocbrain:", "task:")
# Long enough for every task_ref in a real 1,148-row corpus (longest: 164
# chars), short enough that a pasted document cannot become an index key.
MAX_TASK_REF_NORM = 256
_TASK_REF_WHITESPACE = re.compile(r"\s+")


def normalize_task_ref(task_ref: Any) -> str:
    """Fold a free-text ``task_ref`` into the key two closeouts chain on.

    Trims, collapses internal whitespace, strips the wrapper prefixes above, and
    bounds the length. **Case is preserved.** This column carries Linear ids
    (`COFASC-292`) and raw UUIDs, and ``scope.py`` gives the reasoning for the
    same decision about task and session ids: they are machine-minted,
    high-cardinality, and often case-significant, so folding them risks
    collapsing two distinct references into one. Only the spellings a human
    varies by accident are folded.

    Idempotent: ``normalize_task_ref(normalize_task_ref(x)) == normalize_task_ref(x)``.
    A value that is nothing but wrapper prefixes folds back to its trimmed self
    rather than to the empty string, so an odd input cannot chain itself onto
    every other odd input.

    The raw ``task_ref`` column keeps the verbatim value forever; this is a
    derived key stored beside it, never a replacement.
    """
    collapsed = _TASK_REF_WHITESPACE.sub(" ", str(task_ref or "")).strip()
    stripped = collapsed
    peeled = True
    while peeled:
        peeled = False
        for prefix in TASK_REF_WRAPPER_PREFIXES:
            if stripped[: len(prefix)].lower() == prefix:
                stripped = stripped[len(prefix) :].strip()
                peeled = True
    return (stripped or collapsed)[:MAX_TASK_REF_NORM]


def record_closeout(
    conn: sqlite3.Connection,
    *,
    task_ref: str,
    status: str,
    summary: str,
    context: ScopeContext | None = None,
    retrieval_use_ids: list[str] | None = None,
    decision_impact: str = "unknown",
    decision_note: str | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
    verifier_refs: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    outcomes: list[dict[str, Any]] | None = None,
    awaiting: str | None = None,
    actor: str = "agent",
    parent_closeout_id: str | None = None,
    provenance: Provenance | None = None,
) -> dict[str, Any]:
    """Append a generic execution outcome receipt without promoting knowledge.

    ``provenance`` is what the server observed about the connection that sent
    the closeout; ``context.session`` and ``context.runtime`` remain what the
    model said. Both are recorded, separately, because only one of them can be
    trusted and the receipt should say which. The observed fields join into the
    hashed provenance block, so two byte-identical closeouts written from two
    different connections are two distinct receipts rather than a UNIQUE
    collision on ``content_hash``.

    ``parent_closeout_id`` names the closeout this one continues. It is
    validated against ``task_closeouts.id``, and an unresolved value is recorded
    in the receipt as a claim with ``chain.parent_unresolved`` set rather than
    refused: a closeout must never fail over a bad parent, for the same reason
    an unknown ``retrieval_use_id`` no longer voids the whole receipt. Only a
    resolved parent reaches the column, so the pointer is never dangling.
    """
    task_ref = _required_text(task_ref, "task_ref")
    summary = _required_text(summary, "summary")
    actor = _required_text(actor, "actor")
    if status not in CLOSEOUT_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(CLOSEOUT_STATUSES))}")
    if decision_impact not in DECISION_IMPACTS:
        raise ValueError(
            f"decision_impact must be one of: {', '.join(sorted(DECISION_IMPACTS))}"
        )
    if status == "blocked" and not (awaiting and awaiting.strip()):
        raise ValueError("blocked closeouts require awaiting")
    retrieval_ids, unmatched_retrieval_ids = _partition_retrieval_ids(
        conn, _dedupe_text(retrieval_use_ids or [])
    )
    artifacts = [_normalize_artifact_ref(value) for value in artifact_refs or []]
    verifiers = [_normalize_verifier_ref(value) for value in verifier_refs or []]
    normalized_actions = [_normalize_action(value) for value in actions or []]
    normalized_outcomes = [_normalize_outcome(value) for value in outcomes or []]
    verification_status = _verification_status(verifiers)
    resolved = context or ScopeContext()
    closed_at = now_iso()
    task_ref_norm = normalize_task_ref(task_ref)
    parent_claim = parent_closeout_id.strip() if isinstance(parent_closeout_id, str) else None
    resolved_parent = _resolve_parent(conn, parent_claim)
    chain: dict[str, Any] = {
        "parent_closeout_id": parent_claim or None,
        # One indexed read on (task_ref_norm, closed_at). Historical rows carry
        # a NULL norm and are deliberately not rewritten, so a chain begins at
        # the first closeout written by a server that has this column.
        "previous_in_chain": _previous_in_chain(conn, task_ref_norm),
    }
    if parent_claim and resolved_parent is None:
        chain["parent_unresolved"] = True
    observed = provenance or EMPTY_PROVENANCE
    provenance_block: dict[str, Any] = {
        "source": "agent_reported",
        "actor": actor,
        "runtime": resolved.runtime or "mcp",
        "session_id": resolved.session,
        "reported_at": closed_at,
        # Named so nobody has to read this file to know which half is a claim.
        "server_observed": observed.to_dict(),
    }
    base_receipt: dict[str, Any] = {
        "schema_version": CLOSEOUT_SCHEMA_VERSION,
        "closed_at": closed_at,
        "task_ref": task_ref,
        "status": status,
        "summary": summary,
        "decision": {
            "impact": decision_impact,
            "note": decision_note.strip() if decision_note and decision_note.strip() else None,
        },
        "retrieval_use_ids": retrieval_ids,
        "unmatched_retrieval_use_ids": unmatched_retrieval_ids,
        "artifact_refs": artifacts,
        "verifier_refs": verifiers,
        "actions": normalized_actions,
        "outcomes": normalized_outcomes,
        "verification_status": verification_status,
        "awaiting": awaiting.strip() if awaiting and awaiting.strip() else None,
        "task_ref_norm": task_ref_norm,
        "chain": chain,
        "context": resolved.to_dict(),
        "provenance": provenance_block,
    }
    digest = content_hash(canonical_json(base_receipt))
    closeout_id = stable_id("close", task_ref, closed_at, digest)
    receipt = {"id": closeout_id, "content_hash": digest, **base_receipt}
    conn.execute(
        """
        INSERT INTO task_closeouts (
          id, schema_version, closed_at, task_ref, status, summary,
          decision_impact, decision_note, awaiting, runtime, session_id,
          context_json, artifact_refs_json, verifier_refs_json, provenance_json,
          receipt_json, content_hash,
          server_connection_id, client_session_hint, client_runtime_key,
          parent_closeout_id, task_ref_norm
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            closeout_id,
            CLOSEOUT_SCHEMA_VERSION,
            closed_at,
            task_ref,
            status,
            summary,
            decision_impact,
            base_receipt["decision"]["note"],
            base_receipt["awaiting"],
            provenance_block["runtime"],
            provenance_block["session_id"],
            canonical_json(base_receipt["context"]),
            canonical_json(artifacts),
            canonical_json(verifiers),
            canonical_json(provenance_block),
            canonical_json(receipt),
            digest,
            observed.server_connection_id,
            observed.client_session_hint,
            observed.client_runtime_key,
            resolved_parent,
            task_ref_norm,
        ),
    )
    for retrieval_use_id in retrieval_ids:
        conn.execute(
            "INSERT INTO task_closeout_retrievals (closeout_id, retrieval_use_id) "
            "VALUES (?, ?)",
            (closeout_id, retrieval_use_id),
        )
        conn.execute(
            "UPDATE retrieval_uses SET affected_decision = ? WHERE id = ?",
            (_affected_decision(decision_impact), retrieval_use_id),
        )
    return receipt


def get_closeout(conn: sqlite3.Connection, closeout_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT receipt_json FROM task_closeouts WHERE id = ?",
        (closeout_id,),
    ).fetchone()
    return json.loads(row["receipt_json"]) if row is not None else None


def _resolve_parent(conn: sqlite3.Connection, parent_closeout_id: str | None) -> str | None:
    """Return the parent id only if a closeout by that id exists.

    An unresolved claim is kept in the receipt and out of the column, mirroring
    how ``_partition_retrieval_ids`` treats an unknown retrieval id: the claim
    is evidence, a dangling pointer is not.
    """
    if not parent_closeout_id:
        return None
    row = conn.execute(
        "SELECT id FROM task_closeouts WHERE id=?", (parent_closeout_id,)
    ).fetchone()
    return str(row["id"]) if row is not None else None


def _previous_in_chain(conn: sqlite3.Connection, task_ref_norm: str) -> str | None:
    """The most recent closeout already filed against the same normalized ref.

    This is what gives an agent chain continuity without having to remember an
    id across sessions: it never has to pass ``parent_closeout_id`` to find out
    what the last run on this task concluded. Ordered by ``closed_at`` with the
    id as a tiebreaker, so two closeouts written inside the same clock tick
    still resolve deterministically.
    """
    if not task_ref_norm:
        return None
    row = conn.execute(
        "SELECT id FROM task_closeouts WHERE task_ref_norm=? "
        "ORDER BY closed_at DESC, id DESC LIMIT 1",
        (task_ref_norm,),
    ).fetchone()
    return str(row["id"]) if row is not None else None


def _partition_retrieval_ids(
    conn: sqlite3.Connection, retrieval_ids: list[str]
) -> tuple[list[str], list[str]]:
    """Split linked retrieval ids into (known, unknown), refusing neither.

    This used to raise on any unknown id, which voided the entire receipt: an
    agent holding one mangled id — a live fleet retried `ocbret_…` three times
    in one evening — cannot repair its own context, so the retry fails
    identically and the closeout is simply lost. A receipt with one unlinked id
    recorded as unmatched is strictly more evidence than no receipt. Unknown ids
    are carried in the receipt verbatim, and never inserted into
    ``task_closeout_retrievals``, so no join is ever fabricated.
    """
    if not retrieval_ids:
        return [], []
    placeholders = ",".join("?" for _ in retrieval_ids)
    found = {
        str(row["id"])
        for row in conn.execute(
            f"SELECT id FROM retrieval_uses WHERE id IN ({placeholders})",  # noqa: S608
            retrieval_ids,
        )
    }
    known = [value for value in retrieval_ids if value in found]
    unknown = [value for value in retrieval_ids if value not in found]
    return known, unknown


def _normalize_artifact_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("artifact_refs entries must be objects")
    uri = _required_text(value.get("uri"), "artifact_refs[].uri")
    result: dict[str, Any] = {"uri": uri}
    for key in ("kind", "sha256", "label"):
        item = value.get(key)
        if item is not None:
            result[key] = _required_text(item, f"artifact_refs[].{key}")
    return result


def _normalize_verifier_ref(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("verifier_refs entries must be objects")
    uri = _required_text(value.get("uri"), "verifier_refs[].uri")
    status = str(value.get("status") or "unknown")
    if status not in VERIFIER_STATUSES:
        raise ValueError(f"verifier status must be one of: {', '.join(sorted(VERIFIER_STATUSES))}")
    result: dict[str, Any] = {"uri": uri, "status": status}
    for key in ("kind", "sha256", "detail"):
        item = value.get(key)
        if item is not None:
            result[key] = _required_text(item, f"verifier_refs[].{key}")
    return result


def _normalize_action(value: Any) -> dict[str, Any]:
    """Preserve a portable action envelope without pretending it is a reward."""
    if not isinstance(value, dict):
        raise ValueError("actions entries must be objects")
    target = _json_object(value.get("target"), "actions[].target", required=True)
    result: dict[str, Any] = {
        "schema_version": ACTION_SCHEMA_VERSION,
        "mechanism": _required_text(value.get("mechanism"), "actions[].mechanism"),
        "semantic_role": _required_text(
            value.get("semantic_role"), "actions[].semantic_role"
        ),
        "target": target,
    }
    for key in ("action_id", "occurred_at"):
        item = value.get(key)
        if item is not None:
            result[key] = _required_text(item, f"actions[].{key}")
    for key in ("context_before", "policy", "cost", "provenance"):
        item = value.get(key)
        if item is not None:
            result[key] = _json_object(item, f"actions[].{key}", required=False)
    features = value.get("features")
    if features is not None:
        normalized_features = _json_object(
            features, "actions[].features", required=False
        )
        if normalized_features:
            result["features"] = normalized_features
    if "features" in result:
        result["feature_schema"] = _required_text(
            value.get("feature_schema"), "actions[].feature_schema"
        )
    elif value.get("feature_schema") is not None:
        raise ValueError("actions[].feature_schema requires non-empty actions[].features")
    return result


def _normalize_outcome(value: Any) -> dict[str, Any]:
    """Keep outcome components and local meaning instead of one scalar reward."""
    if not isinstance(value, dict):
        raise ValueError("outcomes entries must be objects")
    if "value" not in value:
        raise ValueError("outcomes[].value is required")
    result: dict[str, Any] = {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "metric": _required_text(value.get("metric"), "outcomes[].metric"),
        "value": _json_value(value["value"], "outcomes[].value"),
        "role": _required_text(value.get("role") or "primary", "outcomes[].role"),
        "interpretation": _required_text(
            value.get("interpretation"), "outcomes[].interpretation"
        ),
    }
    for key in ("unit", "observed_at"):
        item = value.get(key)
        if item is not None:
            result[key] = _required_text(item, f"outcomes[].{key}")
    for key in (
        "observation_window",
        "baseline",
        "counterfactual",
        "attribution",
        "uncertainty",
    ):
        item = value.get(key)
        if item is not None:
            result[key] = _json_value(item, f"outcomes[].{key}")
    features = value.get("features")
    if features is not None:
        normalized_features = _json_object(
            features, "outcomes[].features", required=False
        )
        if normalized_features:
            result["features"] = normalized_features
    if "features" in result:
        result["feature_schema"] = _required_text(
            value.get("feature_schema"), "outcomes[].feature_schema"
        )
    elif value.get("feature_schema") is not None:
        raise ValueError("outcomes[].feature_schema requires non-empty outcomes[].features")
    return result


def _json_object(value: Any, name: str, *, required: bool) -> dict[str, Any]:
    if not isinstance(value, dict) or (required and not value):
        suffix = "a non-empty object" if required else "an object"
        raise ValueError(f"{name} must be {suffix}")
    return _json_value(value, name)


def _json_value(value: Any, name: str) -> Any:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite JSON") from exc
    return json.loads(encoded)


def _verification_status(verifiers: list[dict[str, Any]]) -> str:
    if any(value["status"] == "failed" for value in verifiers):
        return "failed"
    if verifiers and all(value["status"] == "passed" for value in verifiers):
        return "verified"
    return "agent_reported"


def _affected_decision(decision_impact: str) -> int | None:
    if decision_impact in {"informed", "changed", "prevented_error"}:
        return 1
    if decision_impact == "none":
        return 0
    return None


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _dedupe_text(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _required_text(value, "retrieval_use_ids[]")
        if text not in result:
            result.append(text)
    return result
