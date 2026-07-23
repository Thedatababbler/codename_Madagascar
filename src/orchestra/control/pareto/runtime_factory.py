"""Shared construction of Pareto / Slow Loop runtime from typed config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestra.control.pareto.catalog import SafeCandidateCatalog
from orchestra.control.pareto.controller import ParetoGlobalCandidatePolicy
from orchestra.control.pareto.schemas import ParetoConfig
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import SlowLoopConfig
from orchestra.experiments.control_plane import (
    ControlPlaneConfig,
    ResolvedParetoRuntime,
    load_control_plane_mapping,
    resolve_pareto_runtime,
)
from orchestra.runtime.task_checkpoint import TaskCheckpointStore


def resolve_from_mapping(
    raw: dict[str, Any], *, repo_root: str | Path | None = None
) -> ResolvedParetoRuntime:
    control = load_control_plane_mapping(raw)
    return resolve_pareto_runtime(control, repo_root=repo_root)


def resolve_from_path(
    path: str | Path, *, repo_root: str | Path | None = None
) -> ResolvedParetoRuntime:
    from orchestra.config import load_experiment_raw

    return resolve_from_mapping(load_experiment_raw(path), repo_root=repo_root)


def build_pareto_policy(
    resolved: ResolvedParetoRuntime,
    *,
    run_dir: str | Path,
    contracts_dir: str = "configs/contracts",
    repo_root: str | Path | None = None,
) -> ParetoGlobalCandidatePolicy:
    if not resolved.pareto_config.enabled:
        raise RuntimeError("Pareto requested but pareto.enabled is false")
    catalog = SafeCandidateCatalog(
        resolved.candidate_catalog,
        contracts_dir=contracts_dir,
        repo_root=repo_root,
    )
    return ParetoGlobalCandidatePolicy(
        config=resolved.pareto_config,
        preference_profile=resolved.preference_profile,
        run_dir=str(run_dir),
        catalog=catalog,
    )


def build_slow_loop_controller(
    resolved: ResolvedParetoRuntime,
    *,
    run_dir: str | Path,
    contracts_dir: str = "configs/contracts",
    repo_root: str | Path | None = None,
    checkpoint_store: TaskCheckpointStore | None = None,
    fail_closed: bool = True,
) -> SlowLoopController:
    """Construct SlowLoopController; attach Pareto policy when enabled."""
    policy = None
    if resolved.pareto_config.enabled:
        try:
            policy = build_pareto_policy(
                resolved,
                run_dir=run_dir,
                contracts_dir=contracts_dir,
                repo_root=repo_root,
            )
        except Exception as exc:
            if fail_closed:
                raise RuntimeError(
                    f"Pareto requested but runtime components failed to initialize: {exc}"
                ) from exc
            raise
    return SlowLoopController(
        config=resolved.slow_loop_config,
        candidate_policy=policy,
        checkpoint_store=checkpoint_store or TaskCheckpointStore(run_dir),
    )


def control_plane_manifest_fields(resolved: ResolvedParetoRuntime) -> dict[str, Any]:
    """Fields recorded in run_manifest for auditable M6 runs."""
    return {
        "pareto_config": resolved.pareto_config.model_dump(mode="json"),
        "slow_loop_config": resolved.slow_loop_config.model_dump(mode="json"),
        "preference_profile_id": resolved.preference_profile.profile_id,
        "preference_hash": resolved.preference_hash,
        "objective_hash": resolved.objective_hash,
        "pricing_version": resolved.pricing_version,
        "pricing_registry": resolved.pricing_registry,
        "control_plane_hash": resolved.control_plane_hash,
        "candidate_catalog": resolved.candidate_catalog.model_dump(mode="json"),
    }


def reported_pareto_config(control: ControlPlaneConfig) -> ParetoConfig:
    """Exact ParetoConfig that production YAML constructs (for tests)."""
    return resolve_pareto_runtime(control).pareto_config


def default_slow_loop_config() -> SlowLoopConfig:
    return SlowLoopConfig(enabled=False)
