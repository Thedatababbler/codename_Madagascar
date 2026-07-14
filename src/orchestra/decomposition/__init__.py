"""Task decomposition package (Milestone 3 Task/Subtask IR)."""

from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.fallback import build_single_subtask_plan
from orchestra.decomposition.schemas import (
    BudgetSpec,
    DecompositionLimits,
    DecompositionStatus,
    SubtaskSpec,
    TaskPlan,
)
from orchestra.decomposition.validator import TaskPlanValidationError, validate_task_plan

__all__ = [
    "BudgetSpec",
    "DecompositionLimits",
    "DecompositionStatus",
    "SubtaskSpec",
    "TaskDecomposer",
    "TaskPlan",
    "TaskPlanValidationError",
    "build_single_subtask_plan",
    "validate_task_plan",
]
