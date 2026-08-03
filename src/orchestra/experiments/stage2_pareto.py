"""Stage-2 Pareto experiment configs, fixture runner, calibration, and reports."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ObjectiveDirection,
    ParetoEvaluationKind,
)
from orchestra.experiments.control_plane import (
    DEFAULT_OBJECTIVES,
    ControlPlaneConfig,
    load_control_plane_mapping,
    resolve_pareto_runtime,
)

Stage2Mode = Literal[
    "m5_rule_based",
    "m6_quality_first",
    "m6_cost_capped_quality",
    "m6_latency_capped_quality",
    "m6_robustness_first",
    "m6_balanced_knee",
    "m6_no_two_edit",
    "m6_no_archive_replay",
    "m6_scalarized_ablation",
]

MODE_ORDER: list[Stage2Mode] = [
    "m5_rule_based",
    "m6_quality_first",
    "m6_cost_capped_quality",
    "m6_latency_capped_quality",
    "m6_robustness_first",
    "m6_balanced_knee",
    "m6_no_two_edit",
    "m6_no_archive_replay",
    "m6_scalarized_ablation",
]

PROFILE_BY_MODE: dict[Stage2Mode, str | None] = {
    "m5_rule_based": None,
    "m6_quality_first": "quality_first",
    "m6_cost_capped_quality": "cost_capped_quality",
    "m6_latency_capped_quality": "latency_capped_quality",
    "m6_robustness_first": "robustness_first",
    "m6_balanced_knee": "balanced_knee",
    "m6_no_two_edit": "balanced_knee",
    "m6_no_archive_replay": "balanced_knee",
    "m6_scalarized_ablation": "balanced_knee",
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def shared_stage2_base(*, seed: int = 42) -> dict[str, Any]:
    """Shared task/model/sandbox settings for comparable Stage-2 modes."""
    return {
        "experiment": {
            "name": "stage2_shared",
            "seed": seed,
            "graph_config": "configs/graphs/codex_single_implementer.yaml",
            "plan_config": "configs/plans/stage2_pareto_three_subtasks.yaml",
            "contracts_dir": "configs/contracts",
            "output_root": "outputs/stage2_pareto",
            "source_repo": "tests/fixtures/codex_tiny_repo",
        },
        "benchmark": {
            "name": "livecodebench",
            "release_version": "release_v6",
            "scenario": "codegeneration",
            "language": "python",
            "manifest": "configs/manifests/lcb_smoke.json",
            "data_dir": "${LCB_DATA_DIR:-/root/data/livecodebench/code_generation_lite}",
            "repository_path": "${LCB_REPOSITORY_PATH:-/root/projects/LiveCodeBench}",
        },
        "runtime": {
            "backend": "native_async",
            "max_parallel_benchmark_tasks": 1,
            "max_parallel_nodes_per_task": 2,
            "max_parallel_llm_calls": 4,
            "max_parallel_sandboxes": 1,
            "max_concurrent_subtasks": 2,
            "checkpoint_after_each_wave": True,
        },
        "sandbox": {
            "backend": "mock",
            "per_test_timeout_seconds": 6,
            "worker_grace_seconds": 5,
            "max_worker_wall_seconds": 60,
            "num_process_evaluate": 1,
            "limits": {
                "memory_mb": 2048,
                "max_processes": 32,
                "max_open_files": 128,
                "max_file_size_mb": 16,
            },
        },
        "evaluation": {
            "max_repair_attempts": 0,
            "final_samples_per_task": 1,
            "total_task_budget_usd": 1.0,
            "timeout_seconds": 120,
        },
        "logging": {
            "save_prompts": True,
            "save_raw_responses": True,
            "save_artifacts": True,
            "redact_environment": True,
        },
        "candidate_catalog": {
            "graph_templates": [
                {
                    "template_id": "single_implementer",
                    "graph_path": "configs/graphs/codex_single_implementer.yaml",
                    "target_roles": ["s2", "s3", "s4"],
                    # Evidence present → may enter complete frontier.
                    "declared_cost_usd": 0.02,
                    "declared_latency_seconds": 0.8,
                }
            ],
            "concurrency_alternatives": [1, 2],
            # Serialization of the fork wave is a catalog alternative (not initial policy).
            "serialization_groups": [["s2", "s3"]],
            "context_budget_alternatives": {
                "s2": [512, 1024],
                "s3": [512, 1024],
                "s4": [512, 1024],
            },
            "allow_archive_replay": True,
            "allow_two_edit_pairs": True,
        },
    }


def build_mode_config(mode: Stage2Mode, *, seed: int = 42) -> dict[str, Any]:
    cfg = shared_stage2_base(seed=seed)
    cfg["experiment"]["name"] = f"stage2_{mode}"
    profile = PROFILE_BY_MODE[mode]
    if mode == "m5_rule_based":
        cfg["slow_loop"] = {
            "enabled": True,
            "every_n_committed_subtasks": 1,
            "context_pressure_ratio": 0.5,
            "max_updates_per_task": 4,
            "max_candidates_per_update": 8,
            "allowed_backend_assignments": {
                "coding": ["codex_sdk", "smolagents_code"],
                "s2": ["codex_sdk", "smolagents_code"],
                "s3": ["codex_sdk", "smolagents_code"],
            },
        }
        cfg["pareto"] = {"enabled": False}
    else:
        cfg["slow_loop"] = {
            "enabled": True,
            "every_n_committed_subtasks": 1,
            "context_pressure_ratio": 0.5,
            "max_updates_per_task": 4,
            "max_candidates_per_update": 8,
            "allowed_backend_assignments": {
                "coding": ["codex_sdk", "smolagents_code"],
                "s2": ["codex_sdk", "smolagents_code"],
                "s3": ["codex_sdk", "smolagents_code"],
            },
        }
        cfg["pareto"] = {
            "enabled": True,
            "max_candidates": 8,
            "horizon_commits": 1,
            "fallback_to_rule_based": False,
            "max_estimated_archive_size": 64,
            "max_realized_archive_size": 64,
            "allow_two_edit_pairs": mode != "m6_no_two_edit",
            "allow_archive_replay": mode != "m6_no_archive_replay",
            "scalarize_without_pareto_filter": mode == "m6_scalarized_ablation",
            "preference_profile": profile,
            "objectives": {k: v.value for k, v in DEFAULT_OBJECTIVES.items()},
            "pricing_registry": "configs/pricing/backend_models.yaml",
            "preferences_path": "configs/pareto/default_preferences.yaml",
        }
        cfg["preference_profile"] = profile
        if mode == "m6_no_two_edit":
            cfg["candidate_catalog"]["allow_two_edit_pairs"] = False
        if mode == "m6_no_archive_replay":
            cfg["candidate_catalog"]["allow_archive_replay"] = False
    cfg["stage2"] = {
        "enabled": True,
        "output_root": "outputs/stage2_pareto",
        "mode": mode,
        "seed": seed,
        "fixture_plan": "configs/plans/stage2_pareto_three_subtasks.yaml",
        "include_oracle_diagnostics": False,
    }
    return cfg


def write_all_stage2_configs(output_dir: str | Path | None = None) -> list[Path]:
    out = Path(output_dir or (_repo_root() / "configs/experiments/stage2"))
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for mode in MODE_ORDER:
        path = out / f"{mode}.yaml"
        path.write_text(
            yaml.safe_dump(build_mode_config(mode), sort_keys=False),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def validate_stage2_config(path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    control = load_control_plane_mapping(raw)
    resolved = resolve_pareto_runtime(control, repo_root=_repo_root())
    # Shared loader rejects unknown objectives / missing profiles already.
    return {
        "config_path": str(path),
        "ok": True,
        "pareto_enabled": resolved.pareto_config.enabled,
        "slow_loop_enabled": resolved.slow_loop_config.enabled,
        "preference_profile": resolved.preference_profile.profile_id,
        "preference_hash": resolved.preference_hash,
        "objective_hash": resolved.objective_hash,
        "pricing_version": resolved.pricing_version,
        "control_plane_hash": resolved.control_plane_hash,
        "pareto_config": resolved.pareto_config.model_dump(mode="json"),
    }


class CalibrationMismatchError(RuntimeError):
    """Typed fail-closed error for frozen calibration / held-out gate mismatches."""

    def __init__(
        self,
        field: str,
        *,
        expected: Any,
        observed: Any,
        detail: str | None = None,
    ) -> None:
        self.field = field
        self.expected = expected
        self.observed = observed
        self.detail = detail or (
            f"frozen calibration mismatch on {field}: "
            f"expected={expected!r} observed={observed!r}"
        )
        super().__init__(self.detail)


class CalibrationFreezeError(RuntimeError):
    """Typed fail-closed error for freeze-calibration refusals."""


@dataclass
class CalibrationArtifact:
    schema_version: str
    control_plane_hash: str
    preference_hash: str
    objective_hash: str
    pricing_version: str
    pricing_registry_hash: str
    candidate_catalog_hash: str
    graph_catalog_hash: str
    resolved_graph_hash: str
    preference_profile_id: str
    objectives: dict[str, str]
    objective_directions: dict[str, str]
    objective_required: dict[str, bool]
    backend_model_settings: dict[str, Any]
    backend_kinds: list[str]
    model_identifiers: dict[str, Any]
    seed_policy: dict[str, Any]
    git_sha: str
    config_hash: str
    selection_config_hash: str
    benchmark_manifest_hash: str
    dataset_identity: dict[str, Any]
    dataset_split_identity: dict[str, Any]
    private_data_policy: str
    public_evaluator_id: str
    public_evaluator_version: str
    preference_profile: dict[str, Any]
    source_run_id: str
    source_manifest_hash: str
    manifest_schema_version: str
    normalization: dict[str, dict[str, Any]]
    normalization_source_record_ids: dict[str, list[str]]
    reference_point: dict[str, float]
    created_at: str
    source_split: str = "development"
    # Backward-compatible alias used by older readers.
    split: str = "development"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "control_plane_hash": self.control_plane_hash,
            "preference_hash": self.preference_hash,
            "objective_hash": self.objective_hash,
            "pricing_version": self.pricing_version,
            "pricing_registry_hash": self.pricing_registry_hash,
            "candidate_catalog_hash": self.candidate_catalog_hash,
            "graph_catalog_hash": self.graph_catalog_hash,
            "resolved_graph_hash": self.resolved_graph_hash,
            "preference_profile_id": self.preference_profile_id,
            "preference_profile": self.preference_profile,
            "objectives": self.objectives,
            "objective_directions": self.objective_directions,
            "objective_required": self.objective_required,
            "backend_model_settings": self.backend_model_settings,
            "backend_kinds": self.backend_kinds,
            "model_identifiers": self.model_identifiers,
            "seed_policy": self.seed_policy,
            "git_sha": self.git_sha,
            "config_hash": self.config_hash,
            "selection_config_hash": self.selection_config_hash,
            "benchmark_manifest_hash": self.benchmark_manifest_hash,
            "dataset_identity": self.dataset_identity,
            "dataset_split_identity": self.dataset_split_identity,
            "private_data_policy": self.private_data_policy,
            "public_evaluator_id": self.public_evaluator_id,
            "public_evaluator_version": self.public_evaluator_version,
            "source_run_id": self.source_run_id,
            "source_manifest_hash": self.source_manifest_hash,
            "manifest_schema_version": self.manifest_schema_version,
            "normalization": self.normalization,
            "normalization_source_record_ids": self.normalization_source_record_ids,
            "reference_point": self.reference_point,
            "created_at": self.created_at,
            "source_split": self.source_split,
            "split": self.split,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CalibrationArtifact:
        required = (
            "control_plane_hash",
            "preference_hash",
            "objective_hash",
            "pricing_version",
        )
        missing = [k for k in required if k not in payload]
        if missing:
            raise RuntimeError(
                f"malformed calibration artifact: missing fields {missing}"
            )
        split = str(payload.get("source_split") or payload.get("split") or "")
        if not split:
            raise RuntimeError(
                "malformed calibration artifact: missing source_split"
            )
        objectives = {
            str(k): str(v) for k, v in dict(payload.get("objectives") or {}).items()
        }
        directions = {
            str(k): str(v)
            for k, v in dict(
                payload.get("objective_directions") or payload.get("objectives") or {}
            ).items()
        }
        required_map = {
            str(k): bool(v)
            for k, v in dict(payload.get("objective_required") or {}).items()
        }
        if not required_map and objectives:
            required_map = {k: True for k in objectives}
        return cls(
            schema_version=str(
                payload.get("schema_version")
                or payload.get("manifest_schema_version")
                or "stage2-calibration-v3"
            ),
            control_plane_hash=str(payload["control_plane_hash"]),
            preference_hash=str(payload["preference_hash"]),
            objective_hash=str(payload["objective_hash"]),
            pricing_version=str(payload["pricing_version"]),
            pricing_registry_hash=str(payload.get("pricing_registry_hash") or ""),
            candidate_catalog_hash=str(payload.get("candidate_catalog_hash") or ""),
            graph_catalog_hash=str(payload.get("graph_catalog_hash") or ""),
            resolved_graph_hash=str(payload.get("resolved_graph_hash") or ""),
            preference_profile_id=str(payload.get("preference_profile_id") or ""),
            preference_profile=dict(payload.get("preference_profile") or {}),
            objectives=objectives,
            objective_directions=directions,
            objective_required=required_map,
            backend_model_settings=dict(payload.get("backend_model_settings") or {}),
            backend_kinds=list(payload.get("backend_kinds") or []),
            model_identifiers=dict(payload.get("model_identifiers") or {}),
            seed_policy=dict(payload.get("seed_policy") or {}),
            git_sha=str(payload.get("git_sha") or ""),
            config_hash=str(payload.get("config_hash") or ""),
            selection_config_hash=str(payload.get("selection_config_hash") or ""),
            benchmark_manifest_hash=str(payload.get("benchmark_manifest_hash") or ""),
            dataset_identity=dict(payload.get("dataset_identity") or {}),
            dataset_split_identity=dict(payload.get("dataset_split_identity") or {}),
            private_data_policy=str(payload.get("private_data_policy") or ""),
            public_evaluator_id=str(payload.get("public_evaluator_id") or ""),
            public_evaluator_version=str(
                payload.get("public_evaluator_version") or ""
            ),
            source_run_id=str(payload.get("source_run_id") or ""),
            source_manifest_hash=str(payload.get("source_manifest_hash") or ""),
            manifest_schema_version=str(
                payload.get("manifest_schema_version") or "stage2-calibration-v3"
            ),
            normalization=dict(payload.get("normalization") or {}),
            normalization_source_record_ids=dict(
                payload.get("normalization_source_record_ids") or {}
            ),
            reference_point=dict(payload.get("reference_point") or {}),
            created_at=str(payload.get("created_at") or ""),
            source_split=split,
            split=split,
        )


def _stable_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _development_observation_rows(
    run_dir: Path,
) -> tuple[list[dict[str, float]], dict[str, list[str]]]:
    """Extract finite objective observations and provenance IDs from a run."""
    rec = collect_run_records(run_dir)
    rows: list[dict[str, float]] = []
    source_ids: dict[str, list[str]] = {}
    for d in rec["decisions"]:
        snap = d.get("selected_candidate_snapshot") or {}
        vals = (snap.get("objectives") or {}).get("values") or {}
        row: dict[str, float] = {}
        decision_id = str(d.get("decision_id") or "")
        for name in (
            "quality",
            "cost",
            "latency",
            "risk",
            "communication_overhead",
        ):
            number = _finite_float((vals.get(name) or {}).get("value"))
            available = (vals.get(name) or {}).get("available")
            if number is None or available is False:
                continue
            row[name] = number
            if decision_id:
                source_ids.setdefault(name, []).append(decision_id)
        if row:
            rows.append(row)
    for name, ids in list(source_ids.items()):
        source_ids[name] = sorted(set(ids))
    return rows, source_ids


def _build_normalization_from_evidence(
    *,
    objectives: dict[str, ObjectiveDirection],
    development_points: list[dict[str, float | None]],
    source_record_ids: dict[str, list[str]],
) -> tuple[dict[str, dict[str, Any]], dict[str, float], dict[str, list[str]]]:
    """Derive normalization only from persisted development evidence.

    Constant-objective policy: when all finite observations for a required
    objective share the same value (min == max), freeze ``min == max == value``
    and mark ``constant_objective=true``. Do not invent a wider range.
    """
    norms: dict[str, dict[str, Any]] = {}
    reference: dict[str, float] = {}
    provenance: dict[str, list[str]] = {}
    missing: list[str] = []
    for name, direction in objectives.items():
        values: list[float] = []
        for point in development_points:
            number = _finite_float(point.get(name))
            if number is not None:
                values.append(number)
        ids = list(source_record_ids.get(name) or [])
        if not values:
            missing.append(name)
            continue
        if not ids:
            raise CalibrationFreezeError(
                f"calibration freeze refused: missing normalization provenance "
                f"for required objective {name!r}"
            )
        lo, hi = min(values), max(values)
        entry: dict[str, Any] = {
            "min": lo,
            "max": hi,
            "source_record_ids": sorted(set(ids)),
            "observation_count": len(values),
        }
        if math.isclose(lo, hi):
            entry["constant_objective"] = True
            entry["constant_objective_policy"] = (
                "min_equals_max_from_development_evidence"
            )
        else:
            entry["constant_objective"] = False
        norms[name] = entry
        reference[name] = hi if direction is ObjectiveDirection.MINIMIZE else lo
        provenance[name] = sorted(set(ids))
    if missing:
        raise CalibrationFreezeError(
            "calibration freeze refused: missing development evidence for "
            f"required objectives {missing}"
        )
    return norms, reference, provenance


def _dataset_split_policy(dataset_identity: dict[str, Any]) -> dict[str, Any]:
    """Shared split-policy identity (excludes run_id / development|heldout labels)."""
    return {
        "policy": "stage2_explicit_split",
        "dataset_identity_keys": sorted(str(k) for k in (dataset_identity or {})),
    }


CANONICAL_SELECTION_PROJECTION_KEYS: tuple[str, ...] = (
    "schema_version",
    "git_sha",
    "control_plane_hash",
    "preference_hash",
    "objective_hash",
    "objectives",
    "objective_directions",
    "objective_required",
    "preference_profile_id",
    "preference_profile",
    "pricing_version",
    "pricing_registry_hash",
    "candidate_catalog_hash",
    "graph_catalog_hash",
    "resolved_graph_hash",
    "public_evaluator_id",
    "public_evaluator_version",
    "backend_kinds",
    "model_identifiers",
    "backend_model_settings",
    "seed_policy",
    "benchmark_manifest_hash",
    "dataset_identity",
    "dataset_split_policy",
    "private_data_policy",
)


def canonical_selection_projection(identity: dict[str, Any]) -> dict[str, Any]:
    """Project selection-relevant configuration only (no run/source provenance)."""
    projected: dict[str, Any] = {}
    for key in CANONICAL_SELECTION_PROJECTION_KEYS:
        if key == "dataset_split_policy":
            if "dataset_split_policy" in identity and identity.get("dataset_split_policy"):
                projected[key] = dict(identity.get("dataset_split_policy") or {})
            else:
                projected[key] = _dataset_split_policy(
                    dict(identity.get("dataset_identity") or {})
                )
            continue
        if key not in identity:
            continue
        value = identity.get(key)
        if isinstance(value, dict):
            projected[key] = dict(value)
        elif isinstance(value, list):
            projected[key] = list(value)
        else:
            projected[key] = value
    return projected


def compute_selection_config_hash(identity: dict[str, Any]) -> str:
    """Deterministic hash of the canonical selection projection."""
    return _stable_hash(canonical_selection_projection(identity))


def _selection_identity_from_resolved(
    resolved: Any,
    *,
    seed: int,
    git_sha: str,
    config_hash: str,
    benchmark_manifest_hash: str,
    source_split: str,
    source_run_id: str,
    source_manifest_hash: str,
    private_data_policy: str,
    public_evaluator_id: str,
    public_evaluator_version: str,
    dataset_identity: dict[str, Any],
    dataset_split_identity: dict[str, Any],
) -> dict[str, Any]:
    catalog_dump = resolved.candidate_catalog.model_dump(mode="json")
    graph_paths = sorted(
        {
            t.get("graph_path")
            for t in catalog_dump.get("graph_templates") or []
            if isinstance(t, dict) and t.get("graph_path")
        }
    )
    graph_hashes = []
    for gpath in graph_paths:
        p = Path(gpath)
        if not p.is_absolute():
            p = _repo_root() / p
        if p.exists():
            graph_hashes.append(f"{gpath}:{hashlib.sha256(p.read_bytes()).hexdigest()}")
    pricing_path = Path(resolved.pricing_registry)
    if not pricing_path.is_absolute():
        pricing_path = _repo_root() / pricing_path
    pricing_registry_hash = (
        hashlib.sha256(pricing_path.read_bytes()).hexdigest()
        if pricing_path.exists()
        else ""
    )
    backend_model_settings = {
        "allowed_backend_assignments": dict(
            resolved.slow_loop_config.allowed_backend_assignments or {}
        ),
        "backend_model_pools": dict(
            resolved.slow_loop_config.backend_model_pools or {}
        ),
        "pricing_registry": resolved.pricing_registry,
    }
    backend_kinds = sorted(
        (resolved.slow_loop_config.allowed_backend_assignments or {}).keys()
    )
    model_identifiers = dict(resolved.slow_loop_config.backend_model_pools or {})
    if not model_identifiers:
        from orchestra.control.backend_usage import load_pricing_registry

        priced = load_pricing_registry(str(pricing_path) if pricing_path.exists() else None)
        model_identifiers = {
            "pricing_version": priced.pricing_version,
            "registry_models": sorted(priced.models.keys()),
        }
    seed_policy = {"seed": seed, "deterministic_reports": True}
    required_objectives = {
        name: direction.value
        for name, direction in resolved.pareto_config.objectives.items()
    }
    objective_required = {name: True for name in required_objectives}
    preference_profile = resolved.preference_profile.model_dump(mode="json")
    dataset_identity = dict(dataset_identity or {})
    # Canonical selection projection — excludes run/source provenance.
    selection_payload = {
        "schema_version": "stage2-calibration-v3",
        "control_plane_hash": resolved.control_plane_hash,
        "preference_hash": resolved.preference_hash,
        "objective_hash": resolved.objective_hash,
        "pricing_version": resolved.pricing_version,
        "pricing_registry_hash": pricing_registry_hash,
        "candidate_catalog_hash": _stable_hash(catalog_dump),
        "graph_catalog_hash": _stable_hash(graph_hashes),
        "resolved_graph_hash": _stable_hash(graph_hashes),
        "preference_profile_id": resolved.preference_profile.profile_id,
        "preference_profile": preference_profile,
        "objectives": required_objectives,
        "objective_directions": required_objectives,
        "objective_required": objective_required,
        "backend_model_settings": backend_model_settings,
        "backend_kinds": backend_kinds,
        "model_identifiers": model_identifiers,
        "seed_policy": seed_policy,
        "git_sha": git_sha,
        "benchmark_manifest_hash": benchmark_manifest_hash,
        "dataset_identity": dataset_identity,
        "dataset_split_policy": _dataset_split_policy(dataset_identity),
        "private_data_policy": private_data_policy,
        "public_evaluator_id": public_evaluator_id,
        "public_evaluator_version": public_evaluator_version,
    }
    selection_config_hash = compute_selection_config_hash(selection_payload)
    # Provenance and run-scoped fields are persisted separately.
    selection_payload.update(
        {
            "selection_config_hash": selection_config_hash,
            "config_hash": config_hash,
            "dataset_split_identity": dict(dataset_split_identity or {}),
            "source_split": source_split,
            "source_run_id": source_run_id,
            "source_manifest_hash": source_manifest_hash,
        }
    )
    return selection_payload



def write_calibration_artifact(
    path: str | Path,
    *,
    control: ControlPlaneConfig,
    development_points: list[dict[str, float | None]],
    run_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    source_record_ids: dict[str, list[str]] | None = None,
) -> CalibrationArtifact:
    """Freeze calibration from a persisted development run only.

    The run manifest split is authoritative: fixture/heldout sources are refused,
    and ``source_split`` is copied (never rewritten). Normalization is derived
    only from finite development observations with provenance IDs — never from
    fabricated defaults.
    """
    from orchestra.experiments.metadata import git_commit_hash

    if run_dir is None:
        raise CalibrationFreezeError(
            "freeze-calibration requires --run-dir with a persisted development run"
        )
    run_path = Path(run_dir)
    manifest_path = run_path / "run_manifest.json"
    if not manifest_path.exists():
        raise CalibrationFreezeError(
            f"freeze-calibration requires run_manifest.json under {run_path}"
        )
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    source_split = str(manifest.get("split") or "")
    if source_split != "development":
        raise CalibrationFreezeError(
            "freeze-calibration may consume development runs only; "
            f"refusing split={source_split!r} under {run_path}. "
            "A CLI command must never relabel a run's persisted split."
        )
    source_run_id = str(manifest.get("run_id") or run_path.name)
    source_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    resolved = resolve_pareto_runtime(control, repo_root=_repo_root())
    provenance = dict(source_record_ids or {})
    norms, reference, provenance = _build_normalization_from_evidence(
        objectives=dict(resolved.pareto_config.objectives),
        development_points=development_points,
        source_record_ids=provenance,
    )

    seed = 42
    config_hash = str(manifest.get("control_plane_hash") or "")
    benchmark_manifest_hash = str(manifest.get("manifest_hash") or "")
    if config_path is not None:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        seed = int((raw.get("experiment") or {}).get("seed") or 42)
        config_hash = config_hash or hashlib.sha256(
            Path(config_path).read_bytes()
        ).hexdigest()
        bench_manifest = (raw.get("benchmark") or {}).get("manifest")
        if bench_manifest:
            bm = Path(bench_manifest)
            if not bm.is_absolute():
                bm = _repo_root() / bm
            if bm.exists():
                benchmark_manifest_hash = hashlib.sha256(bm.read_bytes()).hexdigest()
    seed = int(
        ((manifest.get("experiment") or {}).get("seed"))
        or ((manifest.get("stage2") or {}).get("seed"))
        or seed
    )
    private_data_policy = str(
        manifest.get("private_data_policy") or "private_labels_offline_only"
    )
    public_evaluator_id = str(manifest.get("public_evaluator_id") or "public_harness")
    public_evaluator_version = str(
        manifest.get("public_evaluator_version") or "public-harness-v1"
    )
    dataset_identity = dict(manifest.get("dataset_identity") or {})
    if not dataset_identity:
        dataset_identity = {"benchmark": "livecodebench"}
    dataset_split_identity = dict(
        manifest.get("dataset_split_identity")
        or {
            "source_split": source_split,
            "source_run_id": source_run_id,
            "source_manifest_hash": source_manifest_hash,
        }
    )
    git_sha = manifest_git_sha(manifest) or (
        git_commit_hash(_repo_root()) or ""
    )
    identity = _selection_identity_from_resolved(
        resolved,
        seed=seed,
        git_sha=git_sha,
        config_hash=config_hash,
        benchmark_manifest_hash=benchmark_manifest_hash,
        source_split=source_split,
        source_run_id=source_run_id,
        source_manifest_hash=source_manifest_hash,
        private_data_policy=private_data_policy,
        public_evaluator_id=public_evaluator_id,
        public_evaluator_version=public_evaluator_version,
        dataset_identity=dataset_identity,
        dataset_split_identity=dataset_split_identity,
    )
    # Deterministic created_at from source evidence (never wall-clock now).
    created_at = str(
        manifest.get("started_at")
        or manifest.get("created_at")
        or source_manifest_hash[:16]
    )
    artifact = CalibrationArtifact(
        schema_version=str(identity["schema_version"]),
        control_plane_hash=str(identity["control_plane_hash"]),
        preference_hash=str(identity["preference_hash"]),
        objective_hash=str(identity["objective_hash"]),
        pricing_version=str(identity["pricing_version"]),
        pricing_registry_hash=str(identity["pricing_registry_hash"]),
        candidate_catalog_hash=str(identity["candidate_catalog_hash"]),
        graph_catalog_hash=str(identity["graph_catalog_hash"]),
        resolved_graph_hash=str(identity["resolved_graph_hash"]),
        preference_profile_id=str(identity["preference_profile_id"]),
        preference_profile=dict(identity["preference_profile"]),
        objectives=dict(identity["objectives"]),
        objective_directions=dict(identity["objective_directions"]),
        objective_required=dict(identity["objective_required"]),
        backend_model_settings=dict(identity["backend_model_settings"]),
        backend_kinds=list(identity["backend_kinds"]),
        model_identifiers=dict(identity["model_identifiers"]),
        seed_policy=dict(identity["seed_policy"]),
        git_sha=git_sha,
        config_hash=config_hash,
        selection_config_hash=str(identity["selection_config_hash"]),
        benchmark_manifest_hash=benchmark_manifest_hash,
        dataset_identity=dataset_identity,
        dataset_split_identity=dataset_split_identity,
        private_data_policy=private_data_policy,
        public_evaluator_id=public_evaluator_id,
        public_evaluator_version=public_evaluator_version,
        source_run_id=source_run_id,
        source_manifest_hash=source_manifest_hash,
        manifest_schema_version="stage2-calibration-v3",
        normalization=norms,
        normalization_source_record_ids=provenance,
        reference_point=reference,
        created_at=created_at,
        source_split=source_split,
        split=source_split,
    )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(artifact.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact


def _expect_equal(field: str, expected: Any, observed: Any) -> None:
    if expected != observed:
        raise CalibrationMismatchError(
            field,
            expected=expected,
            observed=observed,
        )


def _valid_git_hex(value: str) -> bool:
    """Accept full or abbreviated lowercase/uppercase hex SHAs."""
    if not value or not isinstance(value, str):
        return False
    if len(value) < 7 or len(value) > 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def manifest_git_sha(manifest: dict[str, Any]) -> str:
    """Canonical Git identity reader.

    ``git_sha`` is canonical. Legacy ``git_commit`` is accepted only when
    ``git_sha`` is absent. If both exist and differ, fail closed.
    Null or malformed values fail closed (no silent preference).
    """
    has_sha = "git_sha" in manifest
    has_commit = "git_commit" in manifest
    sha_raw = manifest.get("git_sha") if has_sha else None
    commit_raw = manifest.get("git_commit") if has_commit else None

    if has_sha and sha_raw is None:
        raise CalibrationMismatchError(
            "git_sha",
            expected="non-null git identity",
            observed=None,
        )
    if has_commit and commit_raw is None:
        raise CalibrationMismatchError(
            "git_commit",
            expected="non-null git identity",
            observed=None,
        )

    sha = None if sha_raw is None else str(sha_raw)
    commit = None if commit_raw is None else str(commit_raw)

    if sha is not None and not _valid_git_hex(sha):
        raise CalibrationMismatchError(
            "git_sha",
            expected="hex git object id",
            observed=sha,
        )
    if commit is not None and not _valid_git_hex(commit):
        raise CalibrationMismatchError(
            "git_commit",
            expected="hex git object id",
            observed=commit,
        )

    if sha is not None and commit is not None:
        if sha != commit:
            raise CalibrationMismatchError(
                "git_identity_alias_conflict",
                expected=sha,
                observed=commit,
            )
        return sha
    if sha is not None:
        return sha
    if commit is not None:
        return commit
    return ""


HELDOUT_REQUIRED_IDENTITY_FIELDS: tuple[str, ...] = (
    "schema_version",
    "git_sha",
    "selection_config_hash",
    "control_plane_hash",
    "preference_hash",
    "objective_hash",
    "objectives",
    "objective_directions",
    "objective_required",
    "preference_profile_id",
    "preference_profile",
    "pricing_version",
    "pricing_registry_hash",
    "candidate_catalog_hash",
    "graph_catalog_hash",
    "resolved_graph_hash",
    "public_evaluator_id",
    "public_evaluator_version",
    "backend_kinds",
    "model_identifiers",
    "backend_model_settings",
    "seed_policy",
    "benchmark_manifest_hash",
    "dataset_identity",
    "dataset_split_identity",
    "private_data_policy",
)


def selection_identity_from_manifest(
    manifest: dict[str, Any],
    *,
    role: str,
) -> dict[str, Any]:
    """Project a persisted run manifest into the canonical selection identity.

    Fail closed when required fields are missing/null/empty. Accepts legacy
    ``git_commit`` by normalizing it to ``git_sha`` at this boundary.
    Held-out targets must carry the canonical keys themselves (no silent
    fallback that would skip a deleted required field).
    """
    strict = role == "heldout_target"
    if strict:
        for field in HELDOUT_REQUIRED_IDENTITY_FIELDS:
            if field == "git_sha":
                if manifest.get("git_sha") is None and manifest.get("git_commit") is None:
                    raise CalibrationMismatchError(
                        "git_sha",
                        expected="present git_sha or git_commit",
                        observed=None,
                    )
                continue
            if field not in manifest or manifest.get(field) is None:
                raise CalibrationMismatchError(
                    field,
                    expected="present non-null selection-identity field",
                    observed=manifest.get(field) if field in manifest else "<missing>",
                )
    git_sha = manifest_git_sha(manifest)
    if strict:
        schema_version = str(manifest.get("schema_version") or "")
        control_plane_hash = str(manifest.get("control_plane_hash") or "")
        objective_directions = dict(manifest.get("objective_directions") or {})
        preference_profile_id = str(manifest.get("preference_profile_id") or "")
        resolved_graph_hash = str(manifest.get("resolved_graph_hash") or "")
        benchmark_manifest_hash = str(manifest.get("benchmark_manifest_hash") or "")
    else:
        schema_version = str(
            manifest.get("schema_version")
            or manifest.get("selection_schema_version")
            or ""
        )
        control_plane_hash = str(
            manifest.get("control_plane_hash") or manifest.get("config_hash") or ""
        )
        objective_directions = dict(
            manifest.get("objective_directions") or manifest.get("objectives") or {}
        )
        preference_profile_id = str(
            manifest.get("preference_profile_id")
            or (manifest.get("pareto_config") or {}).get("preference_profile")
            or ""
        )
        resolved_graph_hash = str(
            manifest.get("resolved_graph_hash")
            or manifest.get("graph_catalog_hash")
            or ""
        )
        benchmark_manifest_hash = str(
            manifest.get("benchmark_manifest_hash")
            or manifest.get("manifest_hash")
            or ""
        )
    projected = {
        "schema_version": schema_version,
        "git_sha": git_sha,
        "selection_config_hash": str(manifest.get("selection_config_hash") or ""),
        "control_plane_hash": control_plane_hash,
        "preference_hash": str(manifest.get("preference_hash") or ""),
        "objective_hash": str(manifest.get("objective_hash") or ""),
        "objectives": dict(manifest.get("objectives") or {}),
        "objective_directions": objective_directions,
        "objective_required": dict(manifest.get("objective_required") or {}),
        "preference_profile_id": preference_profile_id,
        "preference_profile": dict(manifest.get("preference_profile") or {}),
        "pricing_version": str(manifest.get("pricing_version") or ""),
        "pricing_registry_hash": str(manifest.get("pricing_registry_hash") or ""),
        "candidate_catalog_hash": str(manifest.get("candidate_catalog_hash") or ""),
        "graph_catalog_hash": str(manifest.get("graph_catalog_hash") or ""),
        "resolved_graph_hash": resolved_graph_hash,
        "public_evaluator_id": str(manifest.get("public_evaluator_id") or ""),
        "public_evaluator_version": str(manifest.get("public_evaluator_version") or ""),
        "backend_kinds": list(manifest.get("backend_kinds") or []),
        "model_identifiers": dict(manifest.get("model_identifiers") or {}),
        "backend_model_settings": dict(manifest.get("backend_model_settings") or {}),
        "seed_policy": dict(manifest.get("seed_policy") or {}),
        "benchmark_manifest_hash": benchmark_manifest_hash,
        "dataset_identity": dict(manifest.get("dataset_identity") or {}),
        "dataset_split_identity": dict(manifest.get("dataset_split_identity") or {}),
        "dataset_split_policy": (
            dict(manifest.get("dataset_split_policy") or {})
            if manifest.get("dataset_split_policy")
            else _dataset_split_policy(dict(manifest.get("dataset_identity") or {}))
        ),
        "private_data_policy": str(manifest.get("private_data_policy") or ""),
        "reference_point": dict(manifest.get("reference_point") or {}),
    }
    for field in HELDOUT_REQUIRED_IDENTITY_FIELDS:
        value = projected.get(field)
        if value in (None, "", {}, []):
            raise CalibrationMismatchError(
                field,
                expected="non-empty selection-identity value",
                observed=value,
            )
    return projected


def build_run_selection_identity(
    *,
    control: ControlPlaneConfig,
    split: str,
    run_id: str,
    seed: int,
    git_sha: str,
    benchmark_manifest_hash: str,
    dataset_identity: dict[str, Any] | None = None,
    dataset_split_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical selection-identity block for persistence."""
    resolved = resolve_pareto_runtime(control, repo_root=_repo_root())
    identity = _selection_identity_from_resolved(
        resolved,
        seed=seed,
        git_sha=git_sha,
        config_hash=resolved.control_plane_hash,
        benchmark_manifest_hash=benchmark_manifest_hash,
        source_split=split,
        source_run_id=run_id,
        source_manifest_hash="",
        private_data_policy="private_labels_offline_only",
        public_evaluator_id="public_harness",
        public_evaluator_version="public-harness-v1",
        dataset_identity=dict(dataset_identity or {"benchmark": "livecodebench"}),
        dataset_split_identity=dict(
            dataset_split_identity
            or {"split": split, "run_id": run_id}
        ),
    )
    # Canonical Git field only; legacy git_commit is accepted at the reader.
    return identity


def assert_calibration_matches(
    artifact: CalibrationArtifact,
    control: ControlPlaneConfig,
    *,
    require_held_out_split: bool = False,
    run_manifest: dict[str, Any] | None = None,
) -> None:
    """Compare every selection-relevant frozen field by value (fail closed)."""
    resolved = resolve_pareto_runtime(control, repo_root=_repo_root())
    if artifact.source_split != "development":
        raise CalibrationMismatchError(
            "source_split",
            expected="development",
            observed=artifact.source_split,
        )
    if require_held_out_split:
        if run_manifest is None:
            raise CalibrationMismatchError(
                "run_manifest",
                expected="heldout run_manifest",
                observed=None,
            )
        run_split = str(run_manifest.get("split") or "")
        if run_split != "heldout":
            raise CalibrationMismatchError(
                "target_split",
                expected="heldout",
                observed=run_split,
            )

    seed = 42
    if run_manifest is not None:
        seed = int(
            ((run_manifest.get("experiment") or {}).get("seed"))
            or ((run_manifest.get("stage2") or {}).get("seed"))
            or seed
        )
    expected_identity = _selection_identity_from_resolved(
        resolved,
        seed=seed if run_manifest is not None else int(
            (artifact.seed_policy or {}).get("seed") or 42
        ),
        git_sha=artifact.git_sha,
        config_hash=artifact.config_hash,
        benchmark_manifest_hash=artifact.benchmark_manifest_hash,
        source_split=artifact.source_split,
        source_run_id=artifact.source_run_id,
        source_manifest_hash=artifact.source_manifest_hash,
        private_data_policy=artifact.private_data_policy,
        public_evaluator_id=artifact.public_evaluator_id,
        public_evaluator_version=artifact.public_evaluator_version,
        dataset_identity=dict(artifact.dataset_identity or {}),
        dataset_split_identity=dict(artifact.dataset_split_identity or {}),
    )
    # Always recompute selection-relevant hashes from current control plane.
    catalog_hash = _stable_hash(resolved.candidate_catalog.model_dump(mode="json"))
    pricing_path = Path(resolved.pricing_registry)
    if not pricing_path.is_absolute():
        pricing_path = _repo_root() / pricing_path
    pricing_hash = (
        hashlib.sha256(pricing_path.read_bytes()).hexdigest()
        if pricing_path.exists()
        else ""
    )
    expected_objectives = {
        name: direction.value
        for name, direction in resolved.pareto_config.objectives.items()
    }
    _expect_equal(
        "schema_version",
        "stage2-calibration-v3",
        artifact.schema_version,
    )
    _expect_equal(
        "control_plane_hash",
        resolved.control_plane_hash,
        artifact.control_plane_hash,
    )
    _expect_equal(
        "preference_hash", resolved.preference_hash, artifact.preference_hash
    )
    _expect_equal(
        "objective_hash", resolved.objective_hash, artifact.objective_hash
    )
    _expect_equal(
        "pricing_version", resolved.pricing_version, artifact.pricing_version
    )
    _expect_equal(
        "pricing_registry_hash", pricing_hash, artifact.pricing_registry_hash
    )
    _expect_equal(
        "candidate_catalog_hash", catalog_hash, artifact.candidate_catalog_hash
    )
    _expect_equal(
        "preference_profile_id",
        resolved.preference_profile.profile_id,
        artifact.preference_profile_id,
    )
    _expect_equal(
        "preference_profile",
        expected_identity["preference_profile"],
        artifact.preference_profile,
    )
    _expect_equal("objectives", expected_objectives, artifact.objectives)
    _expect_equal(
        "objective_directions",
        expected_objectives,
        artifact.objective_directions or artifact.objectives,
    )
    _expect_equal(
        "objective_required",
        {name: True for name in expected_objectives},
        artifact.objective_required or {name: True for name in artifact.objectives},
    )
    _expect_equal(
        "backend_model_settings",
        expected_identity["backend_model_settings"],
        artifact.backend_model_settings,
    )
    _expect_equal(
        "backend_kinds", expected_identity["backend_kinds"], artifact.backend_kinds
    )
    _expect_equal(
        "model_identifiers",
        expected_identity["model_identifiers"],
        artifact.model_identifiers,
    )
    _expect_equal(
        "seed_policy", expected_identity["seed_policy"], artifact.seed_policy
    )
    _expect_equal(
        "selection_config_hash",
        expected_identity["selection_config_hash"],
        artifact.selection_config_hash,
    )
    _expect_equal(
        "graph_catalog_hash",
        expected_identity["graph_catalog_hash"],
        artifact.graph_catalog_hash,
    )
    _expect_equal(
        "resolved_graph_hash",
        expected_identity["resolved_graph_hash"],
        artifact.resolved_graph_hash,
    )
    if not artifact.git_sha:
        raise CalibrationMismatchError(
            "git_sha", expected="non-empty", observed=artifact.git_sha
        )
    if not artifact.config_hash and not artifact.selection_config_hash:
        raise CalibrationMismatchError(
            "config_hash",
            expected="non-empty config_hash or selection_config_hash",
            observed=artifact.config_hash,
        )
    if not artifact.source_run_id:
        raise CalibrationMismatchError(
            "source_run_id", expected="non-empty", observed=artifact.source_run_id
        )
    if not artifact.source_manifest_hash:
        raise CalibrationMismatchError(
            "source_manifest_hash",
            expected="non-empty",
            observed=artifact.source_manifest_hash,
        )
    _expect_equal(
        "private_data_policy",
        artifact.private_data_policy,
        artifact.private_data_policy,
    )
    if not artifact.private_data_policy:
        raise CalibrationMismatchError(
            "private_data_policy",
            expected="non-empty",
            observed=artifact.private_data_policy,
        )
    if not artifact.public_evaluator_id:
        raise CalibrationMismatchError(
            "public_evaluator_id",
            expected="non-empty",
            observed=artifact.public_evaluator_id,
        )
    if not artifact.public_evaluator_version:
        raise CalibrationMismatchError(
            "public_evaluator_version",
            expected="non-empty",
            observed=artifact.public_evaluator_version,
        )

    for name in expected_objectives:
        entry = (artifact.normalization or {}).get(name)
        if not entry:
            raise CalibrationMismatchError(
                f"normalization.{name}",
                expected="present development-derived range",
                observed=None,
            )
        lo = _finite_float(entry.get("min"))
        hi = _finite_float(entry.get("max"))
        if lo is None or hi is None:
            raise CalibrationMismatchError(
                f"normalization.{name}",
                expected="finite min/max",
                observed=entry,
            )
        if hi < lo:
            raise CalibrationMismatchError(
                f"normalization.{name}",
                expected="max >= min",
                observed=entry,
            )
        ids = list(
            entry.get("source_record_ids")
            or (artifact.normalization_source_record_ids or {}).get(name)
            or []
        )
        if not ids:
            raise CalibrationMismatchError(
                f"normalization_source_record_ids.{name}",
                expected="non-empty provenance ids",
                observed=ids,
            )
        # Reject fabricated fixture/default ranges with no provenance chain.
        if entry.get("fabricated") or entry.get("fallback_default"):
            raise CalibrationMismatchError(
                f"normalization.{name}",
                expected="development-derived",
                observed=entry,
            )

    if run_manifest is not None and require_held_out_split:
        # Fail-closed: every required selection-identity field must be present
        # on the target and match the frozen calibration artifact.
        target = selection_identity_from_manifest(run_manifest, role="heldout_target")
        _expect_equal("target_split", "heldout", str(run_manifest.get("split") or ""))
        declared_hash = str(target.get("selection_config_hash") or "")
        if not declared_hash:
            raise CalibrationMismatchError(
                "selection_config_hash",
                expected="non-empty recomputable hash",
                observed=declared_hash,
            )
        # Target may omit dataset_split_policy; derive from dataset_identity.
        target_for_hash = dict(target)
        if not target_for_hash.get("dataset_split_policy"):
            target_for_hash["dataset_split_policy"] = _dataset_split_policy(
                dict(target_for_hash.get("dataset_identity") or {})
            )
        recomputed = compute_selection_config_hash(target_for_hash)
        if declared_hash != recomputed:
            raise CalibrationMismatchError(
                "selection_config_hash",
                expected=recomputed,
                observed=declared_hash,
            )
        if declared_hash != artifact.selection_config_hash:
            raise CalibrationMismatchError(
                "selection_config_hash",
                expected=artifact.selection_config_hash,
                observed=declared_hash,
            )
        comparisons = {
            "schema_version": artifact.schema_version,
            "git_sha": artifact.git_sha,
            "control_plane_hash": artifact.control_plane_hash,
            "preference_hash": artifact.preference_hash,
            "objective_hash": artifact.objective_hash,
            "objectives": artifact.objectives,
            "objective_directions": artifact.objective_directions or artifact.objectives,
            "objective_required": artifact.objective_required
            or {name: True for name in artifact.objectives},
            "preference_profile_id": artifact.preference_profile_id,
            "preference_profile": artifact.preference_profile,
            "pricing_version": artifact.pricing_version,
            "pricing_registry_hash": artifact.pricing_registry_hash,
            "candidate_catalog_hash": artifact.candidate_catalog_hash,
            "graph_catalog_hash": artifact.graph_catalog_hash,
            "resolved_graph_hash": artifact.resolved_graph_hash,
            "public_evaluator_id": artifact.public_evaluator_id,
            "public_evaluator_version": artifact.public_evaluator_version,
            "backend_kinds": artifact.backend_kinds,
            "model_identifiers": artifact.model_identifiers,
            "backend_model_settings": artifact.backend_model_settings,
            "seed_policy": artifact.seed_policy,
            "benchmark_manifest_hash": artifact.benchmark_manifest_hash,
            "private_data_policy": artifact.private_data_policy,
        }
        for field, expected in comparisons.items():
            observed = target.get(field)
            if _canonical_json(expected) != _canonical_json(observed):
                raise CalibrationMismatchError(
                    field, expected=expected, observed=observed
                )
        # dataset_identity must be present and equal for shared dataset family.
        if _canonical_json(artifact.dataset_identity) != _canonical_json(
            target.get("dataset_identity")
        ):
            # Allow held-out to carry split-specific extras only when core keys match.
            art_ds = dict(artifact.dataset_identity or {})
            tgt_ds = dict(target.get("dataset_identity") or {})
            for key in sorted(set(art_ds) | set(tgt_ds)):
                if key in {"split", "run_id", "source_run_id"}:
                    continue
                if _canonical_json(art_ds.get(key)) != _canonical_json(tgt_ds.get(key)):
                    raise CalibrationMismatchError(
                        f"dataset_identity.{key}",
                        expected=art_ds.get(key),
                        observed=tgt_ds.get(key),
                    )
        tgt_split_id = dict(target.get("dataset_split_identity") or {})
        if str(tgt_split_id.get("split") or run_manifest.get("split") or "") != "heldout":
            raise CalibrationMismatchError(
                "dataset_split_identity.split",
                expected="heldout",
                observed=tgt_split_id.get("split"),
            )
        if not str(tgt_split_id.get("run_id") or "").strip():
            raise CalibrationMismatchError(
                "dataset_split_identity.run_id",
                expected="non-empty held-out run_id",
                observed=tgt_split_id.get("run_id"),
            )
        # Reject unexpected keys so nested mutations cannot silently pass.
        allowed_split_keys = {"split", "run_id"}
        extra = sorted(set(tgt_split_id) - allowed_split_keys)
        if extra:
            raise CalibrationMismatchError(
                "dataset_split_identity",
                expected=f"only keys {sorted(allowed_split_keys)}",
                observed=tgt_split_id,
            )
    elif run_manifest is not None:
        # Non-held-out callers may still compare optional overlapping fields.
        observed_git = manifest_git_sha(run_manifest)
        if observed_git and artifact.git_sha and observed_git != artifact.git_sha:
            raise CalibrationMismatchError(
                "git_sha", expected=artifact.git_sha, observed=observed_git
            )


def assert_run_split_for_held_out(run_dir: Path) -> dict[str, Any]:
    """Fail closed unless the persisted run identity is held-out."""
    manifest_path = Path(run_dir) / "run_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(
            f"held-out reporting requires run_manifest.json under {run_dir}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    split = str(manifest.get("split") or "")
    if split != "heldout":
        raise RuntimeError(
            "held-out reporting may consume held-out runs only; "
            f"refusing split={split!r} under {run_dir}. "
            "A report flag must never relabel a run's persisted split."
        )
    return manifest


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _objective_value(obj: dict[str, Any] | None) -> float | None:
    if not obj:
        return None
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return float(obj)
    if not isinstance(obj, dict):
        return None
    if "available" in obj and not obj.get("available", False):
        return None
    value = obj.get("value")
    return None if value is None else float(value)


def _realized_objectives_for_decision(
    realized_archive: dict[str, Any], decision: dict[str, Any]
) -> dict[str, float | None]:
    """Join realized archive entries to a decision via content hash."""
    target = decision.get("selected_content_hash")
    complete = (
        realized_archive.get("realized_complete")
        or realized_archive.get("complete")
        or {}
    )
    if target:
        for items in complete.values():
            for item in items:
                if item.get("content_hash") == target:
                    vals = (item.get("objectives") or {}).get("values") or {}
                    return {
                        "quality": _objective_value(vals.get("quality")),
                        "cost": _objective_value(vals.get("cost")),
                        "latency": _objective_value(vals.get("latency")),
                    }
    # Fall back to finalized decision snapshot when evaluation_kind is realized.
    if str(decision.get("evaluation_kind") or "") == "realized":
        snap = decision.get("selected_candidate_snapshot") or {}
        vals = (snap.get("objectives") or {}).get("values") or {}
        return {
            "quality": _objective_value(vals.get("quality")),
            "cost": _objective_value(vals.get("cost")),
            "latency": _objective_value(vals.get("latency")),
        }
    return {"quality": None, "cost": None, "latency": None}


def _load_canonical_checkpoint(run_dir: Path) -> dict[str, Any]:
    """Load the canonical task checkpoint if present (shared evidence source)."""
    tasks_dir = run_dir / "tasks"
    if not tasks_dir.exists():
        return {}
    # Prefer task_execution.json (full state), then any *checkpoint*.json.
    candidates = sorted(tasks_dir.rglob("task_execution.json"))
    if not candidates:
        candidates = sorted(tasks_dir.rglob("*checkpoint*.json"))
    if not candidates:
        return {}
    try:
        return _read_json(candidates[0])
    except (OSError, json.JSONDecodeError):
        return {}


def _usage_records_from_checkpoint(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    raw = checkpoint.get("backend_usage_records") or []
    out: list[dict[str, Any]] = []
    for item in raw:
        if hasattr(item, "model_dump"):
            out.append(item.model_dump(mode="json"))
        elif isinstance(item, dict):
            out.append(item)
    return out


def _sum_available_cost(usage_records: list[dict[str, Any]]) -> float | None:
    total = 0.0
    any_available = False
    for rec in usage_records:
        cost = rec.get("estimated_cost_usd")
        if cost is None:
            continue
        number = _finite_float(cost)
        if number is None:
            continue
        total += number
        any_available = True
    return total if any_available else None


def _unique_recovery_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by recovery_id; ignore incarnation-only markers without reclaim."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        rid = event.get("recovery_id")
        if not rid:
            # Legacy markers without recovery_id do not count as recoveries.
            continue
        if rid in seen:
            continue
        seen.add(str(rid))
        unique.append(event)
    return unique


def collect_run_records(run_dir: Path) -> dict[str, Any]:
    """Build report inputs from durable artifacts (never console output).

    Production, fixture, resume, and reporting share one canonical evidence path:
    run manifest + checkpoint/task state + append-only pareto stores.
    """
    run_dir = Path(run_dir)
    manifest = {}
    if (run_dir / "run_manifest.json").exists():
        manifest = _read_json(run_dir / "run_manifest.json")
    summary = {}
    for name in (
        "m6_orchestra_summary.json",
        "stage2_fixture_summary.json",
        "m6_smoke_summary.json",
    ):
        if (run_dir / name).exists():
            summary = _read_json(run_dir / name)
            break
    checkpoint = _load_canonical_checkpoint(run_dir)
    usage_records = _usage_records_from_checkpoint(checkpoint)
    if not usage_records and summary.get("usage_records"):
        # Summary may embed usage when checkpoint layout differs.
        embedded = summary.get("usage_records")
        if isinstance(embedded, list):
            usage_records = [u for u in embedded if isinstance(u, dict)]
    decisions: list[dict[str, Any]] = []
    decisions_path = run_dir / "pareto" / "decisions.jsonl"
    if decisions_path.exists():
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                decisions.append(json.loads(line))
    if not decisions and checkpoint.get("pareto_state"):
        hist = (checkpoint.get("pareto_state") or {}).get("decision_history") or []
        pending = (checkpoint.get("pareto_state") or {}).get("pending_decision")
        decisions = list(hist)
        if pending:
            decisions.append(pending)
    traces: list[dict[str, Any]] = []
    traces_path = run_dir / "pareto" / "search_traces.jsonl"
    if traces_path.exists():
        for line in traces_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                traces.append(json.loads(line))
    estimated = {}
    realized = {}
    if (run_dir / "pareto" / "estimated_archive.json").exists():
        estimated = _read_json(run_dir / "pareto" / "estimated_archive.json")
    if (run_dir / "pareto" / "realized_archive.json").exists():
        realized = _read_json(run_dir / "pareto" / "realized_archive.json")
    recovery_events: list[dict[str, Any]] = []
    if (run_dir / "recovery_events.json").exists():
        raw_rec = _read_json(run_dir / "recovery_events.json")
        if isinstance(raw_rec, list):
            recovery_events = raw_rec
    if not recovery_events:
        recovery_events = list(checkpoint.get("scheduler_recovery_events") or [])
    if not recovery_events and summary.get("scheduler_recovery_events"):
        recovery_events = list(summary.get("scheduler_recovery_events") or [])
    recovery_events = _unique_recovery_events(recovery_events)
    usage_ids = sorted(
        {
            str(u.get("usage_id"))
            for u in usage_records
            if u.get("usage_id")
        }
    )
    total_cost = _sum_available_cost(usage_records)
    phase_costs: dict[str, float | None] = {}
    for phase in (
        "pre_activation",
        "control_plane",
        "post_activation",
        "recovery",
        "historical",
    ):
        phase_costs[phase] = _sum_available_cost(
            [u for u in usage_records if str(u.get("phase") or "") == phase]
        )
    # Canonical checkpoint/derived evidence is authoritative. A mutable summary
    # may supply non-derivable labels only; it never overrides conflicting
    # evidence-derived values.
    # Checkpoint-authoritative: ignore mutable summary committed/solved values.
    committed = [
        sid
        for sid, sub in sorted((checkpoint.get("subtasks") or {}).items())
        if str(
            (sub.get("status") if isinstance(sub, dict) else getattr(sub, "status", ""))
            or ""
        )
        .lower()
        .endswith("committed")
    ]
    revisions = checkpoint.get("plan_revision_history") or []
    applied = [
        r
        for r in revisions
        if str(
            (r.get("status") if isinstance(r, dict) else getattr(r, "status", ""))
            or ""
        )
        .lower()
        .endswith("applied")
    ]
    # One root task solved iff the full fork/join DAG committed.
    if set(map(str, committed)) >= {"s1", "s2", "s3", "s4"}:
        solved = 1
    elif committed and not (checkpoint.get("subtasks") or {}):
        solved = 1
    else:
        solved = 1 if (
            committed
            and len(committed) == len(checkpoint.get("subtasks") or {})
            and (checkpoint.get("subtasks") or {})
        ) else 0
        if not (checkpoint.get("subtasks") or {}) and committed:
            solved = 1
    subtasks_map = checkpoint.get("subtasks") or {}
    prompt_tokens = sum(
        int(u["prompt_tokens"])
        for u in usage_records
        if u.get("prompt_tokens") is not None
    )
    completion_tokens = sum(
        int(u["completion_tokens"])
        for u in usage_records
        if u.get("completion_tokens") is not None
    )
    total_tokens = sum(
        int(u["total_tokens"])
        for u in usage_records
        if u.get("total_tokens") is not None
    )
    phase_usage_counts = {
        phase: len([u for u in usage_records if str(u.get("phase") or "") == phase])
        for phase in (
            "pre_activation",
            "control_plane",
            "post_activation",
            "recovery",
            "historical",
        )
    }
    realized_decisions = [
        d
        for d in decisions
        if str(d.get("realization_status") or "").lower() == "realized"
    ]
    activated = [
        d for d in decisions if d.get("activated_revision_id")
    ]
    censored = [
        d
        for d in decisions
        if "censor" in str(d.get("realization_status") or "").lower()
    ]
    derived: dict[str, Any] = {
        "usage_record_count": len(usage_records),
        "usage_ids": usage_ids,
        "restart_recovery_counts": len(recovery_events),
        "recovery_ids": sorted(
            {str(e.get("recovery_id")) for e in recovery_events if e.get("recovery_id")}
        ),
        "recovery_event_count": len(recovery_events),
        "m5_revision_count": len(applied),
        "committed": committed,
        "committed_subtask_count": len(committed),
        "subtask_count": len(subtasks_map),
        "root_task_count": 1 if subtasks_map or committed else 0,
        "solved_task_count": solved,
        "solved_root_task_count": solved,
        "cost_provenance": "persisted_usage_estimated_cost_usd",
        "phase_costs": phase_costs,
        "phase_usage_counts": phase_usage_counts,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "activated_count": len(activated),
        "realized_count": len(realized_decisions),
        "censored_count": len(censored),
        "selected_count": len(
            [d for d in decisions if d.get("selected_content_hash")]
        ),
    }
    if checkpoint.get("active_plan_revision_id"):
        derived["active_plan_revision_id"] = checkpoint.get("active_plan_revision_id")
    # Always stamp cost from canonical usage (None when unavailable) so a
    # tampered summary cannot leak a fabricated total.
    derived["total_cost_usd"] = total_cost
    if total_cost is not None and solved > 0:
        derived["cost_per_solved"] = total_cost / float(solved)
        derived["cost_per_solved_task"] = total_cost / float(solved)
    else:
        derived["cost_per_solved"] = None
        derived["cost_per_solved_task"] = None
    # Summary first, then derived overwrites every conflicting key.
    evidence_summary = {**summary, **derived}
    wave_records = list(checkpoint.get("scheduler_wave_records") or [])
    public_evals = list(checkpoint.get("public_evaluation_records") or [])
    return {
        "run_dir": str(run_dir),
        "manifest": manifest,
        "summary": evidence_summary,
        "checkpoint": checkpoint,
        "usage_records": usage_records,
        "usage_ids": usage_ids,
        "phase_costs": phase_costs,
        "public_evaluation_records": public_evals,
        "scheduler_wave_records": wave_records,
        "decisions": decisions,
        "traces": traces,
        "estimated_archive": estimated,
        "realized_archive": realized,
        "recovery_events": recovery_events,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def _svg_scatter(
    path: Path,
    *,
    points: list[dict[str, Any]],
    x_key: str,
    y_key: str,
    title: str,
    profile: str,
) -> None:
    """Deterministic SVG scatter without external plotting deps."""
    width, height = 640, 480
    margin = 60
    usable_w = width - 2 * margin
    usable_h = height - 2 * margin
    xs = [p[x_key] for p in points if p.get(x_key) is not None]
    ys = [p[y_key] for p in points if p.get(y_key) is not None]
    if not xs or not ys:
        path.write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
            f'<text x="20" y="40">no {x_key}/{y_key} points</text></svg>\n',
            encoding="utf-8",
        )
        return
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    if math.isclose(xmin, xmax):
        xmax = xmin + 1.0
    if math.isclose(ymin, ymax):
        ymax = ymin + 1.0

    def px(x: float) -> float:
        return margin + (x - xmin) / (xmax - xmin) * usable_w

    def py(y: float) -> float:
        return margin + (1.0 - (y - ymin) / (ymax - ymin)) * usable_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        f'<text x="{margin}" y="28" font-size="16">{title}</text>',
        f'<text x="{margin}" y="48" font-size="12">profile={profile}</text>',
        f'<line x1="{margin}" y1="{height-margin}" x2="{width-margin}" '
        f'y2="{height-margin}" stroke="#333"/>',
        f'<line x1="{margin}" y1="{margin}" x2="{margin}" '
        f'y2="{height-margin}" stroke="#333"/>',
    ]
    for p in sorted(points, key=lambda r: (r.get("content_hash") or "")):
        if p.get(x_key) is None or p.get(y_key) is None:
            continue
        cx, cy = px(float(p[x_key])), py(float(p[y_key]))
        dominated = bool(p.get("dominated"))
        selected = bool(p.get("selected"))
        kind = str(p.get("evaluation_kind") or "estimated")
        fill = "#4c78a8" if kind == "estimated" else "#f58518"
        if dominated:
            fill = "#bbbbbb"
        r = 7 if selected else 4
        stroke = "#111" if selected else fill
        parts.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.5"/>'
        )
        if selected:
            parts.append(
                f'<text x="{cx + 8:.2f}" y="{cy:.2f}" font-size="10">selected</text>'
            )
    parts.append("</svg>\n")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_stage2_report(
    run_dirs: list[Path],
    *,
    output_dir: Path,
    calibration: CalibrationArtifact | None = None,
    seed: int = 42,
    include_oracle: bool = False,
) -> dict[str, Any]:
    """Generate deterministic Stage-2 CSV/JSON/MD/SVG artifacts."""
    del seed  # reserved for future sampling; outputs already sorted deterministically
    output_dir.mkdir(parents=True, exist_ok=True)
    main_rows: list[dict[str, Any]] = []
    profile_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    est_vs_real: list[dict[str, Any]] = []
    frontier_rows: list[dict[str, Any]] = []
    failure_rows: list[dict[str, Any]] = []
    difficulty_rows: list[dict[str, Any]] = []

    for run_dir in sorted(run_dirs, key=lambda p: str(p)):
        rec = collect_run_records(run_dir)
        manifest = rec["manifest"]
        summary = rec["summary"]
        profile = (
            (manifest.get("preference_profile_id"))
            or (manifest.get("pareto_config") or {}).get("preference_profile")
            or summary.get("preference_profile")
            or "unknown"
        )
        mode = (manifest.get("stage2") or {}).get("mode") or summary.get("mode") or "run"
        decisions = rec["decisions"]
        traces = rec["traces"]
        # One search-trace row per candidate; count distinct hashes / statuses.
        cand_traces = [
            t
            for t in traces
            if t.get("candidate_content_hash")
            and str(t.get("pareto_status") or "") != "realized"
        ]
        generated = len(
            {str(t.get("candidate_content_hash")) for t in cand_traces}
        )
        rejected = sum(
            1
            for t in cand_traces
            if str(t.get("feasibility_status") or "") not in {"ok", ""}
        )
        selected_status = {}
        for d in decisions:
            status = str(d.get("selection_status") or "unknown")
            selected_status[status] = selected_status.get(status, 0) + 1
            if str(d.get("evaluation_kind") or "") == "oracle" and not include_oracle:
                # Diagnostic oracle never enters online aggregates.
                failure_rows.append(
                    {
                        "run_dir": str(run_dir),
                        "category": "oracle_diagnostic_excluded",
                        "detail": d.get("decision_id"),
                    }
                )
                continue
            decision_rows.append(
                {
                    "run_dir": str(run_dir),
                    "mode": mode,
                    "profile": profile,
                    "decision_id": d.get("decision_id"),
                    "selection_status": status,
                    "selected_content_hash": d.get("selected_content_hash"),
                    "activated_revision_id": d.get("activated_revision_id"),
                    "label": "online_policy",
                }
            )
            snap = d.get("selected_candidate_snapshot") or {}
            objs = (snap.get("objectives") or {}).get("values") or {}
            # Estimated vs realized remain distinct; join realized by hash/decision.
            realized_vals = _realized_objectives_for_decision(
                rec["realized_archive"], d
            )
            evidence = d.get("realization_evidence") or {}
            usage_join = list(evidence.get("usage_ids") or [])
            eval_join = list(evidence.get("evaluation_ids") or [])
            est_kind = str(d.get("evaluation_kind") or "estimated")
            objective_meta = {
                "quality": {
                    "unit": "normalized_score",
                    "direction": "maximize",
                },
                "cost": {"unit": "usd", "direction": "minimize"},
                "latency": {"unit": "seconds", "direction": "minimize"},
                "risk": {"unit": "score", "direction": "minimize"},
            }
            norm_meta = json.dumps(
                calibration.normalization if calibration else {},
                sort_keys=True,
                separators=(",", ":"),
            )
            base_ids = {
                "run_dir": str(run_dir),
                "run_id": manifest.get("run_id") or Path(run_dir).name,
                "task_id": summary.get("task_id") or manifest.get("task_id"),
                "decision_id": d.get("decision_id"),
                "candidate_hash": d.get("selected_content_hash"),
                "context_id": (d.get("context") or {}).get("context_id"),
                "plan_revision": d.get("activated_revision_id")
                or summary.get("active_plan_revision_id"),
                "activation_revision": d.get("activated_revision_id"),
                "affected_wave_id": d.get("affected_wave_id"),
                "wave_id": d.get("affected_wave_id"),
                "realization_id": d.get("realization_id"),
                "usage_ids": ",".join(str(u) for u in usage_join),
                "evaluation_ids": ",".join(str(e) for e in eval_join),
                "normalization_metadata": norm_meta,
                "censoring_state": d.get("realization_status") or "",
                "failure_state": d.get("reason") or "",
                "metric_provenance": "persisted_pareto_archives",
                "evaluation_kind": est_kind,
                "label": (
                    "diagnostic_oracle" if est_kind == "oracle" else "online_policy"
                ),
            }
            obj_names = sorted(
                set(objs.keys())
                | set(realized_vals.keys())
                | {"quality", "cost", "latency"}
            )
            for obj_name in obj_names:
                est_obj = objs.get(obj_name) or {}
                est_val = _objective_value(est_obj)
                real_val = realized_vals.get(obj_name)
                meta = objective_meta.get(
                    obj_name, {"unit": "", "direction": ""}
                )
                est_vs_real.append(
                    {
                        **base_ids,
                        "objective_name": obj_name,
                        "estimated_value": est_val,
                        "estimated_availability": (
                            est_obj.get("available")
                            if isinstance(est_obj, dict)
                            else est_val is not None
                        ),
                        "estimated_provenance": (
                            (est_obj.get("detail") if isinstance(est_obj, dict) else None)
                            or "persisted_estimated_objectives"
                        ),
                        "realized_value": real_val,
                        "realized_availability": real_val is not None,
                        "realized_provenance": d.get("realization_status")
                        or "persisted_realized_archive",
                        "unit": meta["unit"],
                        "direction": meta["direction"],
                        # Legacy wide columns retained for compatibility.
                        "quality_est": _objective_value(objs.get("quality")),
                        "cost_est": _objective_value(objs.get("cost")),
                        "latency_est": _objective_value(objs.get("latency")),
                        "quality_real": realized_vals.get("quality"),
                        "cost_real": realized_vals.get("cost"),
                        "latency_real": realized_vals.get("latency"),
                        "estimator_provenance": (
                            (est_obj.get("detail") if isinstance(est_obj, dict) else None)
                            or "persisted_estimated_objectives"
                        ),
                        "realization_provenance": d.get("realization_status")
                        or "persisted_realized_archive",
                        f"estimated_value_{obj_name}": est_val,
                        f"realized_value_{obj_name}": real_val,
                    }
                )

        # Frontier points from estimated archive (complete + dominated partials labeled).
        est = rec["estimated_archive"]
        complete_map = est.get("estimated_complete") or est.get("complete") or {}
        partial_map = est.get("estimated_partial") or est.get("partial") or {}
        selected_hashes = {
            d.get("selected_content_hash") for d in decisions if d.get("selected_content_hash")
        }
        frontier_hashes = {
            item.get("content_hash")
            for items in complete_map.values()
            for item in items
        }
        for ctx_id, items in sorted(complete_map.items()):
            for item in items:
                vals = (item.get("objectives") or {}).get("values") or {}
                row = {
                    "run_dir": str(run_dir),
                    "context_id": ctx_id,
                    "content_hash": item.get("content_hash"),
                    "evaluation_kind": "estimated",
                    "dominated": False,
                    "selected": item.get("content_hash") in selected_hashes,
                    "profile": profile,
                    "quality": _objective_value(vals.get("quality")),
                    "cost": _objective_value(vals.get("cost")),
                    "latency": _objective_value(vals.get("latency")),
                    "partial": False,
                    "metric_provenance": "estimated_archive_complete",
                }
                frontier_rows.append(row)
        # Dominated/rejected complete candidates remain auditable via search traces.
        dominated_count = 0
        for t in traces:
            ch = t.get("candidate_content_hash")
            if not ch or ch in frontier_hashes:
                continue
            if str(t.get("feasibility_status") or "") != "ok":
                continue
            objs = t.get("estimated_objectives") or {}
            vals = objs.get("values") or objs
            if not isinstance(vals, dict):
                continue
            # Only plot complete-enough candidates that left the frontier.
            if not all(
                _objective_value(vals.get(name)) is not None
                for name in ("quality", "cost", "latency")
            ):
                continue
            dominated_count += 1
            frontier_rows.append(
                {
                    "run_dir": str(run_dir),
                    "context_id": (t.get("decision_context") or {}).get("context_id")
                    or t.get("decision_id"),
                    "content_hash": ch,
                    "evaluation_kind": "estimated",
                    "dominated": True,
                    "selected": False,
                    "profile": profile,
                    "quality": _objective_value(vals.get("quality")),
                    "cost": _objective_value(vals.get("cost")),
                    "latency": _objective_value(vals.get("latency")),
                    "partial": False,
                    "metric_provenance": "search_trace_dominated_complete",
                }
            )
        for ctx_id, items in sorted(partial_map.items()):
            for item in items:
                vals = (item.get("objectives") or {}).get("values") or {}
                frontier_rows.append(
                    {
                        "run_dir": str(run_dir),
                        "context_id": ctx_id,
                        "content_hash": item.get("content_hash"),
                        "evaluation_kind": "estimated",
                        "dominated": True,
                        "selected": False,
                        "profile": profile,
                        "quality": _objective_value(vals.get("quality")),
                        "cost": _objective_value(vals.get("cost")),
                        "latency": _objective_value(vals.get("latency")),
                        "partial": True,
                        "metric_provenance": "estimated_archive_partial",
                    }
                )

        committed = summary.get("committed") or []
        revisions = int(summary.get("m5_revision_count") or 0)
        total_cost = summary.get("total_cost_usd", "")
        if total_cost == "" and summary.get("cost_provenance"):
            total_cost = summary.get("total_cost_usd")
        realized_decisions = [
            d
            for d in decisions
            if str(d.get("realization_status") or "").lower() == "realized"
        ]
        realized_cost = ""
        if realized_decisions:
            cost_obj = (
                (realized_decisions[0].get("selected_candidate_snapshot") or {})
                .get("objectives", {})
                .get("values", {})
                .get("cost")
                or {}
            )
            if cost_obj.get("value") is not None:
                realized_cost = cost_obj.get("value")
        main_rows.append(
            {
                "run_dir": str(run_dir),
                "run_id": manifest.get("run_id") or Path(run_dir).name,
                "task_id": summary.get("task_id"),
                "mode": mode,
                "profile": profile,
                "hidden_pass_at_1": summary.get("hidden_pass_at_1", ""),
                "execution_success_rate": summary.get(
                    "execution_success_rate",
                    (1.0 if committed else 0.0),
                ),
                "avg_cost_usd": summary.get("avg_cost_usd", ""),
                "total_cost_usd": total_cost if total_cost is not None else "",
                "cost_per_solved": (
                    summary.get("cost_per_solved")
                    if summary.get("cost_per_solved") is not None
                    else ""
                ),
                "cost_available": summary.get("total_cost_usd") is not None
                and summary.get("total_cost_usd") != "",
                "cost_provenance": summary.get("cost_provenance", "unavailable"),
                "wall_latency_s": summary.get("wall_latency_s", ""),
                "critical_path_latency_s": summary.get("critical_path_latency_s", ""),
                "latency_provenance": summary.get(
                    "fixture_latency_label", "unavailable"
                ),
                "communication_overhead": (
                    summary.get("communication_overhead")
                    if summary.get("communication_overhead") is not None
                    else ""
                ),
                "generated_candidates": generated,
                "rejected_candidates": rejected,
                "partial_candidates": len(partial_map),
                "dominated_candidates": dominated_count,
                "selected_candidates": len(
                    [d for d in decisions if d.get("selected_content_hash")]
                ),
                "archive_frontier_size": sum(len(v) for v in complete_map.values()),
                "m5_revision_count": revisions,
                "control_plane_cost_usd": summary.get("control_plane_cost_usd", ""),
                "restart_recovery_counts": len(rec.get("recovery_events") or []),
                "recovery_ids": ",".join(
                    sorted(
                        {
                            str(e.get("recovery_id"))
                            for e in (rec.get("recovery_events") or [])
                            if e.get("recovery_id")
                        }
                    )
                ),
                "usage_record_count": len(rec.get("usage_records") or []),
                "usage_ids": ",".join(rec.get("usage_ids") or []),
                "solved_task_count": summary.get("solved_task_count", ""),
                "solved_root_task_count": summary.get("solved_root_task_count", ""),
                "root_task_count": summary.get("root_task_count", ""),
                "subtask_count": summary.get("subtask_count", ""),
                "committed_subtask_count": summary.get("committed_subtask_count", ""),
                "committed": ",".join(str(x) for x in (summary.get("committed") or [])),
                "prompt_tokens": summary.get("prompt_tokens", ""),
                "completion_tokens": summary.get("completion_tokens", ""),
                "total_tokens": summary.get("total_tokens", ""),
                "complete_attributed_run_cost": (
                    total_cost if total_cost is not None else ""
                ),
                "candidate_attributed_realized_cost": realized_cost,
                "activated_count": summary.get("activated_count", ""),
                "realized_count": summary.get("realized_count", ""),
                "censored_count": summary.get("censored_count", ""),
                "selected_count": summary.get("selected_count", ""),
                "complete_candidates": sum(len(v) for v in complete_map.values()),
                "split": manifest.get("split") or summary.get("split") or "",
                "label": "online_policy",
                "note": "fixture metrics are not real API performance",
            }
        )
        profile_rows.append(
            {
                "profile": profile,
                "mode": mode,
                "decisions": len(decisions),
                "selected": len([d for d in decisions if d.get("selected_content_hash")]),
                "run_dir": str(run_dir),
            }
        )
        difficulty_rows.append(
            {
                "difficulty": summary.get("difficulty", "fixture"),
                "mode": mode,
                "execution_success_rate": main_rows[-1]["execution_success_rate"],
                "run_dir": str(run_dir),
            }
        )

    _write_csv(
        output_dir / "stage2_main_results.csv",
        main_rows,
        [
            "run_dir",
            "run_id",
            "task_id",
            "split",
            "mode",
            "profile",
            "hidden_pass_at_1",
            "execution_success_rate",
            "avg_cost_usd",
            "total_cost_usd",
            "cost_per_solved",
            "solved_task_count",
            "solved_root_task_count",
            "root_task_count",
            "subtask_count",
            "committed_subtask_count",
            "committed",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "complete_attributed_run_cost",
            "candidate_attributed_realized_cost",
            "activated_count",
            "realized_count",
            "censored_count",
            "selected_count",
            "complete_candidates",
            "wall_latency_s",
            "critical_path_latency_s",
            "communication_overhead",
            "generated_candidates",
            "rejected_candidates",
            "partial_candidates",
            "dominated_candidates",
            "selected_candidates",
            "archive_frontier_size",
            "m5_revision_count",
            "control_plane_cost_usd",
            "restart_recovery_counts",
            "usage_record_count",
            "usage_ids",
            "label",
        ],
    )
    _write_csv(
        output_dir / "stage2_by_difficulty.csv",
        difficulty_rows,
        ["difficulty", "mode", "execution_success_rate", "run_dir"],
    )
    _write_csv(
        output_dir / "stage2_profile_results.csv",
        profile_rows,
        ["profile", "mode", "decisions", "selected", "run_dir"],
    )
    _write_csv(
        output_dir / "stage2_decision_summary.csv",
        decision_rows,
        [
            "run_dir",
            "mode",
            "profile",
            "decision_id",
            "selection_status",
            "selected_content_hash",
            "activated_revision_id",
            "label",
        ],
    )
    _write_csv(
        output_dir / "stage2_estimated_vs_realized.csv",
        est_vs_real,
        [
            "run_dir",
            "run_id",
            "task_id",
            "context_id",
            "candidate_hash",
            "decision_id",
            "activation_revision",
            "affected_wave_id",
            "realization_id",
            "objective_name",
            "estimated_value",
            "estimated_availability",
            "estimated_provenance",
            "realized_value",
            "realized_availability",
            "realized_provenance",
            "unit",
            "direction",
            "normalization_metadata",
            "censoring_state",
            "failure_state",
            "usage_ids",
            "evaluation_ids",
            # Legacy wide columns (compatibility).
            "plan_revision",
            "wave_id",
            "quality_est",
            "cost_est",
            "latency_est",
            "quality_real",
            "cost_real",
            "latency_real",
            "estimator_provenance",
            "realization_provenance",
            "metric_provenance",
            "label",
        ],
    )
    _write_csv(
        output_dir / "stage2_frontier_points.csv",
        frontier_rows,
        [
            "run_dir",
            "context_id",
            "content_hash",
            "evaluation_kind",
            "dominated",
            "selected",
            "profile",
            "quality",
            "cost",
            "latency",
            "partial",
        ],
    )
    _write_csv(
        output_dir / "stage2_failures.csv",
        failure_rows,
        ["run_dir", "category", "detail"],
    )

    profile = profile_rows[0]["profile"] if profile_rows else "unknown"
    _svg_scatter(
        output_dir / "pareto_quality_cost.svg",
        points=frontier_rows,
        x_key="cost",
        y_key="quality",
        title="Pareto quality vs cost (estimated)",
        profile=profile,
    )
    _svg_scatter(
        output_dir / "pareto_quality_latency.svg",
        points=frontier_rows,
        x_key="latency",
        y_key="quality",
        title="Pareto quality vs latency (estimated)",
        profile=profile,
    )

    # Deterministic timestamp: earliest persisted run started_at (never wall-clock now).
    run_times = [
        str(collect_run_records(run_dir)["manifest"].get("started_at"))
        for run_dir in sorted(run_dirs, key=lambda p: str(p))
        if collect_run_records(run_dir)["manifest"].get("started_at")
    ]
    report_time = min(run_times) if run_times else "deterministic"
    payload = {
        "generated_at": report_time,
        "run_count": len(run_dirs),
        "calibration": calibration.to_dict() if calibration else None,
        "hypervolume": None,  # not implemented; do not invent
        "notes": [
            "hidden Pass@1 is evaluation-only when present",
            "oracle diagnostics excluded from online aggregates",
            "missing cost/tokens remain empty (unavailable), never coerced to zero",
            "cost_per_solved uses solved root-task denominator",
            "report generation is deterministic given identical persisted inputs",
        ],
        "main_results": main_rows,
    }
    (output_dir / "stage2_summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    md = [
        "# Stage-2 Pareto Summary",
        "",
        f"- runs: {len(run_dirs)}",
        f"- online decisions: {len(decision_rows)}",
        f"- frontier points: {len(frontier_rows)}",
        "",
        "Do not interpret fixture/smoke results as real-model quality gains.",
        "",
    ]
    (output_dir / "stage2_summary.md").write_text("\n".join(md), encoding="utf-8")
    return payload


# Re-export visibility helpers for tests.
PUBLIC_VIS = {
    EvaluationVisibility.PUBLIC.value,
    EvaluationVisibility.DEVELOPMENT.value,
}
FORBIDDEN_ONLINE_VIS = {
    EvaluationVisibility.HIDDEN.value,
    EvaluationVisibility.PRIVATE.value,
    ParetoEvaluationKind.REALIZED.value,  # realized is post-hoc, not online estimate
}


def content_address(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
