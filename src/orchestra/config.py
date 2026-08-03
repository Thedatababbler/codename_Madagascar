from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from orchestra.runtime.limits import RuntimeLimits

_ENV = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]+))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.getenv(m.group(1), m.group(2) or m.group(0)), value)
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    return value


class ExperimentSection(BaseModel):
    name: str
    seed: int
    graph_config: str
    contracts_dir: str
    output_root: str
    # Typed split identity — never inferred from directory names or report flags.
    split: Literal["fixture", "development", "heldout"] = "development"
    # Optional multi-subtask production / Stage-2 fields (ignored by Stage-1 runner).
    plan_config: str | None = None
    source_repo: str | None = None


class BenchmarkSection(BaseModel):
    name: Literal["livecodebench"]
    release_version: Literal["release_v6"]
    scenario: Literal["codegeneration"]
    language: Literal["python"]
    manifest: str
    data_dir: str
    repository_path: str


class SandboxLimits(BaseModel):
    memory_mb: int = 2048
    max_processes: int = 32
    max_open_files: int = 128
    max_file_size_mb: int = 16


class SandboxSection(BaseModel):
    backend: Literal["lcb_official", "docker", "mock"]
    per_test_timeout_seconds: float = 6
    worker_grace_seconds: float = 5
    max_worker_wall_seconds: float = 60
    num_process_evaluate: Literal[1] = 1
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    image: str = "python:3.11-slim"


class ExperimentConfig(BaseModel):
    """Stage-1 experiment config with optional opt-in M5/M6 control-plane sections.

    Existing Stage-1 YAML files omit control-plane keys and remain behaviorally
    unchanged (slow_loop/pareto default to disabled).
    """

    experiment: ExperimentSection
    benchmark: BenchmarkSection
    runtime: RuntimeLimits
    sandbox: SandboxSection
    evaluation: dict[str, Any]
    logging: dict[str, Any]
    # Opt-in dual-frequency / Pareto sections (shared typed loader).
    slow_loop: dict[str, Any] = Field(default_factory=dict)
    pareto: dict[str, Any] = Field(default_factory=dict)
    preference_profile: str | None = None
    candidate_catalog: dict[str, Any] = Field(default_factory=dict)
    stage2: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _pareto_requires_slow_loop(self) -> ExperimentConfig:
        pareto_enabled = bool((self.pareto or {}).get("enabled", False))
        slow_enabled = bool((self.slow_loop or {}).get("enabled", False))
        if pareto_enabled and not slow_enabled:
            raise ValueError("pareto.enabled=true requires slow_loop.enabled=true")
        return self


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        _expand(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    )


def load_experiment_raw(path: str | Path) -> dict[str, Any]:
    """Load and expand YAML without discarding control-plane keys."""
    return _expand(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
