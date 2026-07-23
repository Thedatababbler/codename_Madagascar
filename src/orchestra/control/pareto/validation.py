"""Pareto admission checks layered on the M5 future-plan validator."""

from __future__ import annotations

from orchestra.control.pareto.schemas import ParetoOrchestraCandidate
from orchestra.control.slow_loop.communication_safety import (
    active_required_communication_blocks,
    candidate_resolves_active_required_blocks,
)
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
        if isinstance(global_candidate, dict):
            from orchestra.control.slow_loop.schemas import GlobalCandidate

            global_candidate = GlobalCandidate.model_validate(global_candidate)
            candidate.global_candidate = global_candidate
        result = self.validator.validate(
            current_state=current_state,
            proposed_plan=global_candidate.proposed_task_plan,
            edits=list(global_candidate.edits),
            leased_subtask_ids=leased_subtask_ids,
            proposed_scheduling_policy=global_candidate.proposed_scheduling_policy,
            proposed_communication_plan=global_candidate.proposed_communication_plan,
        )
        # Candidates rejected during apply_global_edits must not be admitted even if
        # the resulting unchanged plan happens to pass structural validation.
        status = str(getattr(global_candidate, "validation_status", "") or "").lower()
        rejected = getattr(global_candidate, "rejection_reason", None)
        if status in {"invalid", "rejected"} or rejected:
            result.ok = False
            reason = rejected or status or "rejected edits"
            result.errors.append(f"global candidate rejected: {reason}")
        serialized = str(
            global_candidate.proposed_communication_plan.model_dump(mode="json")
        ).lower()
        forbidden = ("candidateartifact", "privateartifact", "hiddenartifact")
        if any(token in serialized.replace("_", "") for token in forbidden):
            result.errors.append(
                "hidden/private artifact type is forbidden in communication contracts"
            )
            result.ok = False
        # Active required communication blocks are hard feasibility constraints.
        if active_required_communication_blocks(current_state):
            ok, block_errors = candidate_resolves_active_required_blocks(
                state=current_state,
                proposed_communication_plan=global_candidate.proposed_communication_plan,
            )
            if not ok:
                result.ok = False
                result.errors.extend(block_errors)
        candidate.validation_errors = list(result.errors)
        return result
