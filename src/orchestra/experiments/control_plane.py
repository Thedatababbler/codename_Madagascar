"""Shared typed Slow Loop / Pareto / Stage-2 control-plane configuration.

Smoke, production, and reporting paths must resolve the same YAML through this
module. Stage-1 configs remain valid with all new sections optional/disabled.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestra.control.pareto.schemas import ObjectiveDirection, ParetoConfig, PreferenceProfile
from orchestra.control.slow_loop.schemas import SlowLoopBudget, SlowLoopConfig

KNOWN_OBJECTIVES = {
    "quality",
    "cost",
    "latency",
    "risk",
    "communication_overhead",
    "sum_node_latency",
}

DEFAULT_OBJECTIVES: dict[str, ObjectiveDirection] = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
    "latency": ObjectiveDirection.MINIMIZE,
    "risk": ObjectiveDirection.MINIMIZE,
    "communication_overhead": ObjectiveDirection.MINIMIZE,
}

DEFAULT_PREFERENCE_PATH = "configs/pareto/default_preferences.yaml"
DEFAULT_PRICING_PATH = "configs/pricing/backend_models.yaml"


class GraphTemplateCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    graph_path: str
    target_roles: list[str] = Field(default_factory=list)
    # Optional metadata for evidence quality; missing evidence keeps objectives
    # unavailable so the candidate stays out of the default complete frontier.
    declared_cost_usd: float | None = None
    declared_latency_seconds: float | None = None


class CandidateCatalogSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_templates: list[GraphTemplateCandidate] = Field(default_factory=list)
    concurrency_alternatives: list[int] = Field(default_factory=list)
    serialization_groups: list[list[str]] = Field(default_factory=list)
    context_budget_alternatives: dict[str, list[int]] = Field(default_factory=dict)
    allow_archive_replay: bool = True
    allow_two_edit_pairs: bool = True


class SlowLoopSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    failure_policy: Literal["keep_previous_plan", "fail_task"] = "keep_previous_plan"
    every_n_committed_subtasks: int = 1
    context_pressure_ratio: float = 0.9
    budget_pressure_ratio: float = 0.35
    max_updates_per_task: int = 4
    max_candidates_per_update: int = 3
    max_wall_time_seconds: float = 30.0
    repeated_failure_threshold: int = 2
    allowed_backend_assignments: dict[str, list[str]] = Field(default_factory=dict)
    backend_model_pools: dict[str, list[str]] = Field(default_factory=dict)


class ParetoSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_candidates: int = 8
    max_estimated_archive_size: int = 64
    max_realized_archive_size: int = 64
    allow_two_edit_pairs: bool = True
    fallback_to_rule_based: bool = False
    horizon_commits: int = 1
    allow_archive_replay: bool = True
    scalarize_without_pareto_filter: bool = False
    objectives: dict[str, ObjectiveDirection] = Field(
        default_factory=lambda: dict(DEFAULT_OBJECTIVES)
    )
    epsilon: dict[str, float] = Field(default_factory=dict)
    preference_profile: str | None = None
    preferences_path: str = DEFAULT_PREFERENCE_PATH
    pricing_registry: str = DEFAULT_PRICING_PATH

    @field_validator("objectives")
    @classmethod
    def _known_objectives(
        cls, value: dict[str, ObjectiveDirection]
    ) -> dict[str, ObjectiveDirection]:
        unknown = sorted(set(value) - KNOWN_OBJECTIVES)
        if unknown:
            raise ValueError(f"unknown Pareto objectives: {unknown}")
        for name, direction in value.items():
            if not isinstance(direction, ObjectiveDirection):
                value[name] = ObjectiveDirection(str(direction))
        return value

    @model_validator(mode="after")
    def _archive_horizon_consistency(self) -> ParetoSection:
        if self.max_estimated_archive_size < 1 or self.max_realized_archive_size < 1:
            raise ValueError("archive size limits must be >= 1")
        if self.horizon_commits < 1:
            raise ValueError("horizon_commits must be >= 1")
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")
        return self


class Stage2ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    output_root: str = "outputs/stage2_pareto"
    mode: Literal[
        "m5_rule_based",
        "m6_quality_first",
        "m6_cost_capped_quality",
        "m6_latency_capped_quality",
        "m6_robustness_first",
        "m6_balanced_knee",
        "m6_no_two_edit",
        "m6_no_archive_replay",
        "m6_scalarized_ablation",
    ] = "m6_balanced_knee"
    seed: int = 42
    fixture_plan: str = "configs/plans/stage2_pareto_three_subtasks.yaml"
    calibration_path: str | None = None
    include_oracle_diagnostics: bool = False


class ControlPlaneConfig(BaseModel):
    """Opt-in dual-frequency / Pareto control plane resolved from YAML."""

    model_config = ConfigDict(extra="forbid")

    slow_loop: SlowLoopSection = Field(default_factory=SlowLoopSection)
    pareto: ParetoSection = Field(default_factory=ParetoSection)
    preference_profile: str = "balanced_knee"
    candidate_catalog: CandidateCatalogSection = Field(
        default_factory=CandidateCatalogSection
    )
    stage2: Stage2ReportSection = Field(default_factory=Stage2ReportSection)

    @model_validator(mode="after")
    def _pareto_requires_slow_loop(self) -> ControlPlaneConfig:
        if self.pareto.enabled and not self.slow_loop.enabled:
            raise ValueError("pareto.enabled=true requires slow_loop.enabled=true")
        return self


class ResolvedParetoRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    slow_loop_config: SlowLoopConfig
    pareto_config: ParetoConfig
    preference_profile: PreferenceProfile
    candidate_catalog: CandidateCatalogSection
    pricing_registry: str
    preferences_path: str
    preference_hash: str
    objective_hash: str
    pricing_version: str
    control_plane_hash: str


def _stable_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_preference_profiles(path: str | Path) -> dict[str, PreferenceProfile]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    profiles_raw = raw.get("profiles") if isinstance(raw, dict) else None
    if not isinstance(profiles_raw, dict):
        raise ValueError(f"preference profiles missing under {path}")
    out: dict[str, PreferenceProfile] = {}
    for profile_id, body in profiles_raw.items():
        data = dict(body or {})
        data["profile_id"] = str(profile_id)
        out[str(profile_id)] = PreferenceProfile.model_validate(data)
    return out


def load_control_plane_mapping(raw: dict[str, Any]) -> ControlPlaneConfig:
    """Parse control-plane sections from an already-expanded YAML mapping."""
    if not isinstance(raw, dict):
        raise ValueError("control-plane config must be a mapping")
    preference = raw.get("preference_profile")
    pareto_raw = dict(raw.get("pareto") or {})
    if preference and "preference_profile" not in pareto_raw:
        # Top-level preference_profile is accepted for smoke / Stage-2 YAML.
        if isinstance(preference, str):
            pareto_raw.setdefault("preference_profile", preference)
    payload = {
        "slow_loop": raw.get("slow_loop") or {},
        "pareto": pareto_raw,
        "preference_profile": (
            preference
            if isinstance(preference, str)
            else pareto_raw.get("preference_profile") or "balanced_knee"
        ),
        "candidate_catalog": raw.get("candidate_catalog") or {},
        "stage2": raw.get("stage2") or {},
    }
    cfg = ControlPlaneConfig.model_validate(payload)
    # Apply catalog generation flags onto Pareto config fields.
    if "allow_two_edit_pairs" in (raw.get("candidate_catalog") or {}):
        cfg.pareto.allow_two_edit_pairs = cfg.candidate_catalog.allow_two_edit_pairs
    if "allow_archive_replay" in (raw.get("candidate_catalog") or {}):
        cfg.pareto.allow_archive_replay = cfg.candidate_catalog.allow_archive_replay
    return cfg


def load_control_plane_config(path: str | Path) -> ControlPlaneConfig:
    from orchestra.config import _expand

    raw = _expand(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    return load_control_plane_mapping(raw)


def pricing_version(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"pricing registry missing: {path}")
    return _stable_hash(p.read_text(encoding="utf-8"))[:16]


def resolve_pareto_runtime(
    control: ControlPlaneConfig,
    *,
    repo_root: str | Path | None = None,
) -> ResolvedParetoRuntime:
    """Build concrete SlowLoopConfig / ParetoConfig / PreferenceProfile."""
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    prefs_path = root / control.pareto.preferences_path
    if not prefs_path.exists():
        # Allow relative-from-cwd as well.
        prefs_path = Path(control.pareto.preferences_path)
    profiles = load_preference_profiles(prefs_path)
    profile_id = (
        control.pareto.preference_profile
        or control.preference_profile
        or "balanced_knee"
    )
    if profile_id not in profiles:
        raise ValueError(
            f"missing preference profile {profile_id!r} in {prefs_path}; "
            f"known={sorted(profiles)}"
        )
    profile = profiles[profile_id]

    pricing_path = root / control.pareto.pricing_registry
    if not pricing_path.exists():
        pricing_path = Path(control.pareto.pricing_registry)
    pver = pricing_version(pricing_path)

    # Validate catalog graph paths exist when declared.
    for item in control.candidate_catalog.graph_templates:
        gpath = root / item.graph_path
        if not gpath.exists():
            gpath = Path(item.graph_path)
        if not gpath.exists():
            raise ValueError(
                f"candidate catalog graph path does not exist: {item.graph_path}"
            )
        # Reject path traversal / staging markers.
        normalized = str(gpath).replace("\\", "/")
        if "/.staging-" in normalized or normalized.startswith("../"):
            raise ValueError(f"unsafe graph template path: {item.graph_path}")

    slow = SlowLoopConfig(
        enabled=control.slow_loop.enabled,
        budget=SlowLoopBudget(
            max_updates_per_task=control.slow_loop.max_updates_per_task,
            max_candidates_per_update=control.slow_loop.max_candidates_per_update,
            max_wall_time_seconds=control.slow_loop.max_wall_time_seconds,
            min_commits_between_updates=control.slow_loop.every_n_committed_subtasks,
            failure_policy=control.slow_loop.failure_policy,
            context_pressure_ratio=control.slow_loop.context_pressure_ratio,
            budget_pressure_ratio=control.slow_loop.budget_pressure_ratio,
            repeated_failure_threshold=control.slow_loop.repeated_failure_threshold,
        ),
        allowed_backend_assignments=dict(control.slow_loop.allowed_backend_assignments),
        backend_model_pools=dict(control.slow_loop.backend_model_pools),
    )
    # Merge catalog backend allowlists into Slow Loop if provided only there.
    if not slow.allowed_backend_assignments and control.slow_loop.allowed_backend_assignments:
        pass

    pareto = ParetoConfig(
        enabled=control.pareto.enabled,
        max_candidates=control.pareto.max_candidates,
        max_estimated_archive_size=control.pareto.max_estimated_archive_size,
        max_realized_archive_size=control.pareto.max_realized_archive_size,
        allow_two_edit_pairs=(
            control.pareto.allow_two_edit_pairs
            and control.candidate_catalog.allow_two_edit_pairs
        ),
        allow_archive_replay=(
            control.pareto.allow_archive_replay
            and control.candidate_catalog.allow_archive_replay
        ),
        scalarize_without_pareto_filter=control.pareto.scalarize_without_pareto_filter,
        fallback_to_rule_based=control.pareto.fallback_to_rule_based,
        horizon_commits=control.pareto.horizon_commits,
        epsilon=dict(control.pareto.epsilon),
        objectives=dict(control.pareto.objectives),
    )
    # Stash generation flags on catalog for the generator.
    catalog = control.candidate_catalog.model_copy(
        update={
            "allow_archive_replay": control.pareto.allow_archive_replay
            and control.candidate_catalog.allow_archive_replay,
            "allow_two_edit_pairs": pareto.allow_two_edit_pairs,
        }
    )

    objective_hash = _stable_hash(
        {k: v.value for k, v in sorted(pareto.objectives.items())}
    )[:16]
    preference_hash = _stable_hash(profile.model_dump(mode="json"))[:16]
    control_hash = _stable_hash(
        {
            "slow_loop": control.slow_loop.model_dump(mode="json"),
            "pareto": control.pareto.model_dump(mode="json"),
            "catalog": catalog.model_dump(mode="json"),
            "preference_profile": profile_id,
        }
    )[:16]
    return ResolvedParetoRuntime(
        slow_loop_config=slow,
        pareto_config=pareto,
        preference_profile=profile,
        candidate_catalog=catalog,
        pricing_registry=str(pricing_path),
        preferences_path=str(prefs_path),
        preference_hash=preference_hash,
        objective_hash=objective_hash,
        pricing_version=pver,
        control_plane_hash=control_hash,
    )
