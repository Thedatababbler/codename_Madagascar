"""Task adapter protocol."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from orchestra.ir.artifacts import ArtifactEnvelope


class TaskEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    execution_success: bool
    answer_correct: bool | None = None
    extracted_answer: str | None = None
    reference_answer: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TaskAdapter(Protocol):
    task_type: str

    def load_instance(self, task_id: str) -> dict[str, Any]: ...

    def build_problem_artifact(self, instance: dict[str, Any]) -> ArtifactEnvelope: ...

    def build_instruction(self, instance: dict[str, Any]) -> str: ...

    def evaluate(
        self,
        *,
        instance: dict[str, Any],
        final_artifact: ArtifactEnvelope | None,
        execution_success: bool,
    ) -> TaskEvaluationResult: ...
