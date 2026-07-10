"""Stage 1 LiveCodeBench experiment phase definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from orchestra.config import ExperimentConfig, load_experiment_config

PhaseName = Literal["bringup", "smoke", "dev", "heldout"]
BaselineName = Literal["b0", "b1", "b2"]

PINNED_LCB_COMMIT = "28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24"

BASELINE_CONFIGS: dict[BaselineName, str] = {
    "b0": "configs/experiments/stage1_b0_direct.yaml",
    "b1": "configs/experiments/stage1_b1_single_harness.yaml",
    "b2": "configs/experiments/stage1_b2_fixed_mas.yaml",
}

PHASE_MANIFESTS: dict[PhaseName, str] = {
    "bringup": "configs/manifests/lcb_bringup.json",
    "smoke": "configs/manifests/lcb_smoke.json",
    "dev": "configs/manifests/lcb_dev.json",
    "heldout": "configs/manifests/lcb_heldout.json",
}

PHASE_RUNTIME_OVERRIDES: dict[PhaseName, dict] = {
    "bringup": {
        "max_parallel_benchmark_tasks": 1,
        "max_parallel_llm_calls": 2,
        "max_parallel_sandboxes": 1,
    },
    "smoke": {
        "max_parallel_benchmark_tasks": 2,
        "max_parallel_llm_calls": 4,
        "max_parallel_sandboxes": 2,
    },
    "dev": {
        "max_parallel_benchmark_tasks": 4,
        "max_parallel_llm_calls": 8,
        "max_parallel_sandboxes": 2,
    },
    "heldout": {
        "max_parallel_benchmark_tasks": 4,
        "max_parallel_llm_calls": 8,
        "max_parallel_sandboxes": 2,
    },
}

BASELINE_ORDER: list[BaselineName] = ["b0", "b1", "b2"]


def phase_output_root(phase: PhaseName, baseline: BaselineName) -> str:
    return f"outputs/stage1_experiments/{phase}/{baseline}"


def build_phase_config(phase: PhaseName, baseline: BaselineName) -> ExperimentConfig:
    config = load_experiment_config(BASELINE_CONFIGS[baseline])
    payload = config.model_dump()
    payload["benchmark"]["manifest"] = PHASE_MANIFESTS[phase]
    payload["experiment"]["name"] = f"stage1_{phase}_{baseline}"
    payload["experiment"]["output_root"] = phase_output_root(phase, baseline)
    payload["runtime"].update(PHASE_RUNTIME_OVERRIDES[phase])
    return ExperimentConfig.model_validate(payload)


def write_phase_config(
    phase: PhaseName, baseline: BaselineName, output_dir: str | Path
) -> Path:
    import yaml

    output = Path(output_dir) / f"{phase}_{baseline}.yaml"
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = build_phase_config(phase, baseline).model_dump()
    output.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return output


def write_all_phase_configs(output_dir: str | Path = "configs/experiments/stage1") -> list[Path]:
    paths = []
    for phase in PHASE_MANIFESTS:
        for baseline in BASELINE_CONFIGS:
            paths.append(write_phase_config(phase, baseline, output_dir))
    return paths
