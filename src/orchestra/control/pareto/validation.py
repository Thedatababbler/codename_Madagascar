"""Pareto admission checks layered on the M5 future-plan validator."""

from __future__ import annotations

from orchestra.control.pareto.schemas import ParetoOrchestraCandidate
from orchestra.control.slow_loop.validation import FuturePlanValidationResult, FuturePlanValidator
from orchestra.control.task_state import TaskExecutionState


class ParetoCandidateValidator:
    def __init__(self, validator: FuturePlanValidator | None = None) -> None:
        self.validator = validator or FuturePlanValidator()

    def validate(
        self,
        candidate: ParetoOrchestraCandidate,
        *,
        current_state: TaskExecutionState,
        leased_subtask_ids: set[str],
    ) -> FuturePlanValidationResult:
        global_candidate = candidate.global_candidate
        result = self.validator.validate(
            current_state=current_state,
            proposed_plan=global_candidate.proposed_task_plan,
            edits=list(global_candidate.edits),
            leased_subtask_ids=leased_subtask_ids,
            proposed_scheduling_policy=global_candidate.proposed_scheduling_policy,
            proposed_communication_plan=global_candidate.proposed_communication_plan,
        )
        serialized = str(
            global_candidate.proposed_communication_plan.model_dump(mode="json")
        ).lower()
        forbidden = ("candidateartifact", "privateartifact", "hiddenartifact")
        if any(token in serialized.replace("_", "") for token in forbidden):
            result.errors.append(
                "hidden/private artifact type is forbidden in communication contracts"
            )
            result.ok = False
        candidate.validation_errors = list(result.errors)
        return result
