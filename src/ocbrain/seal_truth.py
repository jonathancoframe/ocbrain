"""Compile one verified sealed release into sparse current OCBrain truth."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ocbrain.core_v1 import (
    append_core_event,
    get_core_v1_belief,
    is_core_v1,
    record_core_v1_evidence,
)
from ocbrain.hybrid import (
    DEFAULT_OLLAMA_URL,
    build_vector_index,
    connection_path,
)
from ocbrain.ids import stable_id
from ocbrain.mcp_v1 import correct_v1, decide_proposal_v1
from ocbrain.scope import ScopeTag
from ocbrain.wiki import materialize_wiki


def compile_sealed_release(
    conn,
    seal_path: Path,
    *,
    wiki_dir: Path,
    actor: str = "seal-truth-compiler",
) -> dict[str, Any]:
    if not is_core_v1(conn):
        raise ValueError("seal truth compilation requires an OCBrain v1 core")
    seal_path = seal_path.expanduser().resolve()
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    if seal.get("schema_version") != "agent-control.release.v1":
        raise ValueError("unsupported release seal schema")
    if seal.get("state") != "sealed":
        raise ValueError("release is not sealed")
    release_id = required_text(seal.get("release_id"), "release_id")
    task_id = required_text(seal.get("task_id"), "task_id")
    artifacts = verify_artifacts(seal_path.parent, seal.get("artifacts"))
    closeout_entry = next(
        (item for item in artifacts if item["role"] == "closeout"), None
    )
    if closeout_entry is None:
        return {
            "status": "ignored",
            "reason": "sealed release has no canonical closeout artifact",
            "release_id": release_id,
            "task_id": task_id,
        }
    closeout = json.loads(Path(closeout_entry["path"]).read_text(encoding="utf-8"))
    candidate = candidate_from_closeout(
        closeout,
        release_id=release_id,
        seal_created_at=seal.get("created_at"),
        fallback_task_id=task_id,
    )
    project = candidate["project"]
    belief_id = stable_id("belief", "sealed-task-truth", project, task_id)
    existing = get_core_v1_belief(conn, belief_id)
    existing_attributes = (existing or {}).get("attributes") or {}
    if (
        existing is not None
        and existing.get("status") == "current"
        and bool(existing.get("serve"))
        and existing_attributes.get("release_id") == release_id
    ):
        return {
            "status": "unchanged",
            "belief_id": belief_id,
            "release_id": release_id,
            "task_id": task_id,
        }
    if existing is not None and existing.get("status") in {"retracted", "tombstoned"}:
        raise PermissionError(f"belief is {existing['status']}: {belief_id}")

    body = candidate["body"]
    evidence_body = (
        f"{body} Canonical release {release_id} is sealed and hash-verified. "
        f"Verification status: verified."
    )
    if candidate["uncertainty"]:
        evidence_body += f" Caveat: {candidate['uncertainty']}"
    scope = ScopeTag(
        "project",
        f"project:{project}",
        visibility="internal",
        egress_policy="local_only",
        provenance="sealed_release",
    )
    evidence_id, _event_id = record_core_v1_evidence(
        conn,
        body=evidence_body,
        kind="sealed_mission_closeout",
        scope=scope,
        writer=actor,
        artifact_ref=str(seal_path),
    )
    prior_release = existing_attributes.get("release_id")
    attributes = {
        "key": f"{task_id}-current-truth",
        "title": candidate["title"],
        "category": "project",
        "lifecycle": "current",
        "compiler": "seal-truth-compiler-v1",
        "task_id": task_id,
        "release_id": release_id,
        "seal_path": str(seal_path),
        "last_verified_at": candidate["last_verified_at"],
        "verification_status": "verified",
        "uncertainty": candidate["uncertainty"],
        "supersedes_release": prior_release,
        "supersedes_beliefs": candidate["supersedes"],
        "source_quality": 1.0,
    }
    proposal_id = append_core_event(
        conn,
        "compilation_proposed",
        {
            "schema_version": "ocbrain.compilation.v1",
            "subject": {"kind": "belief", "id": belief_id},
            "belief_id": belief_id,
            "belief_type": "wiki_fact",
            "body": body,
            "evidence_ids": [evidence_id],
            "scope": scope.to_dict(),
            "confidence": candidate["confidence"],
            "reward_band": "strong",
            "attributes": attributes,
        },
        writer=actor,
    )
    decide_proposal_v1(
        conn,
        proposal_event_id=proposal_id,
        decision="approve",
        actor="deterministic:seal-truth-compiler",
        edited_body=None,
        reason=(
            "canonical closeout is completed, verifier-backed, and enclosed by a "
            "hash-verified immutable release"
        ),
    )
    superseded: list[str] = []
    for target in candidate["supersedes"]:
        prior = get_core_v1_belief(conn, target)
        if prior is None or prior.get("status") != "current" or not prior.get("serve"):
            continue
        prior_scope = prior.get("scope") or {}
        if prior_scope.get("scope_id") != f"project:{project}":
            raise PermissionError(f"superseded belief is outside project scope: {target}")
        correct_v1(
            conn,
            layer="belief",
            target=target,
            op="retract",
            body=None,
            actor="deterministic:seal-truth-compiler",
            hard=False,
        )
        superseded.append(target)
    conn.commit()
    digest = hashlib.sha256(
        json.dumps(
            {
                "release_id": release_id,
                "belief_id": belief_id,
                "evidence_id": evidence_id,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    run = {
        "schema_version": "ocbrain.seal-truth-run.v1",
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "action": "seal-triggered-compile",
        "model": "deterministic-local",
        "input_digest": digest,
        "evidence_count": 1,
        "accepted_count": 1,
        "applied_count": 1,
        "release_id": release_id,
        "belief_id": belief_id,
    }
    wiki_count = materialize_wiki(conn, wiki_dir, run=run)
    vector_result = rebuild_local_vectors(conn)
    return {
        "status": "compiled",
        "release_id": release_id,
        "task_id": task_id,
        "belief_id": belief_id,
        "evidence_id": evidence_id,
        "wiki_count": wiki_count,
        "wiki_index": str(wiki_dir / "index.md"),
        "vector_index": vector_result,
        "superseded_beliefs": superseded,
    }


def verify_artifacts(release_dir: Path, raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("seal artifacts must be a non-empty list")
    verified: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("seal artifact entry must be an object")
        filename = required_text(item.get("filename"), "artifact filename")
        if Path(filename).name != filename:
            raise ValueError(f"unsafe artifact filename: {filename}")
        path = (release_dir / filename).resolve()
        if path.parent != release_dir.resolve() or not path.is_file():
            raise ValueError(f"sealed artifact missing: {path}")
        expected = required_text(item.get("sha256"), "artifact sha256")
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(f"sealed artifact hash mismatch: {path}")
        verified.append(
            {
                "role": required_text(item.get("role"), "artifact role"),
                "path": str(path),
                "sha256": actual,
            }
        )
    return verified


def candidate_from_closeout(
    closeout: dict[str, Any],
    *,
    release_id: str,
    seal_created_at: Any,
    fallback_task_id: str,
) -> dict[str, Any]:
    del release_id
    status = str(closeout.get("status") or "").lower()
    if status != "completed":
        raise ValueError("canonical closeout status must be completed")
    verification_status = str(closeout.get("verification_status") or "").lower()
    verifier_refs = closeout.get("verifier_refs")
    has_passed_verifier = isinstance(verifier_refs, list) and any(
        isinstance(item, dict) and item.get("status") == "passed"
        for item in verifier_refs
    )
    explicitly_verified = closeout.get("verified") is True
    if verification_status != "verified" or not has_passed_verifier:
        if not explicitly_verified:
            raise ValueError("canonical closeout must be verified and verifier-backed")
    summary = human_text(closeout.get("summary"), maximum=420)
    context = closeout.get("context") if isinstance(closeout.get("context"), dict) else {}
    project = human_text(context.get("project") or closeout.get("project"), maximum=120)
    title = human_text(
        closeout.get("title")
        or f"{context.get('task') or closeout.get('task_ref') or fallback_task_id} current truth",
        maximum=100,
    )
    decision = closeout.get("decision") if isinstance(closeout.get("decision"), dict) else {}
    uncertainty = human_text(
        closeout.get("uncertainty") or closeout.get("caveat") or decision.get("note"),
        maximum=300,
        required=False,
    )
    supersedes = closeout.get("supersedes")
    if supersedes is None:
        supersedes = []
    if not isinstance(supersedes, list) or any(
        not isinstance(item, str) or not item.strip() for item in supersedes
    ):
        raise ValueError("closeout supersedes must be a list of belief IDs")
    last_verified_at = str(
        closeout.get("closed_at")
        or closeout.get("verified_at")
        or seal_created_at
        or datetime.now(UTC).isoformat(timespec="seconds")
    )
    confidence = float(closeout.get("confidence", 0.95))
    if not 0.55 <= confidence <= 1.0:
        raise ValueError("closeout confidence must be between 0.55 and 1.0")
    return {
        "body": summary,
        "project": project,
        "title": title,
        "uncertainty": uncertainty,
        "last_verified_at": last_verified_at,
        "confidence": confidence,
        "supersedes": list(dict.fromkeys(item.strip() for item in supersedes)),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rebuild_local_vectors(conn) -> dict[str, Any]:
    core_path = connection_path(conn)
    if core_path is None:
        return {"status": "degraded", "reason": "core database path unavailable"}
    try:
        with urllib.request.urlopen(
            DEFAULT_OLLAMA_URL + "/api/tags", timeout=1
        ) as response:  # noqa: S310 - fixed loopback URL
            if response.status != 200:
                raise OSError(f"Ollama returned HTTP {response.status}")
    except OSError as exc:
        return {
            "status": "degraded",
            "reason": f"local Ollama unavailable: {type(exc).__name__}",
        }
    try:
        return build_vector_index(core_path)
    except Exception as exc:  # noqa: BLE001 - derived index must not fail truth
        return {
            "status": "degraded",
            "reason": f"vector rebuild failed: {type(exc).__name__}: {exc}",
        }


def human_text(value: Any, *, maximum: int, required: bool = True) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise ValueError("required human-readable text is missing")
    if len(text) > maximum:
        raise ValueError(f"human-readable text exceeds {maximum} characters")
    return text


def required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


__all__ = ["candidate_from_closeout", "compile_sealed_release", "verify_artifacts"]
