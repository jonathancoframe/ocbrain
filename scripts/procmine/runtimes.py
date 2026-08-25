"""Read-side cleanup for the self-reported ``runtime`` field.

This used to run on the write path, in ``ocbrain.core_v1``: every retrieval was
stored under a guessed slug because the only thing the server knew about its
caller was a free-text string the model had typed. The server now captures the
caller's identity for itself (see ``ocbrain.provenance``), so nothing new needs
guessing.

The historical corpus still does. Rows written before server-captured
provenance carry 129 distinct ``served_to_runtime`` spellings and ~170 distinct
closeout ``runtime`` values, and any analysis over them has to fold those
together somehow. That is a mining concern, so the folder lives here.

Two folders, answering two different questions, deliberately kept apart:

* :func:`canonical_runtime` -- collapse spellings of the *same* client to one
  slug, and keep an unrecognized runtime legible rather than bucketing it.
  Returns ``None`` for "not reported", which is different from "reported but
  unrecognized".
* :func:`procmine.episodes.normalize_runtime` -- assign a runtime to one of a
  fixed set of *families* for grouping, and answer ``"unknown"`` when it cannot
  place one. It must not invent a family, so it never falls through to a slug.
"""

from __future__ import annotations

import re

# Runtimes self-report a free-text name, and the same client arrives spelled a
# dozen ways ("codex-desktop", "Codex desktop", "Codex desktop local macOS").
# Ungrouped, that makes per-client analytics and feedback aggregation useless.
# Match the client where one is identifiable and keep the slug otherwise, so an
# unrecognized runtime stays legible instead of collapsing into "unknown".
RUNTIME_CANONICAL_MARKERS: tuple[tuple[str, str], ...] = (
    ("codex", "codex"),
    ("cursor", "cursor"),
    ("claude", "claude-code"),
    ("hermes", "hermes"),
    ("telegram", "telegram"),
)


def canonical_runtime(runtime: str | None) -> str | None:
    """Collapse a self-reported runtime name to a stable slug.

    Returns ``None`` unchanged so "not reported" stays distinct from "reported
    but unrecognized". The raw value is never modified in the store; this is a
    view over it.
    """
    if runtime is None:
        return None
    slug = "-".join(re.findall(r"[a-z0-9]+", runtime.lower()))
    if not slug:
        return None
    for marker, canonical in RUNTIME_CANONICAL_MARKERS:
        if marker in slug:
            return canonical
    return slug[:64]


__all__ = ["RUNTIME_CANONICAL_MARKERS", "canonical_runtime"]
