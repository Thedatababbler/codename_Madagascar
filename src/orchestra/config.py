import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

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
    timeout_seconds: float = 10
    num_process_evaluate: Literal[1] = 1
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    image: str = "python:3.11-slim"


class ExperimentConfig(BaseModel):
    experiment: ExperimentSection
    benchmark: BenchmarkSection
    runtime: RuntimeLimits
    sandbox: SandboxSection
    evaluation: dict[str, Any]
    logging: dict[str, Any]


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        _expand(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    )
