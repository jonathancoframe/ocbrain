from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

SCOPE_TYPES = {
    "global",
    "project",
    "repo",
    "client",
    "personal_finance",
    "task",
    "session",
    "legacy_unscoped",
}
VISIBILITIES = {"public", "internal", "confidential", "secret"}
EGRESS_POLICIES = {"hosted_ok", "local_only", "approval_required", "prohibited"}

LOCAL_MODEL_TARGET = "local_model"
HOSTED_MODEL_TARGET = "hosted_model"
DELIVERY_TARGETS = {LOCAL_MODEL_TARGET, HOSTED_MODEL_TARGET}

DEFAULT_GLOBAL_SCOPE_ID = "global:doctrine"

# Scope components a caller types by hand and spells inconsistently. Folding
# these collapses "Coframe Brain", "coframe-brain", and "coframe_brain" onto one
# id. Task and session ids are machine-minted, high-cardinality, and often
# case-significant; folding them would risk collapsing two distinct ids into one,
# so they are only trimmed.
FOLDED_CONTEXT_FIELDS: tuple[str, ...] = ("project", "repo", "client")
TRIMMED_CONTEXT_FIELDS: tuple[str, ...] = ("task", "session")

# Tables whose ``scope_id`` inventory may be consulted by
# ``matching_stored_scope_ids``. The name is interpolated into SQL, so it is
# selected from this fixed set rather than taken from the caller verbatim.
SCOPE_INVENTORY_TABLES = frozenset({"current_beliefs", "evidence_objects"})

_SCOPE_SEPARATORS = re.compile(r"[\s_\-]+")
# ``repo`` is routinely a filesystem path — ``retrieve`` and ``shared_context``
# call ``Path(context.repo).resolve()`` on it. Folding a path renames the
# directory, so a path-shaped component is trimmed and otherwise left alone. The
# rule lives in the fold itself so the caller side and the stored side agree.
_PATH_SHAPED = re.compile(r"[/\\]|^~")

_DEFAULT_SCOPES = (True, {})
_SCOPES_CACHE: tuple[Any, tuple[bool, dict[str, str]]] | None = None


def _scopes_cache_key() -> Any:
    """Identify the config state cheaply enough to consult on every fold.

    ``scope_match`` runs per candidate row, so re-reading and re-parsing the
    config file each time is not affordable. A stat of the resolved file plus the
    two env overrides that can change the answer is, and it stays correct when an
    operator edits the file or a test points ``$OCBRAIN_CONFIG`` somewhere else.
    """
    stamp: tuple[str, int, int] = ("<unavailable>", 0, 0)
    try:
        from ocbrain.config import default_config_path

        path = default_config_path()
        info = path.stat()
        stamp = (str(path), info.st_mtime_ns, info.st_size)
    except Exception:  # noqa: BLE001 - a missing/unreadable config is normal
        pass
    return (
        stamp,
        os.environ.get("OCBRAIN_CONFIG"),
        os.environ.get("OCBRAIN_SCOPES_ALIASES"),
        os.environ.get("OCBRAIN_SCOPES_FOLD_ENABLED"),
    )


def _scope_settings() -> tuple[bool, dict[str, str]]:
    """Resolve ``(fold_enabled, aliases)``, failing open to shipped defaults.

    Mirrors ``core_v1._retrieval_tuning``: scope resolution sits in front of
    every read and every write, so a malformed config must degrade to plain
    exact matching rather than take the brain down.
    """
    global _SCOPES_CACHE
    key = _scopes_cache_key()
    if _SCOPES_CACHE is not None and _SCOPES_CACHE[0] == key:
        return _SCOPES_CACHE[1]
    settings = _DEFAULT_SCOPES
    try:
        from ocbrain.config import load_config

        section = load_config().scopes
        aliases = {
            str(name).strip().lower(): str(target).strip()
            for name, target in dict(section.aliases).items()
            if str(name).strip() and str(target).strip()
        }
        settings = (bool(section.fold_enabled), aliases)
    except Exception:  # noqa: BLE001 - config problems must not break scoping
        settings = _DEFAULT_SCOPES
    _SCOPES_CACHE = (key, settings)
    return settings


def fold_scope_component(value: Any) -> str | None:
    """Collapse one scope component to its canonical spelling.

    Lowercases, trims, collapses runs of whitespace, underscores, and hyphens to
    a single ``-``, and strips leading/trailing ``-``. Returns ``None`` for a
    value that is empty or folds away entirely, matching ``optional_str``.
    A path-shaped value is trimmed only, never folded.

    Folding is idempotent: ``fold(fold(x)) == fold(x)``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _scope_settings()[0] or _PATH_SHAPED.search(text):
        # Folding off means "behave exactly as before": no case change either.
        return text
    return _SCOPE_SEPARATORS.sub("-", text.lower()).strip("-") or None


def fold_scope_id(scope_id: Any) -> str:
    """Fold the value half of a ``type:value`` scope id, keeping the prefix.

    The prefix is left verbatim. Scope types are code-minted and several contain
    underscores (``personal_finance``, ``legacy_unscoped``); folding those would
    turn a valid type into one that matches nothing.
    """
    text = str(scope_id or "").strip()
    if not text:
        return text
    prefix, separator, remainder = text.partition(":")
    if not separator:
        return fold_scope_component(text) or text
    folded = fold_scope_component(remainder)
    if folded is None:
        return text
    return f"{prefix}:{folded}"


def resolve_scope_alias(scope_id: Any) -> str:
    """Fold ``scope_id`` and map it through the operator's alias table.

    An alias renames a scope; it never re-types one. A mapping whose target
    carries a different ``type:`` prefix is ignored, so the alias table cannot be
    used to make a project belief globally reachable without a ``scope_promoted``
    event. An unknown id passes through folded.
    """
    folded = fold_scope_id(scope_id)
    if not folded:
        return folded
    target = _scope_settings()[1].get(folded.lower())
    if not target:
        return folded
    resolved = fold_scope_id(target)
    if resolved.partition(":")[0] != folded.partition(":")[0]:
        return folded
    return resolved


def canonical_scope_component(scope_type: str, value: Any) -> str | None:
    """Canonical spelling of a context component, alias table applied.

    An alias whose target re-types the scope is ignored here for the same reason
    it is ignored in :func:`resolve_scope_alias`, and because a re-typed target
    cannot be spliced back into a ``{scope_type}:{value}`` id at all.
    """
    folded = fold_scope_component(value)
    if folded is None:
        return None
    resolved = resolve_scope_alias(f"{scope_type}:{folded}")
    prefix = f"{scope_type}:"
    if resolved.startswith(prefix):
        return resolved[len(prefix) :] or folded
    return folded


def fold_scope_dict(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Canonicalize an explicit, client-supplied scope payload before tagging.

    Applied at the MCP argument boundary only. Folding must NOT happen inside
    ``ScopeTag.__post_init__`` or ``ScopeTag.from_dict``: those run during
    projection replay and on stored handle scopes, and an alias-table-dependent
    rewrite there would make a ledger refold depend on today's config.
    """
    if not data:
        return data
    folded = dict(data)
    if folded.get("scope_id"):
        folded["scope_id"] = resolve_scope_alias(folded["scope_id"])
    elif folded.get("project"):
        scope_type = str(folded.get("scope_type") or folded.get("tier") or "project")
        canonical = canonical_scope_component(scope_type, folded["project"])
        if canonical is not None:
            folded["project"] = canonical
    return folded


def matching_stored_scope_ids(
    conn: sqlite3.Connection,
    table: str,
    compatible: Iterable[str],
) -> list[str]:
    """Widen a caller's compatible set to the stored spellings that mean the same.

    Stored rows are never rewritten, so a belief written as ``project:Coframe
    Brain`` stays that way forever. Matching only the caller's own canonical form
    would leave it unreachable. Fold the stored ``SELECT DISTINCT scope_id``
    inventory instead — tens of values, not tens of thousands — and admit the
    rows whose canonical form is one the caller can already see.

    This only ever adds spellings of scopes the caller already named. It never
    admits a scope the caller did not name, so client and confidential inventory
    stays behind the same gate it was behind before.
    """
    requested = sorted({str(value) for value in compatible})
    if table not in SCOPE_INVENTORY_TABLES:
        raise ValueError(f"unsupported scope inventory table: {table}")
    targets = {resolve_scope_alias(value) for value in requested}
    matched = set(requested)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT scope_id FROM {table}"  # noqa: S608 - name is from a fixed set
        ).fetchall()
    except sqlite3.Error:
        # A schema without this table (or mid-migration) must not break serving.
        return requested
    for row in rows:
        stored = str(row[0])
        if stored in matched:
            continue
        if resolve_scope_alias(stored) in targets:
            matched.add(stored)
    return sorted(matched)


@dataclass(frozen=True)
class ScopeTag:
    scope_type: str
    scope_id: str
    visibility: str = "internal"
    egress_policy: str = "local_only"
    provenance: str = "explicit"

    def __post_init__(self) -> None:
        if self.scope_type not in SCOPE_TYPES:
            raise ValueError(f"invalid scope_type: {self.scope_type}")
        if not self.scope_id:
            raise ValueError("scope_id is required")
        if self.visibility not in VISIBILITIES:
            raise ValueError(f"invalid visibility: {self.visibility}")
        if self.egress_policy not in EGRESS_POLICIES:
            raise ValueError(f"invalid egress_policy: {self.egress_policy}")

    @property
    def confidential(self) -> bool:
        return self.visibility in {"confidential", "secret"} or self.scope_type == "client"

    @property
    def hosted_egress_allowed(self) -> bool:
        return self.egress_policy == "hosted_ok" and not self.confidential

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ScopeTag:
        if not data:
            return legacy_unscoped_scope()
        return cls(
            scope_type=str(data.get("scope_type") or data.get("tier") or "legacy_unscoped"),
            scope_id=str(data.get("scope_id") or inferred_scope_id(data)),
            visibility=str(data.get("visibility") or default_visibility(data)),
            egress_policy=str(data.get("egress_policy") or default_egress_policy(data)),
            provenance=str(data.get("provenance") or "explicit"),
        )


@dataclass(frozen=True)
class ScopeContext:
    project: str | None = None
    repo: str | None = None
    client: str | None = None
    task: str | None = None
    session: str | None = None
    runtime: str | None = None

    def __post_init__(self) -> None:
        """Canonicalize the caller's scope once, at the only place they all meet.

        Every entry point — MCP arguments, CLI flags, auto-compile, the curator —
        builds one of these, so folding here is what stops the same project from
        fragmenting into a new scope per spelling. ``object.__setattr__`` is the
        supported way to normalize a frozen dataclass in place.
        """
        for name in FOLDED_CONTEXT_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            canonical = canonical_scope_component(name, value)
            if canonical != value:
                object.__setattr__(self, name, canonical)
        for name in TRIMMED_CONTEXT_FIELDS:
            value = getattr(self, name)
            if value is None:
                continue
            trimmed = optional_str(value)
            if trimmed != value:
                object.__setattr__(self, name, trimmed)

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ScopeContext:
        if not data:
            return cls()
        return cls(
            project=optional_str(data.get("project")),
            repo=optional_str(data.get("repo")),
            client=optional_str(data.get("client")),
            task=optional_str(data.get("task")),
            session=optional_str(data.get("session")),
            runtime=optional_str(data.get("runtime")),
        )

    def compatible_scope_ids(self) -> set[str]:
        ids: set[str] = {DEFAULT_GLOBAL_SCOPE_ID}
        if self.project:
            ids.add(f"project:{self.project}")
        if self.repo:
            ids.add(f"repo:{self.repo}")
        if self.client:
            ids.add(f"client:{self.client}")
        if self.task:
            ids.add(f"task:{self.task}")
        if self.session:
            ids.add(f"session:{self.session}")
        return ids


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def global_scope(*, hosted_ok: bool = True) -> ScopeTag:
    return ScopeTag(
        scope_type="global",
        scope_id=DEFAULT_GLOBAL_SCOPE_ID,
        visibility="internal",
        egress_policy="hosted_ok" if hosted_ok else "local_only",
        provenance="explicit",
    )


def legacy_unscoped_scope() -> ScopeTag:
    return ScopeTag(
        scope_type="legacy_unscoped",
        scope_id="legacy:unscoped",
        visibility="internal",
        egress_policy="local_only",
        provenance="quarantined",
    )


def resolve_write_scope(
    context: ScopeContext | None = None,
    explicit: ScopeTag | dict[str, Any] | None = None,
) -> ScopeTag:
    if isinstance(explicit, ScopeTag):
        return explicit
    if isinstance(explicit, dict):
        return ScopeTag.from_dict(explicit)
    context = context or ScopeContext()
    confidential = context.client is not None

    def inferred(scope_type: str, scope_id: str) -> ScopeTag:
        return ScopeTag(
            scope_type,
            scope_id,
            visibility="confidential" if confidential else "internal",
            egress_policy="local_only" if confidential else "approval_required",
            provenance="inferred",
        )

    if context.task:
        return inferred("task", f"task:{context.task}")
    if context.session:
        return inferred("session", f"session:{context.session}")
    if context.repo:
        return inferred("repo", f"repo:{context.repo}")
    if context.client:
        return ScopeTag(
            "client",
            f"client:{context.client}",
            visibility="confidential",
            egress_policy="local_only",
            provenance="inferred",
        )
    if context.project:
        return inferred("project", f"project:{context.project}")
    return legacy_unscoped_scope()


def scope_match(
    scope: ScopeTag,
    context: ScopeContext | None = None,
    *,
    cross_scope: bool = False,
) -> float:
    context = context or ScopeContext()
    if scope.scope_type == "global":
        return 1.0
    if scope.scope_type == "legacy_unscoped":
        return 0.05 if cross_scope else 0.0
    compatible = context.compatible_scope_ids()
    if scope.scope_id in compatible:
        return 1.25
    # Canonicalize both sides. The SQL prefilter admits rows by their stored
    # spelling; without the same fold here a row it admitted would score 0 and be
    # dropped a few lines later, which is a widened query that still serves
    # nothing.
    if resolve_scope_alias(scope.scope_id) in {
        resolve_scope_alias(value) for value in compatible
    }:
        return 1.25
    if scope.confidential:
        return 0.0
    return 0.15 if cross_scope else 0.0


def normalize_delivery_target(
    target: str | None,
    *,
    default: str = LOCAL_MODEL_TARGET,
) -> str:
    resolved = default if target is None else target
    if not isinstance(resolved, str) or resolved not in DELIVERY_TARGETS:
        allowed = ", ".join(sorted(DELIVERY_TARGETS))
        raise ValueError(f"delivery_target must be one of: {allowed}")
    return resolved


def egress_allowed(
    scope: ScopeTag,
    context: ScopeContext,
    target: str,
    *,
    cross_scope: bool = False,
) -> tuple[bool, str]:
    match = scope_match(scope, context, cross_scope=cross_scope)
    if match == 0:
        return False, "scope_mismatch"
    if target == LOCAL_MODEL_TARGET:
        return scope.egress_policy != "prohibited", "allowed_local"
    if target in {HOSTED_MODEL_TARGET, "hosted_teacher"}:
        if scope.hosted_egress_allowed:
            return True, "allowed_hosted"
        return False, f"egress_policy:{scope.egress_policy};visibility:{scope.visibility}"
    if target == "human_export":
        if scope.egress_policy in {"hosted_ok", "approval_required"}:
            return True, "allowed_export"
        return False, f"egress_policy:{scope.egress_policy}"
    return False, f"unknown_target:{target}"


def inferred_scope_id(data: dict[str, Any]) -> str:
    tier = str(data.get("scope_type") or data.get("tier") or "legacy_unscoped")
    project = data.get("project") or data.get("scope_project")
    if tier == "global":
        return DEFAULT_GLOBAL_SCOPE_ID
    if project:
        return f"{tier}:{project}"
    return "legacy:unscoped"


def default_visibility(data: dict[str, Any]) -> str:
    if (
        data.get("confidential")
        or data.get("scope_type") == "client"
        or data.get("tier") == "confidential"
    ):
        return "confidential"
    return "internal"


def default_egress_policy(data: dict[str, Any]) -> str:
    if default_visibility(data) in {"confidential", "secret"}:
        return "local_only"
    if data.get("scope_type") == "global" or data.get("tier") == "global":
        return "hosted_ok"
    return "local_only"
