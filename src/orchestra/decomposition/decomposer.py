"""Task decomposer: validate candidate plans or fall back to a single subtask."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from orchestra.decomposition.fallback import build_single_subtask_plan
from orchestra.decomposition.schemas import (
    DecompositionLimits,
    DecompositionStatus,
    TaskPlan,
)
from orchestra.decomposition.validator import TaskPlanValidationError, validate_task_plan


class TaskDecomposer:
    """Produce a validated TaskPlan.

    Milestone 3: LLM decomposition is disabled by default. When disabled, always
    emit a deterministic single-subtask plan. When a candidate plan is supplied,
    validate it and fall back explicitly on failure (never silently).
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        default_graph_template: str,
        keystone_harness_id: str = "public_code_harness",
        limits: DecompositionLimits | None = None,
        require_graph_files: bool = True,
    ) -> None:
        self.enabled = enabled
        self.default_graph_template = default_graph_template
        self.keystone_harness_id = keystone_harness_id
        self.limits = limits or DecompositionLimits()
        self.require_graph_files = require_graph_files

    def decompose(
        self,
        *,
        task_id: str,
        objective: str,
        candidate_plan: dict[str, Any] | TaskPlan | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskPlan:
        if not self.enabled:
            return build_single_subtask_plan(
                task_id=task_id,
                objective=objective,
                local_graph_template=self.default_graph_template,
                keystone_harness_id=self.keystone_harness_id,
                status=DecompositionStatus.DISABLED,
                rationale="decomposition disabled; single-subtask compatibility mode",
                metadata=metadata,
            )

        if candidate_plan is None:
            return build_single_subtask_plan(
                task_id=task_id,
                objective=objective,
                local_graph_template=self.default_graph_template,
                keystone_harness_id=self.keystone_harness_id,
                status=DecompositionStatus.FALLBACK_SINGLE_SUBTASK,
                rationale="decomposition enabled but no candidate plan provided",
                metadata=metadata,
            )

        try:
            plan = (
                candidate_plan
                if isinstance(candidate_plan, TaskPlan)
                else TaskPlan.model_validate(candidate_plan)
            )
            validate_task_plan(
                plan,
                limits=self.limits,
                require_graph_files=self.require_graph_files,
            )
            return plan.model_copy(
                update={
                    "decomposition_status": DecompositionStatus.OK,
                    "metadata": {**plan.metadata, **(metadata or {})},
                }
            )
        except (ValidationError, TaskPlanValidationError) as exc:
            return build_single_subtask_plan(
                task_id=task_id,
                objective=objective,
                local_graph_template=self.default_graph_template,
                keystone_harness_id=self.keystone_harness_id,
                status=DecompositionStatus.FALLBACK_SINGLE_SUBTASK,
                rationale=f"invalid decomposition; fallback: {exc}",
                metadata={
                    **(metadata or {}),
                    "fallback_reason": str(exc),
                    "decomposition_error_type": type(exc).__name__,
                },
            )
