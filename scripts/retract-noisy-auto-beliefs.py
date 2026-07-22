#!/usr/bin/env python3
"""Retract raw and unattended beliefs without deleting ledger evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ocbrain.core_v1 import (
    append_core_event,
    is_core_v1,
    project_core_v1,
    set_automatic_activation,
)
from ocbrain.db import connect

TARGETS_SQL = """
SELECT DISTINCT
  cb.belief_id,
  CASE
    WHEN cb.belief_type = 'auto_compiled' THEN 'auto_compiled_receipt'
    WHEN EXISTS (
      SELECT 1
      FROM json_each(cb.evidence_ids) AS linked
      JOIN evidence_objects AS evidence ON evidence.evidence_id = linked.value
      WHERE evidence.kind IN ('memory_file', 'deployment_receipt')
    ) THEN 'whole_source_document'
    ELSE 'whole_transcript'
  END AS reason
FROM current_beliefs AS cb
WHERE cb.status = 'current'
  AND cb.serve = 1
  AND COALESCE(cb.belief_type, '') != 'wiki_fact'
  AND (
    cb.belief_type = 'auto_compiled'
    OR EXISTS (
      SELECT 1
      FROM json_each(cb.evidence_ids) AS linked
      JOIN evidence_objects AS evidence ON evidence.evidence_id = linked.value
      WHERE evidence.kind LIKE '%_history_file'
         OR evidence.kind IN ('memory_file', 'deployment_receipt')
    )
  )
ORDER BY cb.belief_id
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Keep OCBrain's append-only evidence ledger while retracting raw source documents, "
            "whole transcripts, and unattended closeouts from current truth."
        )
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    conn = connect(args.db.expanduser())
    try:
        if not is_core_v1(conn):
            raise ValueError("database is not an OCBrain v1 core")
        targets = [dict(row) for row in conn.execute(TARGETS_SQL)]
        counts: dict[str, int] = {}
        for target in targets:
            reason = str(target["reason"])
            counts[reason] = counts.get(reason, 0) + 1

        result: dict[str, object] = {
            "apply": bool(args.apply),
            "automatic_activation_after": False if args.apply else None,
            "target_count": len(targets),
            "targets_by_reason": counts,
            "sample_belief_ids": [str(item["belief_id"]) for item in targets[:12]],
        }
        if not args.apply:
            print(json.dumps(result, sort_keys=True))
            return 0

        conn.execute("BEGIN IMMEDIATE")
        set_automatic_activation(conn, False)
        for target in targets:
            belief_id = str(target["belief_id"])
            reason = str(target["reason"])
            append_core_event(
                conn,
                "correction_recorded",
                {
                    "schema_version": "ocbrain.correction.v1",
                    "subject": {"kind": "belief", "id": belief_id},
                    "target_layer": "belief",
                    "target_id": belief_id,
                    "op": "retract",
                    "body": f"Retracted from serving current truth: {reason}",
                    "author": "maintenance:belief-hygiene-v1",
                    "hard": True,
                },
                writer="maintenance:belief-hygiene-v1",
                project=False,
            )
        projection = project_core_v1(conn)
        conn.commit()
        result["projection"] = projection
        result["current_serving_after"] = int(
            conn.execute(
                "SELECT count(*) FROM current_beliefs WHERE status='current' AND serve=1"
            ).fetchone()[0]
        )
        result["retracted_after"] = int(
            conn.execute(
                "SELECT count(*) FROM current_beliefs WHERE status='retracted'"
            ).fetchone()[0]
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
