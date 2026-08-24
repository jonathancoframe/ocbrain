"""procmine — a procedural-memory miner for the OCBrain trajectory corpus.

OCBrain has been quietly accumulating an outcome-labeled trajectory corpus:
~1,100 append-only ``task_closeouts`` on one side, and the raw tool-call
transcripts of four agent runtimes on the other. Nobody has mined it. This
package does, in four stages:

1. ``extract``  raw transcripts -> normalized ``(step, tool, arg_signature, result_class)``
2. ``label``    closeouts graded by how checkable their receipt is, joined to traces
3. ``mine``     per-family sequence mining -> procedure DAGs with failure branches
4. ``atlas``    a markdown report plus machine-readable ``procedures.json``

Prototype status: this reads live stores strictly read-only and writes nothing
back to the brain. It does not touch the core schema or the serving path.
"""

from __future__ import annotations

__version__ = "0.1.0"
