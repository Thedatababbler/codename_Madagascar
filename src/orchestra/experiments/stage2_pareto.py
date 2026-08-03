"""Stage-2 Pareto experiment configs, fixture runner, calibration, and reports."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
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


@dataclass(frozen=True)
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
    backend_model_settings: dict[str, Any]
    backend_kinds: list[str]
    model_identifiers: dict[str, Any]
    seed_policy: dict[str, Any]
    git_sha: str
    config_hash: str
    benchmark_manifest_hash: str
    dataset_split_identity: dict[str, Any]
    private_data_policy: str
    public_evaluator_id: str
    public_evaluator_version: str
    preference_profile: dict[str, Any]
    source_run_id: str
    manifest_schema_version: str
    normalization: dict[str, dict[str, float]]
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
            "backend_model_settings": self.backend_model_settings,
            "backend_kinds": self.backend_kinds,
            "model_identifiers": self.model_identifiers,
            "seed_policy": self.seed_policy,
            "git_sha": self.git_sha,
            "config_hash": self.config_hash,
            "benchmark_manifest_hash": self.benchmark_manifest_hash,
            "dataset_split_identity": self.dataset_split_identity,
            "private_data_policy": self.private_data_policy,
            "public_evaluator_id": self.public_evaluator_id,
            "public_evaluator_version": self.public_evaluator_version,
            "source_run_id": self.source_run_id,
            "manifest_schema_version": self.manifest_schema_version,
            "normalization": self.normalization,
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
        split = str(payload.get("source_split") or payload.get("split") or "development")
        objectives = {
            str(k): str(v) for k, v in dict(payload.get("objectives") or {}).items()
        }
        directions = {
            str(k): str(v)
            for k, v in dict(
                payload.get("objective_directions") or payload.get("objectives") or {}
            ).items()
        }
        return cls(
            schema_version=str(
                payload.get("schema_version")
                or payload.get("manifest_schema_version")
                or "stage2-calibration-v2"
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
            backend_model_settings=dict(payload.get("backend_model_settings") or {}),
            backend_kinds=list(payload.get("backend_kinds") or []),
            model_identifiers=dict(payload.get("model_identifiers") or {}),
            seed_policy=dict(payload.get("seed_policy") or {}),
            git_sha=str(payload.get("git_sha") or ""),
            config_hash=str(payload.get("config_hash") or ""),
            benchmark_manifest_hash=str(payload.get("benchmark_manifest_hash") or ""),
            dataset_split_identity=dict(payload.get("dataset_split_identity") or {}),
            private_data_policy=str(
                payload.get("private_data_policy") or "private_labels_offline_only"
            ),
            public_evaluator_id=str(
                payload.get("public_evaluator_id") or "public_harness"
            ),
            public_evaluator_version=str(
                payload.get("public_evaluator_version") or "public-harness-v1"
            ),
            source_run_id=str(payload.get("source_run_id") or ""),
            manifest_schema_version=str(
                payload.get("manifest_schema_version") or "stage2-calibration-v2"
            ),
            normalization=dict(payload.get("normalization") or {}),
            reference_point=dict(payload.get("reference_point") or {}),
            created_at=str(payload.get("created_at") or ""),
            source_split=split,
            split=split,
        )


def _stable_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def write_calibration_artifact(
    path: str | Path,
    *,
    control: ControlPlaneConfig,
    development_points: list[dict[str, float | None]],
    run_dir: str | Path | None = None,
    config_path: str | Path | None = None,
) -> CalibrationArtifact:
    from orchestra.experiments.metadata import git_commit_hash

    resolved = resolve_pareto_runtime(control, repo_root=_repo_root())
    norms: dict[str, dict[str, float]] = {}
    reference: dict[str, float] = {}
    for name, direction in resolved.pareto_config.objectives.items():
        values = [
            float(p[name])
            for p in development_points
            if p.get(name) is not None and not isinstance(p.get(name), bool)
        ]
        if not values:
            continue
        lo, hi = min(values), max(values)
        if math.isclose(lo, hi):
            hi = lo + 1.0
        norms[name] = {"min": lo, "max": hi}
        reference[name] = hi if direction is ObjectiveDirection.MINIMIZE else lo

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
    seed = 42
    if config_path is not None:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        seed = int((raw.get("experiment") or {}).get("seed") or 42)
    if run_dir is not None and (Path(run_dir) / "run_manifest.json").exists():
        manifest = json.loads(
            (Path(run_dir) / "run_manifest.json").read_text(encoding="utf-8")
        )
        seed = int(
            ((manifest.get("experiment") or {}).get("seed"))
            or ((manifest.get("stage2") or {}).get("seed"))
            or seed
        )

    source_run_id = ""
    source_split = "development"
    config_hash = ""
    benchmark_manifest_hash = ""
    if run_dir is not None and (Path(run_dir) / "run_manifest.json").exists():
        manifest = json.loads(
            (Path(run_dir) / "run_manifest.json").read_text(encoding="utf-8")
        )
        source_run_id = str(manifest.get("run_id") or Path(run_dir).name)
        run_split = str(manifest.get("split") or "development")
        if run_split not in {"development", "fixture"}:
            raise RuntimeError(
                "freeze-calibration may consume development (or fixture-as-dev) "
                f"runs only; got split={run_split!r}"
            )
        # Fixture runs may seed development calibration for synthetic gates only.
        source_split = "development"
        config_hash = str(manifest.get("control_plane_hash") or "")
        benchmark_manifest_hash = str(manifest.get("manifest_hash") or "")
    if config_path is not None:
        config_hash = config_hash or hashlib.sha256(
            Path(config_path).read_bytes()
        ).hexdigest()
        raw_cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        bench_manifest = (raw_cfg.get("benchmark") or {}).get("manifest")
        if bench_manifest:
            bm = Path(bench_manifest)
            if not bm.is_absolute():
                bm = _repo_root() / bm
            if bm.exists():
                benchmark_manifest_hash = hashlib.sha256(bm.read_bytes()).hexdigest()
    required_objectives = {
        name: direction.value
        for name, direction in resolved.pareto_config.objectives.items()
    }
    # Fill missing normalization ranges from explicit development fallbacks so
    # every required objective is covered (fail closed later if still missing).
    fallback_defaults = {
        "quality": {"min": 0.0, "max": 1.0},
        "cost": {"min": 0.0, "max": 1.0},
        "latency": {"min": 0.0, "max": 10.0},
        "risk": {"min": 0.0, "max": 1.0},
        "communication_overhead": {"min": 0.0, "max": 1000.0},
    }
    for name, direction in resolved.pareto_config.objectives.items():
        if name in norms:
            continue
        fb = fallback_defaults.get(name, {"min": 0.0, "max": 1.0})
        norms[name] = dict(fb)
        reference[name] = (
            fb["max"] if direction is ObjectiveDirection.MINIMIZE else fb["min"]
        )
    missing_norms = [n for n in required_objectives if n not in norms]
    if missing_norms:
        raise RuntimeError(
            "calibration freeze refused: missing normalization for required "
            f"objectives {missing_norms}"
        )
    artifact = CalibrationArtifact(
        schema_version="stage2-calibration-v2",
        control_plane_hash=resolved.control_plane_hash,
        preference_hash=resolved.preference_hash,
        objective_hash=resolved.objective_hash,
        pricing_version=resolved.pricing_version,
        pricing_registry_hash=pricing_registry_hash,
        candidate_catalog_hash=_stable_hash(catalog_dump),
        graph_catalog_hash=_stable_hash(graph_hashes),
        resolved_graph_hash=_stable_hash(graph_hashes),
        preference_profile_id=resolved.preference_profile.profile_id,
        preference_profile=resolved.preference_profile.model_dump(mode="json"),
        objectives=required_objectives,
        objective_directions=required_objectives,
        backend_model_settings={
            "allowed_backend_assignments": dict(
                resolved.slow_loop_config.allowed_backend_assignments or {}
            ),
            "backend_model_pools": dict(
                resolved.slow_loop_config.backend_model_pools or {}
            ),
            "pricing_registry": resolved.pricing_registry,
        },
        backend_kinds=sorted(
            (resolved.slow_loop_config.allowed_backend_assignments or {}).keys()
        ),
        model_identifiers=dict(resolved.slow_loop_config.backend_model_pools or {}),
        seed_policy={"seed": seed, "deterministic_reports": True},
        git_sha=git_commit_hash(_repo_root()) or "",
        config_hash=config_hash,
        benchmark_manifest_hash=benchmark_manifest_hash,
        dataset_split_identity={
            "source_split": source_split,
            "source_run_id": source_run_id,
        },
        private_data_policy="private_labels_offline_only",
        public_evaluator_id="public_harness",
        public_evaluator_version="public-harness-v1",
        source_run_id=source_run_id,
        manifest_schema_version="stage2-calibration-v2",
        normalization=norms,
        reference_point=reference,
        created_at=datetime.now(UTC).isoformat(),
        source_split=source_split,
        split=source_split,
    )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(artifact.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact


def assert_calibration_matches(
    artifact: CalibrationArtifact,
    control: ControlPlaneConfig,
    *,
    require_held_out_split: bool = False,
    run_manifest: dict[str, Any] | None = None,
) -> None:
    resolved = resolve_pareto_runtime(control, repo_root=_repo_root())
    mismatches: list[str] = []
    if artifact.source_split not in {"development"}:
        mismatches.append("calibration_not_sourced_from_development")
    if require_held_out_split:
        if run_manifest is None:
            mismatches.append("missing_run_manifest_for_heldout_gate")
        else:
            run_split = str(run_manifest.get("split") or "")
            if run_split != "heldout":
                mismatches.append(f"target_run_not_heldout(split={run_split!r})")
    if artifact.control_plane_hash != resolved.control_plane_hash:
        mismatches.append("control_plane_hash")
    if artifact.preference_hash != resolved.preference_hash:
        mismatches.append("preference_hash")
    if artifact.objective_hash != resolved.objective_hash:
        mismatches.append("objective_hash")
    if artifact.pricing_version != resolved.pricing_version:
        mismatches.append("pricing_version")
    expected_objectives = {
        name: direction.value
        for name, direction in resolved.pareto_config.objectives.items()
    }
    if artifact.objectives != expected_objectives:
        mismatches.append("objectives")
    if artifact.objective_directions and artifact.objective_directions != expected_objectives:
        mismatches.append("objective_directions")
    for name in expected_objectives:
        if name not in (artifact.normalization or {}):
            mismatches.append(f"missing_normalization:{name}")
    catalog_hash = _stable_hash(resolved.candidate_catalog.model_dump(mode="json"))
    if artifact.candidate_catalog_hash and artifact.candidate_catalog_hash != catalog_hash:
        mismatches.append("candidate_catalog_hash")
    pricing_path = Path(resolved.pricing_registry)
    if not pricing_path.is_absolute():
        pricing_path = _repo_root() / pricing_path
    pricing_hash = (
        hashlib.sha256(pricing_path.read_bytes()).hexdigest()
        if pricing_path.exists()
        else ""
    )
    if (
        artifact.pricing_registry_hash
        and pricing_hash
        and artifact.pricing_registry_hash != pricing_hash
    ):
        mismatches.append("pricing_registry_hash")
    if (
        artifact.preference_profile_id
        and artifact.preference_profile_id != resolved.preference_profile.profile_id
    ):
        mismatches.append("preference_profile_id")
    if artifact.public_evaluator_version and artifact.public_evaluator_version != (
        "public-harness-v1"
    ):
        mismatches.append("evaluator_version")
    if run_manifest is not None:
        if (
            artifact.graph_catalog_hash
            and run_manifest.get("graph_catalog_hash")
            and artifact.graph_catalog_hash != run_manifest.get("graph_catalog_hash")
            and artifact.resolved_graph_hash != run_manifest.get("graph_catalog_hash")
        ):
            # Only fail when both sides present and disagree with resolved hash too.
            if artifact.resolved_graph_hash and artifact.resolved_graph_hash != str(
                run_manifest.get("graph_catalog_hash")
            ):
                mismatches.append("graph_catalog_hash")
        if (
            artifact.benchmark_manifest_hash
            and run_manifest.get("manifest_hash")
            and artifact.benchmark_manifest_hash != run_manifest.get("manifest_hash")
        ):
            mismatches.append("benchmark_manifest_hash")
    if mismatches:
        raise RuntimeError(
            "frozen calibration mismatch: refusing held-out reporting "
            f"(mismatches={mismatches}; artifact={artifact.control_plane_hash}; "
            f"current={resolved.control_plane_hash})"
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


def collect_run_records(run_dir: Path) -> dict[str, Any]:
    """Build report inputs from durable artifacts (never console output)."""
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
    decisions: list[dict[str, Any]] = []
    decisions_path = run_dir / "pareto" / "decisions.jsonl"
    if decisions_path.exists():
        for line in decisions_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                decisions.append(json.loads(line))
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
    return {
        "run_dir": str(run_dir),
        "manifest": manifest,
        "summary": summary,
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
        generated = sum(int(t.get("generated_count") or 0) for t in traces)
        rejected = sum(int(t.get("rejected_count") or 0) for t in traces)
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
            est_kind = str(d.get("evaluation_kind") or "estimated")
            est_vs_real.append(
                {
                    "run_dir": str(run_dir),
                    "run_id": manifest.get("run_id") or Path(run_dir).name,
                    "task_id": summary.get("task_id") or manifest.get("task_id"),
                    "decision_id": d.get("decision_id"),
                    "candidate_hash": d.get("selected_content_hash"),
                    "context_id": (d.get("context") or {}).get("context_id"),
                    "plan_revision": d.get("activated_revision_id")
                    or summary.get("active_plan_revision_id"),
                    "activation_revision": d.get("activated_revision_id"),
                    "realization_id": d.get("realization_id"),
                    "objective_name": "multi",
                    "quality_est": _objective_value(objs.get("quality")),
                    "cost_est": _objective_value(objs.get("cost")),
                    "latency_est": _objective_value(objs.get("latency")),
                    "quality_real": realized_vals.get("quality"),
                    "cost_real": realized_vals.get("cost"),
                    "latency_real": realized_vals.get("latency"),
                    "quality_est_available": (objs.get("quality") or {}).get("available"),
                    "cost_est_available": (objs.get("cost") or {}).get("available"),
                    "latency_est_available": (objs.get("latency") or {}).get("available"),
                    "quality_real_available": realized_vals.get("quality") is not None,
                    "cost_real_available": realized_vals.get("cost") is not None,
                    "latency_real_available": realized_vals.get("latency") is not None,
                    "quality_units": "normalized_score",
                    "cost_units": "usd",
                    "latency_units": "seconds",
                    "estimator_provenance": (objs.get("quality") or {}).get("detail")
                    or "persisted_estimated_objectives",
                    "realization_provenance": d.get("realization_status")
                    or "persisted_realized_archive",
                    "direction_quality": "maximize",
                    "direction_cost": "minimize",
                    "direction_latency": "minimize",
                    "censoring_state": d.get("realization_status") or "",
                    "metric_provenance": "persisted_pareto_archives",
                    "evaluation_kind": est_kind,
                    "label": (
                        "diagnostic_oracle"
                        if est_kind == "oracle"
                        else "online_policy"
                    ),
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
                "generated_candidates": generated or summary.get("generated_candidates", ""),
                "rejected_candidates": rejected or summary.get("rejected_candidates", ""),
                "partial_candidates": summary.get("partial_candidates", len(partial_map)),
                "dominated_candidates": summary.get(
                    "dominated_candidates", dominated_count
                ),
                "selected_candidates": len(
                    [d for d in decisions if d.get("selected_content_hash")]
                ),
                "archive_frontier_size": sum(len(v) for v in complete_map.values()),
                "m5_revision_count": revisions,
                "control_plane_cost_usd": summary.get("control_plane_cost_usd", ""),
                "restart_recovery_counts": (
                    len(rec.get("recovery_events") or [])
                    if rec.get("recovery_events") is not None
                    else summary.get("restart_recovery_counts", 0)
                ),
                "solved_task_count": summary.get("solved_task_count", ""),
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
            "mode",
            "profile",
            "hidden_pass_at_1",
            "execution_success_rate",
            "avg_cost_usd",
            "total_cost_usd",
            "cost_per_solved",
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
            "decision_id",
            "candidate_hash",
            "context_id",
            "plan_revision",
            "activation_revision",
            "realization_id",
            "quality_est",
            "cost_est",
            "latency_est",
            "quality_real",
            "cost_real",
            "latency_real",
            "quality_est_available",
            "cost_est_available",
            "latency_est_available",
            "quality_real_available",
            "cost_real_available",
            "latency_real_available",
            "quality_units",
            "cost_units",
            "latency_units",
            "estimator_provenance",
            "realization_provenance",
            "direction_quality",
            "direction_cost",
            "direction_latency",
            "censoring_state",
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
