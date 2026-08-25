"""ocbrain configuration surface.

One config module for every v0.2 tunable (spec §3, resolution R1). The public
entry point is :func:`load_config`, which layers, in order:

1. hard-coded defaults (the section dataclasses below),
2. an optional JSON file at ``$OCBRAIN_CONFIG`` (default
   ``~/.ocbrain/ocbrain.config.json``, with legacy checkout fallback),
3. ``OCBRAIN_<SECTION>_<FIELD>`` environment overrides.

``DatasetConfig`` is the ``dataset`` section here — there is deliberately no
separate ``dataset/config.py`` (R1). The single shared ``correction.threshold``
key (R1/R2) lives on :class:`CorrectionConfig`.

Secrets are never stored: ``JudgeConfig.api_key_env`` holds the *name* of an
environment variable, never its value.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

# Config lives beside the data it configures, not beside the code. The historical
# default was the *relative* ``data/ocbrain.config.json``, which made three things
# true at once: resolution depended on the working directory, a `git clean -xfd`
# or fresh clone silently discarded operator settings, and a test suite run from a
# checkout inherited whatever that checkout happened to have. A brain whose
# curator settings vanished that way keeps exiting 0 while promoting nothing.
#
# ``~/.ocbrain/ocbrain.config.json`` is checked first and is the documented home.
# The old checkout-relative path is still honored when it exists and the new one
# does not, so an existing install keeps working until it moves.
USER_CONFIG_PATH = Path("~/.ocbrain/ocbrain.config.json").expanduser()
LEGACY_CONFIG_PATH = Path("data/ocbrain.config.json")


def default_config_path() -> Path:
    """Resolve the config path: ``$OCBRAIN_CONFIG``, then user, then legacy."""
    if override := os.environ.get("OCBRAIN_CONFIG"):
        return Path(override).expanduser()
    if USER_CONFIG_PATH.exists():
        return USER_CONFIG_PATH
    if LEGACY_CONFIG_PATH.exists():
        return LEGACY_CONFIG_PATH
    return USER_CONFIG_PATH


# Retained for compatibility. Previously this read $OCBRAIN_CONFIG at *import*
# time, so it went stale the moment the environment changed; call
# ``default_config_path()`` instead.
DEFAULT_CONFIG_PATH = USER_CONFIG_PATH


@dataclass(frozen=True)
class AutopilotConfig:
    lock_path: str = "data/autopilot.lock"
    snapshot_dir: str = "data/snapshots/"
    snapshot_keep: int = 3
    stage_budget_seconds: int = 300
    # Per-stage wall-clock overrides (seconds). A stage named here uses its own
    # budget; every other budget-aware stage falls back to stage_budget_seconds.
    # e.g. {"dataset_mine": 900}. Set via config JSON / OCBRAIN_AUTOPILOT_STAGE_BUDGETS.
    stage_budgets: dict[str, int] = field(default_factory=dict)
    runtimes_excerpt: list[str] = field(default_factory=list)
    # Named stage sequences retained for explicit, manual compatibility runs.
    # No recurring light/heavy scheduler is installed or enabled by ocbrain.
    profiles: dict[str, list[str]] = field(
        default_factory=lambda: {
            "light": [
                "migrate",
                "review",
                "autolabel",
                "tripwires",
                "promote",
                "excerpt_render",
                "maintain",
            ],
            "heavy": [
                "snapshot",
                "migrate",
                "harvest",
                "injection_scan",
                "review",
                "compile",
                "autolabel",
                "tripwires",
                "promote",
                "excerpt_render",
                "maintain",
                "dataset_mine",
                "dataset_export",
            ],
        }
    )
    # Locking discipline across profiles. ``shared`` == manually requested
    # light and heavy runs contend for the same autopilot lock.
    profile_locks: str = "shared"
    # A running profile checkpoints a durable deadman row after every completed
    # stage. These historical deadman windows remain available to an explicitly
    # invoked observer; there is no default stallcheck schedule.
    profile_deadman_seconds: dict[str, int] = field(
        default_factory=lambda: {
            "light": 3600,
            "heavy": 14400,
            "full": 14400,
            "manual": 3600,
        }
    )
    # Reclaim a large WAL only after dataset mining has committed every bounded
    # writer batch. Small WALs are left to SQLite's normal autocheckpoint path.
    checkpoint_after_dataset_mine: bool = True
    checkpoint_wal_min_bytes: int = 64 * 1024 * 1024
    # Transient SQLite writers (MCP feedback / stallcheck) must not turn an
    # otherwise healthy harvest into a partial run. Retries remain bounded by
    # both this count and the harvest stage deadline.
    sqlite_lock_retries: int = 4
    sqlite_lock_backoff_seconds: float = 0.25


@dataclass(frozen=True)
class ReviewConfig:
    settle_minutes: int = 30
    min_tool_calls_success: int = 5
    session_roots: list[str] = field(
        default_factory=lambda: [
            "~/.openclaw/agents",
            "~/.claude/projects",
            "~/.codex",
            "~/.hermes/sessions",
        ]
    )


@dataclass(frozen=True)
class CorrectionConfig:
    # Shared threshold: review's user_correction signal AND DPO pair mining (R1/R2).
    threshold: float = 0.6


@dataclass(frozen=True)
class LabelsConfig:
    half_life_days: float = 30.0
    good_threshold: float = 0.35
    bad_threshold: float = -0.35
    min_mass: float = 0.6
    hard_bad_weight: float = 0.9


@dataclass(frozen=True)
class QuarantineConfig:
    bad_feedback_count: int = 2
    bad_feedback_window_days: int = 7
    thrash_count: int = 3
    thrash_window_days: int = 14


@dataclass(frozen=True)
class PromoteConfig:
    min_confidence: float = 0.6
    max_injected: int = 40
    max_chars: int = 6000
    decay_days: int = 30
    bootstrap_min_confidence: float = 0.85
    # One-time human-authored seeding of the injectable memory set. ``sources``
    # names the harvest origins (e.g. curated ``memory_file`` doctrine) whose
    # high-confidence rows may be bootstrapped into memory up to ``cap`` (v0.3).
    human_bootstrap: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": True,
            "sources": ["memory_file"],
            "cap": 15,
        }
    )


@dataclass(frozen=True)
class JudgeConfig:
    # Hosted inference is fail-closed. A local operator must explicitly enable
    # it in untracked configuration in addition to supplying credentials.
    enabled: bool = False
    api_key_env: str = "OPENAI_API_KEY"  # variable NAME only; value never persisted
    model: str = "gpt-5-mini"
    daily_usd_cap: float = 0.50
    batch_size: int = 20
    per_run_item_cap: int = 100
    signal_weight: float = 0.4
    timeout_seconds: float = 45.0
    timeout_max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    # {model: {"prompt": usd_per_mtok, "completion": usd_per_mtok}}; supplied via
    # config JSON so no price is baked into source.
    price_per_mtok: dict[str, dict[str, float]] = field(default_factory=dict)
    # Candidate-filtering knobs (v0.3). ``sources`` whitelists which knowledge
    # origins the judge grades; ``exclude_catalog_docs`` keeps the 101k-file
    # catalog backlog out of the graded set so spend is not wasted on it.
    targeting: dict[str, Any] = field(
        default_factory=lambda: {
            "sources": ["retrieval_touched", "lesson", "session_derived"],
            "exclude_catalog_docs": True,
        }
    )


@dataclass(frozen=True)
class DatasetConfig:
    # Training and training-pilot preparation are paused by default. Mining,
    # classification, local grading, selection, export, and human audit can
    # continue without enabling this authority boundary.
    training_enabled: bool = False
    sft_min_assistant_chars: int = 80
    sft_max_context_turns: int = 12
    sft_max_context_chars: int = 16000
    dpo_side_chars: list[int] = field(default_factory=lambda: [40, 8000])
    include_tool_turns: bool = False
    tool_result_truncate: int = 500
    # Identity-bearing persona selectors (telegram sender ids / usernames, git
    # author name+email strings, and the persona system prompt) ship EMPTY /
    # generic. This repo is public; no real ids, usernames, emails, or names may
    # live in committed code. The operator supplies real values via the config
    # JSON file or OCBRAIN_DATASET_* env overrides (never committed).
    persona_author_ids: list[str] = field(default_factory=list)
    # Founder feedback authors: telegram sender ids whose corrections / approvals /
    # thanks carry extra weight in the label fold and get author-provenance stamped
    # on mined DPO pairs. Each entry is a ``{"id": "<sender_id>", "weight": <float>}``
    # dict supplied by the LOCAL config JSON (never committed — this repo is public).
    # Ships EMPTY: an author absent from this list is a generic user (weight 1.0).
    # Membership here does NOT admit an author into the persona/voice stream; that is
    # governed solely by ``persona_author_ids`` (a founder like a co-founder can be a
    # feedback author WITHOUT ever becoming a persona target).
    founder_feedback_authors: list[dict] = field(default_factory=list)
    persona_direct_agents: list[str] = field(default_factory=lambda: ["main"])
    persona_git_repos: list[str] = field(default_factory=list)
    persona_git_authors: list[str] = field(default_factory=list)
    persona_authored_globs: list[str] = field(default_factory=list)
    persona_system_prompt: str = "You are the operator. Reply as they would."
    export_dir: str = "data/datasets"
    export_min_scope: str = "workspace"
    export_min_label: str = "good"
    # Optional local-LLM grade threshold. ``None`` preserves the v0.3 export
    # behavior; when set, ungraded rows and rows below the threshold stay local
    # but are withheld from the training export.
    export_min_grade: float | None = None
    learning_db: str = "~/.openclaw/learning.db"
    commitments_path: str = "~/.openclaw/commitments/commitments.json"
    cron_state_path: str = "~/.openclaw/cron/jobs-state.json"
    # Curated memory / identity / doctrine files to harvest as ``memory_file``
    # evidence, in addition to the transcript session_roots. Absolute paths or
    # globs; ships EMPTY (public repo). The operator points these at high-value
    # doctrine outside the session roots — e.g. per-workspace MEMORY.md / IDENTITY
    # files that the transcript harvest never reaches.
    memory_globs: list[str] = field(default_factory=list)
    # Relax the DPO structural pair gate (v0.3). The strict gate rejected both
    # real founder corrections in the overnight run; when true, mining admits a
    # pair on softer structural evidence. Defaults on for v0.3.
    dpo_relaxed_gate: bool = True
    # Mining never holds SQLite's single-writer lock across an entire corpus.
    # Commit after either bound and record wait/hold telemetry in the stage
    # result. Smaller batches make MCP feedback and stallcheck writes responsive.
    write_batch_size: int = 50
    write_batch_seconds: float = 2.0


@dataclass(frozen=True)
class DatasetGradingConfig:
    """Privacy-preserving local LLM grading.

    Dataset text is more sensitive than ordinary knowledge metadata and the
    corpus is contractually local-only. The grader therefore accepts loopback
    HTTP endpoints only; remote/hosted URLs are rejected in code.
    """

    endpoint: str = "http://127.0.0.1:11434/api/chat"
    model: str = ""
    timeout_seconds: int = 180
    per_run_item_cap: int = 100
    daily_item_cap: int = 500
    prompt_version: str = "dataset-rubric-v3-human-calibration-anchors"
    parallel_requests: int = 1
    # Optional owner-only JSONL used to calibrate the local grader before a
    # grade/re-selection pass. Empty preserves ordinary local grading. A supplied
    # file must carry per-row named-human provenance; AI/delegated triage labels
    # can never satisfy this gate.
    calibration_path: str = ""
    calibration_min_agreement: float = 0.90
    calibration_min_items: int = 150


@dataclass(frozen=True)
class TeacherConfig:
    """Hosted-teacher request packaging authority.

    The teacher helper never dispatches a network call itself. Keeping even the
    egress package behind an explicit opt-in prevents a runtime from treating a
    prepared request as authorization for hosted inference.
    """

    enabled: bool = False


@dataclass(frozen=True)
class ArchiveConfig:
    # Maintenance-lane archival of never-referenced catalog docs (v0.3). A catalog
    # doc untouched by any retrieval for ``catalog_never_referenced_days`` is
    # eligible for archival, up to ``batch_cap`` rows per pass.
    enabled: bool = True
    catalog_never_referenced_days: int = 14
    batch_cap: int = 5000


@dataclass(frozen=True)
class EmbedConfig:
    # Semantic embedding of knowledge rows for vector attribution (v0.3),
    # replacing FTS-only attribution. Secrets are never stored: ``api_key_env``
    # holds the NAME of an env var, never its value. ``daily_usd_cap`` bounds spend.
    enabled: bool = False
    provider: str = "openai"
    model: str = "text-embedding-3-small"
    daily_usd_cap: float = 0.25
    batch_size: int = 128
    api_key_env: str = "OPENAI_API_KEY"  # variable NAME only; value never persisted
    price_per_mtok: dict[str, float] = field(
        default_factory=lambda: {"text-embedding-3-small": 0.02}
    )


@dataclass(frozen=True)
class ExcerptRenderConfig:
    # Autopilot ``excerpt_render`` stage (v0.3): render the injectable memory view
    # into the managed block of runtime files after ``promote``. ``targets`` is a
    # list of file paths whose ``BEGIN/END OCBRAIN MANAGED BLOCK`` is written or
    # updated each cycle; content OUTSIDE the markers is never touched, and an
    # unchanged block is not rewritten (mtime preserved). Ships EMPTY (public
    # repo) — the operator points ``targets`` at real runtime files (e.g.
    # per-workspace ``MEMORY.md``) via the LOCAL config JSON. ``scope`` / ``limit``
    # bound what is rendered; the char budget comes from ``promote.max_chars``.
    targets: list[str] = field(default_factory=list)
    scope: str | None = None
    limit: int = 40


@dataclass(frozen=True)
class RetrievalConfig:
    # Hybrid ranking gates for ``search_core_v1``. These were module constants
    # until an operator needed to tune serving precision without editing source.
    # Defaults match the shipped constants in ``core_v1``; raising the floors
    # trades recall for precision, lowering them does the reverse.
    #
    # ``min_dense_cosine`` is the floor for a candidate the lexical arm also
    # found; ``min_dense_only_cosine`` is the stricter floor for a candidate only
    # the dense arm found. ``require_dense_support`` additionally holds lexical
    # hits to ``min_dense_cosine`` when the dense arm is healthy, which is what
    # keeps a shared generic token from serving an unrelated belief.
    hybrid_rrf_k: int = 60
    min_dense_cosine: float = 0.30
    min_dense_only_cosine: float = 0.55
    min_lexical_query_term_matches: int = 2
    min_redundant_lexical_strength_ratio: float = 0.50
    require_dense_support: bool = True
    # Retrieval feedback shifts a belief's score by ``1 + boost``. The boost is
    # ``avg_signal * weight``, damped by observation count and clamped to
    # +/-``feedback_clamp``. Set ``feedback_weight`` to 0 to ignore feedback.
    feedback_weight: float = 0.125
    feedback_clamp: float = 0.25
    feedback_prior_observations: float = 3.0


@dataclass(frozen=True)
class ScopesConfig:
    """Operator vocabulary for scope ids: folding and an alias table.

    Callers name their own scope. The same project therefore arrives spelled a
    dozen ways ("Coframe Brain", "coframe-brain", "coframe_brain__v2"), and scope
    matching is exact string equality, so every spelling that is not the stored
    one reaches nothing. ``fold_enabled`` collapses case and separator noise;
    ``aliases`` handles the rest, where two genuinely different names mean the
    same scope.

    ``aliases`` maps a FOLDED, fully prefixed scope id to the canonical fully
    prefixed id, e.g. ``{"project:coframe-brain": "project:coframe"}``. An alias
    may rename a scope but never re-type it: a mapping whose target carries a
    different ``type:`` prefix is ignored, so the table can never be used to
    promote a project belief into ``global:doctrine`` behind the ledger's back.

    Ships EMPTY. This repo is public and real project names are operator data;
    an empty table reproduces today's exact-match behavior.
    """

    aliases: dict[str, str] = field(default_factory=dict)
    fold_enabled: bool = True


@dataclass(frozen=True)
class CuratorConfig:
    # Which evidence the wiki curator may send to a model, and which model.
    #
    # `egress_policies` is the operator's standing declaration of intent. It
    # ships as `hosted_ok` only, so a fresh install sends nothing it was not
    # explicitly given. An operator running a brain whose evidence is all
    # `local_only` -- the default for anything written through a client -- can add
    # that policy here to let their own curator read their own notes.
    #
    # Two things are NOT configurable and are enforced in code regardless:
    # `prohibited` egress and `secret` visibility are never eligible. Those are
    # the floor, not a default.
    #
    # Every applied run records an egress audit naming exactly what was sent.
    egress_policies: list[str] = field(default_factory=lambda: ["hosted_ok"])
    visibilities: list[str] = field(default_factory=lambda: ["public", "internal"])
    provider: str = "anthropic"
    model: str = ""  # empty means the provider's default
    max_beliefs: int = 24
    current_ttl_days: int = 90
    # Which project scopes the scheduled curator compiles, in order. A single
    # pinned project is a wiki that freezes the moment work moves to a second
    # scope, and the evidence keeps arriving regardless: one real brain had 574
    # eligible objects spread over ~40 project scopes while the pin curated 19.
    #
    # Ships as the historical single pin. Real project names are operator data
    # and this repo is public, so the list an operator actually wants is set in
    # their own config file, never here.
    projects: list[str] = field(default_factory=lambda: ["workspace"])
    # A project with fewer eligible objects than this is skipped, and reported as
    # skipped, rather than spending a hosted call on a handful of rows. Set to 1
    # to curate every project with any eligible evidence at all.
    min_evidence_per_project: int = 3


@dataclass(frozen=True)
class DeslopConfig:
    # Whether a client's closeout is refused when its summary trips an enforced
    # slop rule. Off by default, and the default is the point: a rejected
    # closeout loses the client's work, and the closeout-to-evidence path is the
    # single largest supply of curator-eligible evidence. Findings ride along in
    # the receipt as `slop_findings` so the writer sees them either way; turn
    # this on once you trust the rules against your own corpus.
    reject_closeout_slop: bool = False
    # Repairs applied per unattended `ocbrain deslop --apply` run. A cap means a
    # rule that starts over-firing damages a handful of beliefs, not the corpus.
    max_repairs_per_run: int = 8


@dataclass(frozen=True)
class OcbrainConfig:
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    scopes: ScopesConfig = field(default_factory=ScopesConfig)
    curator: CuratorConfig = field(default_factory=CuratorConfig)
    deslop: DeslopConfig = field(default_factory=DeslopConfig)
    autopilot: AutopilotConfig = field(default_factory=AutopilotConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    correction: CorrectionConfig = field(default_factory=CorrectionConfig)
    labels: LabelsConfig = field(default_factory=LabelsConfig)
    quarantine: QuarantineConfig = field(default_factory=QuarantineConfig)
    promote: PromoteConfig = field(default_factory=PromoteConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    dataset_grading: DatasetGradingConfig = field(default_factory=DatasetGradingConfig)
    teacher: TeacherConfig = field(default_factory=TeacherConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    embed: EmbedConfig = field(default_factory=EmbedConfig)
    excerpt_render: ExcerptRenderConfig = field(default_factory=ExcerptRenderConfig)


def _coerce(current: Any, incoming: Any) -> Any:
    """Coerce an incoming (JSON/env) value to the type of the current default."""
    if isinstance(current, bool):
        if isinstance(incoming, str):
            return incoming.strip().lower() in {"1", "true", "yes", "on"}
        return bool(incoming)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(incoming)
    if isinstance(current, float):
        return float(incoming)
    if isinstance(current, list):
        if isinstance(incoming, str):
            parsed = json.loads(incoming)
            return list(parsed) if isinstance(parsed, list) else [parsed]
        return list(incoming)
    if isinstance(current, dict):
        if isinstance(incoming, str):
            return dict(json.loads(incoming))
        return dict(incoming)
    return incoming


def _apply_section_overrides(section: Any, overrides: dict[str, Any]) -> Any:
    """Return a copy of a frozen section dataclass with ``overrides`` applied."""
    valid = {f.name for f in fields(section)}
    changes: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in valid:
            continue
        changes[key] = _coerce(getattr(section, key), value)
    return replace(section, **changes) if changes else section


def _env_overrides(section_name: str, section: Any) -> dict[str, Any]:
    """Collect ``OCBRAIN_<SECTION>_<FIELD>`` env vars for one section."""
    overrides: dict[str, Any] = {}
    for f in fields(section):
        env_key = f"OCBRAIN_{section_name.upper()}_{f.name.upper()}"
        if env_key in os.environ:
            overrides[f.name] = os.environ[env_key]
    return overrides


def load_config(
    path: Path | str | None = None, *, env: dict[str, str] | None = None
) -> OcbrainConfig:
    """Load config from defaults + optional JSON file + env overrides.

    ``path`` defaults to :func:`default_config_path`. A missing file is fine
    (defaults win). ``env`` defaults to ``os.environ``.
    """
    if env is not None:
        # Temporarily consult the provided mapping for env overrides.
        saved = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(env)
            return _load_config_from_environ(path)
        finally:
            os.environ.clear()
            os.environ.update(saved)
    return _load_config_from_environ(path)


def _load_config_from_environ(path: Path | str | None) -> OcbrainConfig:
    config_path = Path(path).expanduser() if path is not None else default_config_path()
    file_data: dict[str, Any] = {}
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            file_data = loaded

    cfg = OcbrainConfig()
    section_changes: dict[str, Any] = {}
    for f in fields(cfg):
        section = getattr(cfg, f.name)
        if not is_dataclass(section):
            continue
        overrides: dict[str, Any] = {}
        from_file = file_data.get(f.name)
        if isinstance(from_file, dict):
            overrides.update(from_file)
        overrides.update(_env_overrides(f.name, section))
        if overrides:
            section_changes[f.name] = _apply_section_overrides(section, overrides)
    return replace(cfg, **section_changes) if section_changes else cfg


def describe_config(path: Path | str | None = None) -> dict[str, Any]:
    """Report the effective config and where every value came from.

    A layered config is only usable if an operator can see which layer won. This
    labels each field ``default``, ``file``, or ``env`` and names the file it
    resolved, so "why is the curator sending nothing" is one command rather than
    an archaeology exercise.
    """
    config_path = Path(path).expanduser() if path is not None else default_config_path()
    file_data: dict[str, Any] = {}
    if config_path.exists():
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            file_data = loaded
    effective = load_config(config_path)
    defaults = OcbrainConfig()

    sections: dict[str, Any] = {}
    for f in fields(effective):
        section = getattr(effective, f.name)
        if not is_dataclass(section):
            continue
        default_section = getattr(defaults, f.name)
        from_file = file_data.get(f.name) if isinstance(file_data.get(f.name), dict) else {}
        env_keys = set(_env_overrides(f.name, section))
        entries: dict[str, Any] = {}
        for field_def in fields(section):
            if field_def.name in env_keys:
                source = "env"
            elif field_def.name in from_file:
                source = "file"
            else:
                source = "default"
            entries[field_def.name] = {
                "value": getattr(section, field_def.name),
                "source": source,
                "default": getattr(default_section, field_def.name),
            }
        sections[f.name] = entries
    return {
        "config_path": str(config_path),
        "config_path_exists": config_path.exists(),
        "user_config_path": str(USER_CONFIG_PATH),
        "legacy_config_path": str(LEGACY_CONFIG_PATH),
        "env_override_pattern": "OCBRAIN_<SECTION>_<FIELD>",
        "sections": sections,
    }


# --------------------------------------------------------------------------- #
# Founder feedback helpers
# --------------------------------------------------------------------------- #
def founder_ids(cfg: OcbrainConfig) -> list[str]:
    """Return the configured founder-feedback author ids (attribution only).

    These ids let the transcript parser stamp ``authored_by`` on a founder's turns
    even when the founder is not a persona author, so their corrections/approvals
    can be weighted and their DPO pairs tagged. Being here never admits an author
    into the persona/voice stream (that is ``persona_author_ids`` only).
    """
    out: list[str] = []
    for entry in cfg.dataset.founder_feedback_authors:
        if isinstance(entry, dict):
            ident = str(entry.get("id") or "").strip()
        else:
            ident = str(entry or "").strip()
        if ident:
            out.append(ident)
    return out


def founder_weight(cfg: OcbrainConfig, author_id: str | None) -> float:
    """Weight multiplier for a signal authored by ``author_id`` (1.0 == generic).

    A founder in ``founder_feedback_authors`` carries their configured weight; an
    author absent from the list (or ``None``) is a generic user at 1.0. A present
    entry with a missing/invalid ``weight`` also falls back to 1.0.
    """
    if not author_id:
        return 1.0
    target = str(author_id).strip()
    for entry in cfg.dataset.founder_feedback_authors:
        if not isinstance(entry, dict):
            if str(entry or "").strip() == target:
                return 1.0
            continue
        if str(entry.get("id") or "").strip() == target:
            try:
                weight = float(entry.get("weight", 1.0))
            except (TypeError, ValueError):
                return 1.0
            return weight if weight > 0 else 1.0
    return 1.0
