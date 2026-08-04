#!/usr/bin/env python3
"""Compile high-signal OCBrain evidence into a sparse, human-readable wiki.

An explicit operator-invoked hosted operation. Dry-run by default: without
``--apply`` it prints what it would send and makes no network call. Only
already-redacted, bounded evidence bodies that pass project, visibility, and
egress gates are eligible, and raw transcripts never are (they are excluded by
kind).

Which egress policies qualify is the operator's declaration, set once in
``curator.egress_policies`` or overridden with ``--egress-policy``. It ships as
``hosted_ok`` only. ``prohibited`` egress and ``secret`` visibility are refused
in code and cannot be enabled. Every applied run writes an ``egress_audits`` row
naming exactly what was sent, before it is sent.

Providers: ``anthropic`` (default), ``openai``, ``moonshot``. See
``ocbrain.curator`` for the provider backends and the local claim validation
every provider's output must pass.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ocbrain.config import load_config
from ocbrain.core_v1 import is_core_v1
from ocbrain.curator import (
    PROVIDER_DEFAULTS,
    WIKI_STATE_SCHEMA,
    apply_claims,
    input_digest,
    load_env_value,
    now_iso,
    record_curation_egress,
    request_claims,
    resolve_selection_policy,
    select_evidence,
    validate_claims,
)
from ocbrain.db import connect
from ocbrain.wiki import current_wiki_beliefs, materialize_wiki

WIKI_DIR_NAME = "wiki"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--provider",
        default="anthropic",
        choices=sorted(PROVIDER_DEFAULTS),
        help="hosted model provider (default: anthropic)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path.home() / ".common",
        help="dotenv file consulted when the API key env var is unset",
    )
    parser.add_argument("--api-key-env", help="defaults to the provider's usual variable")
    parser.add_argument("--base-url", help="defaults to the provider's endpoint")
    parser.add_argument("--model", help="defaults to the provider's mid-tier model")
    parser.add_argument("--project", default="workspace")
    parser.add_argument("--max-evidence", type=int, default=260)
    parser.add_argument("--max-beliefs", type=int, default=24)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=8_000,
        help=(
            "output token budget. On models with adaptive thinking this budget "
            "covers thinking as well as the visible answer, so leave headroom"
        ),
    )
    parser.add_argument(
        "--current-ttl-days",
        type=int,
        default=90,
        help=(
            "expiry stamped on 'current' lifecycle claims so they can age out; "
            "0 disables expiry. Durable claims never expire"
        ),
    )
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--egress-policy",
        action="append",
        choices=["hosted_ok", "approval_required", "local_only"],
        help=(
            "evidence egress policy the curator may read; repeatable. Overrides "
            "curator.egress_policies from config. `prohibited` egress and `secret` "
            "visibility are never eligible and cannot be enabled"
        ),
    )
    parser.add_argument(
        "--allow-hosted-egress",
        action="store_true",
        help=(
            "explicitly authorize bounded approval-required evidence and wiki facts "
            "for this hosted compilation; local-only, prohibited, confidential, and "
            "secret objects stay excluded"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run even when the input digest is unchanged since the last run",
    )
    args = parser.parse_args()

    defaults = PROVIDER_DEFAULTS[args.provider]
    model = args.model or defaults["model"]
    api_key_env = args.api_key_env or defaults["api_key_env"]
    base_url = args.base_url or defaults["base_url"]

    db_path = args.db.expanduser()
    wiki_dir = (args.wiki_dir or db_path.parent / WIKI_DIR_NAME).expanduser()
    state_path = wiki_dir / "state.json"
    conn = connect(db_path)
    try:
        if not is_core_v1(conn):
            raise ValueError("database is not an OCBrain v1 core")
        # The operator's standing declaration of what their curator may read,
        # from config (OCBRAIN_CURATOR_EGRESS_POLICIES / the config file), with a
        # CLI override. `prohibited` and `secret` are refused in code either way.
        curator_cfg = load_config().curator
        egress_policies = args.egress_policy or curator_cfg.egress_policies
        resolved_egress, resolved_visibility = resolve_selection_policy(
            egress_policies=egress_policies,
            visibilities=curator_cfg.visibilities,
            allow_hosted_egress=bool(args.allow_hosted_egress),
        )
        evidence = select_evidence(
            conn,
            limit=max(1, args.max_evidence),
            project=args.project,
            egress_policies=resolved_egress,
            visibilities=resolved_visibility,
        )
        existing = current_wiki_beliefs(
            conn,
            project=args.project,
            hosted_egress=True,
            allow_approval_required=bool(args.allow_hosted_egress),
        )
        digest = input_digest(evidence, existing)
        prior = {}
        if state_path.is_file():
            prior = json.loads(state_path.read_text(encoding="utf-8"))
        preview = {
            "action": "wiki-curate",
            "apply": bool(args.apply),
            "provider": args.provider,
            "model": model,
            "eligible_evidence": len(evidence),
            "eligible_kinds": sorted({str(row["kind"]) for row in evidence}),
            "input_characters": sum(len(str(row["body"])) for row in evidence),
            "input_digest": digest,
            "prior_digest_matches": prior.get("input_digest") == digest,
            "raw_transcripts_eligible": False,
            "confidential_or_prohibited_eligible": False,
            "hosted_egress_acknowledged": bool(args.allow_hosted_egress),
            "egress_policies": list(resolved_egress),
            "visibilities": list(resolved_visibility),
        }
        if not args.apply:
            print(json.dumps(preview, sort_keys=True))
            return 0
        # An unchanged digest means the model would see the same input and produce
        # the same wiki, so the scheduled path costs nothing on a quiet cycle.
        if preview["prior_digest_matches"] and not args.force:
            print(json.dumps(preview | {"status": "unchanged_no_api_call"}, sort_keys=True))
            return 0
        if not evidence:
            print(json.dumps(preview | {"status": "no_eligible_evidence"}, sort_keys=True))
            return 0

        api_key = load_env_value(args.env_file.expanduser(), api_key_env)
        if not api_key:
            raise ValueError(f"{api_key_env} is not configured")
        max_beliefs = max(1, min(args.max_beliefs, 40))
        # Record what is about to leave the machine before it leaves.
        audit_id = record_curation_egress(
            conn,
            evidence=evidence,
            provider=args.provider,
            model=model,
            project=args.project,
            egress_policies=resolved_egress,
        )
        response = request_claims(
            provider=args.provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            evidence=evidence,
            existing=existing,
            max_beliefs=max_beliefs,
            max_tokens=max(1_000, args.max_tokens),
        )
        claims, rejected = validate_claims(
            response, evidence=evidence, max_beliefs=max_beliefs
        )
        if not claims:
            raise RuntimeError(
                f"no quote-validated beliefs survived; rejected={rejected[:8]}"
            )
        applied = apply_claims(
            conn,
            claims,
            model=model,
            project=args.project,
            provider=args.provider,
            current_ttl_days=max(0, args.current_ttl_days),
        )
        run = {
            "schema_version": WIKI_STATE_SCHEMA,
            "at": now_iso(),
            "action": "wiki-curate",
            "provider": args.provider,
            "model": model,
            "egress_audit_id": audit_id,
            "input_digest": digest,
            "evidence_count": len(evidence),
            "accepted_count": len(claims),
            "rejected_count": len(rejected),
            "applied_count": len(applied["applied"]),
            "unchanged_count": len(applied["unchanged"]),
            "blocked_count": len(applied["blocked"]),
        }
        wiki_count = materialize_wiki(conn, wiki_dir, run=run)
        print(
            json.dumps(
                preview
                | {
                    "status": "completed",
                    "egress_audit_id": audit_id,
                    "accepted": len(claims),
                    "rejected": len(rejected),
                    "applied": len(applied["applied"]),
                    "unchanged": len(applied["unchanged"]),
                    "blocked": len(applied["blocked"]),
                    "wiki_current_beliefs": wiki_count,
                    "wiki_index": str(wiki_dir / "index.md"),
                    "rejection_sample": rejected[:8],
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
