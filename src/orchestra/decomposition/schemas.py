"""Task/Subtask IR schemas (Milestone 3)."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestra.backends.base import ArtifactRef, OutputContract
from orchestra.communication.aggregation import AggregationSpec
from orchestra.communication.plan import CommunicationPlan


class DecompositionStatus(StrEnum):
    OK = "ok"
    FALLBACK_SINGLE_SUBTASK = "fallback_single_subtask"
    DISABLED = "disabled"


class DecompositionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_subtasks: int = 1
    max_subtasks: int = 8
    max_dependency_depth: int = 6

    @field_validator("min_subtasks", "max_subtasks", "max_dependency_depth")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("limits must be >= 1")
        return value


class BudgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_llm_calls: int = 8
    max_steps: int = 8
    timeout_seconds: float = 300.0

    @field_validator("max_llm_calls", "max_steps")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("budget integers must be >= 1")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout_seconds must be > 0")
        return value


class SubtaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subtask_id: str
    title: str
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    expected_outputs: list[OutputContract] = Field(default_factory=list)
    keystone_harness_id: str
    local_graph_template: str
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    priority: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "subtask_id",
        "title",
        "objective",
        "keystone_harness_id",
        "local_graph_template",
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not str(value).strip():
            raise ValueError("field must be non-empty")
        return value


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    subtasks: list[SubtaskSpec]
    final_aggregation: AggregationSpec = Field(default_factory=AggregationSpec)
    communication_plan: CommunicationPlan = Field(default_factory=CommunicationPlan)
    decomposition_rationale: str = ""
    plan_version: int = 1
    decomposition_status: DecompositionStatus = DecompositionStatus.OK
    metadata: dict[str, Any] = Field(default_factory=dict)

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def subtask_map(self) -> dict[str, SubtaskSpec]:
        return {item.subtask_id: item for item in self.subtasks}
