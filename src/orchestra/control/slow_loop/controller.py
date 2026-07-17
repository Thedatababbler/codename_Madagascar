"""SlowLoopController: observe → diagnose → generate ≤K → validate → select → apply."""

from __future__ import annotations

import logging
import time

from orchestra.control.slow_loop.candidate_generator import (
    RuleBasedGlobalCandidateGenerator,
    eligible_future_subtask_ids,
)
from orchestra.control.slow_loop.diagnosis import detect_triggers, diagnose
from orchestra.control.slow_loop.observation import build_global_observation
from orchestra.control.slow_loop.revision import (
    apply_revision_to_state,
    build_revision,
    write_plan_revision_snapshot,
)
from orchestra.control.slow_loop.schemas import (
    GlobalCandidateValidationStatus,
    GlobalPlanRevisionStatus,
    SlowLoopConfig,
    SlowLoopState,
    SlowLoopUpdateResult,
    TaskBudgetRemaining,
    TaskSchedulingPolicy,
)
from orchestra.control.slow_loop.selector import DeterministicGlobalCandidateSelector
from orchestra.control.slow_loop.validation import FuturePlanValidator
from orchestra.control.task_state import GlobalUpdateRecord, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan
from orchestra.runtime.backend import RunContext

logger = logging.getLogger(__name__)


class SlowLoopController:
    def __init__(
        self,
        *,
        config: SlowLoopConfig | None = None,
        generator: RuleBasedGlobalCandidateGenerator | None = None,
        selector: DeterministicGlobalCandidateSelector | None = None,
        validator: FuturePlanValidator | None = None,
    ) -> None:
        self.config = config or SlowLoopConfig()
        self.generator = generator or RuleBasedGlobalCandidateGenerator(self.config)
        self.selector = selector or DeterministicGlobalCandidateSelector()
        self.validator = validator or FuturePlanValidator(self.config)

    async def maybe_update(
        self,
        *,
        task_plan: TaskPlan,
        state: TaskExecutionState,
        context: RunContext,
        leased_subtask_ids: set[str],
        task_budget: TaskBudgetRemaining | None = None,
    ) -> SlowLoopUpdateResult:
        if not self.config.enabled:
            return SlowLoopUpdateResult(updated=False, message="slow_loop disabled")

        started = time.monotonic()
        if state.slow_loop_state is None:
            state.slow_loop_state = SlowLoopState()
        slow: SlowLoopState = (
            state.slow_loop_state
            if isinstance(state.slow_loop_state, SlowLoopState)
            else SlowLoopState.model_validate(state.slow_loop_state)
        )
        state.slow_loop_state = slow

        if slow.updates_applied >= self.config.budget.max_updates_per_task:
            return SlowLoopUpdateResult(
                updated=False, message="max_updates_per_task exhausted"
            )

        policy = state.scheduling_policy or TaskSchedulingPolicy(
            max_concurrent_subtasks=1
        )
        if not isinstance(policy, TaskSchedulingPolicy):
            policy = TaskSchedulingPolicy.model_validate(policy)

        observation = build_global_observation(
            state, scheduling_policy=policy, task_budget=task_budget
        )
        triggers = detect_triggers(observation, budget=self.config.budget)
        if not triggers:
            return SlowLoopUpdateResult(updated=False, message="no trigger")

        diagnosis = diagnose(
            observation=observation,
            task_plan=task_plan,
            task_state=state,
            communication_plan=state.communication_plan,
            triggers=triggers,
            budget=self.config.budget,
        )
        if not diagnosis.update_required:
            return SlowLoopUpdateResult(
                updated=False,
                trigger_reasons=triggers,
                diagnosis=diagnosis,
                message="diagnosis NO_CHANGE",
            )

        eligible = eligible_future_subtask_ids(state) - set(leased_subtask_ids)
        if not eligible:
            return SlowLoopUpdateResult(
                updated=False,
                trigger_reasons=triggers,
                diagnosis=diagnosis,
                message="no eligible future subtasks",
            )

        candidates = self.generator.generate(
            task_plan=state.task_plan,
            communication_plan=state.communication_plan,
            scheduling_policy=policy,
            state=state,
            observation=observation,
            diagnosis=diagnosis,
            eligible=eligible,
        )
        # Future-only validate each candidate.
        validated = []
        for cand in candidates:
            result = self.validator.validate(
                current_state=state,
                proposed_plan=cand.proposed_task_plan,
                edits=list(cand.edits),
                leased_subtask_ids=set(leased_subtask_ids),
            )
            if not result.ok:
                cand.validation_status = GlobalCandidateValidationStatus.INVALID
                cand.rejection_reason = "; ".join(result.errors)
            else:
                validated.append(cand)

        selected = self.selector.select(validated or candidates, observation)
        if selected is None or selected.rejection_reason:
            rev = build_revision(
                state=state,
                parent_plan=state.task_plan,
                new_plan=state.task_plan,
                edits=[],
                diagnosis=diagnosis,
                triggers=triggers,
                eligible=sorted(eligible),
                rejected_edit_ids=[],
            )
            rev.status = GlobalPlanRevisionStatus.REJECTED
            state.plan_revision_history.append(rev)
            state.slow_loop_history.append(
                GlobalUpdateRecord(
                    record_id=rev.revision_id,
                    revision=state.global_revision,
                    summary="slow_loop rejected/no valid candidate",
                    metadata={"triggers": [t.value for t in triggers]},
                )
            )
            return SlowLoopUpdateResult(
                updated=False,
                revision=rev,
                trigger_reasons=triggers,
                diagnosis=diagnosis,
                message="no valid candidate",
            )

        revision = build_revision(
            state=state,
            parent_plan=state.task_plan,
            new_plan=selected.proposed_task_plan,
            edits=list(selected.edits),
            diagnosis=diagnosis,
            triggers=triggers,
            eligible=sorted(eligible),
            rejected_edit_ids=[],
        )
        revision.status = GlobalPlanRevisionStatus.VALIDATED

        # Wall-time budget for control plane.
        if time.monotonic() - started > self.config.budget.max_wall_time_seconds:
            revision.status = GlobalPlanRevisionStatus.FAILED
            state.plan_revision_history.append(revision)
            return SlowLoopUpdateResult(
                updated=False,
                revision=revision,
                trigger_reasons=triggers,
                diagnosis=diagnosis,
                message="slow_loop wall time exhausted",
            )

        try:
            write_plan_revision_snapshot(
                run_dir=context.run_dir,
                revision=revision,
                new_plan=selected.proposed_task_plan,
            )
            apply_revision_to_state(
                state=state,
                revision=revision,
                new_plan=selected.proposed_task_plan,
                scheduling_policy=selected.proposed_scheduling_policy,
            )
            slow.updates_applied += 1
            slow.last_update_state_version = state.state_version
            slow.last_update_revision_id = revision.revision_id
            slow.commits_at_last_update = state.committed_subtask_count or len(
                observation.committed_subtasks
            )
            state.slow_loop_history.append(
                GlobalUpdateRecord(
                    record_id=revision.revision_id,
                    revision=state.global_revision,
                    summary=diagnosis.concise_explanation,
                    metadata={
                        "candidate_id": selected.candidate_id,
                        "triggers": [t.value for t in triggers],
                        "edit_types": [e.type for e in selected.edits],
                        "eligible": sorted(eligible),
                    },
                )
            )
            logger.info(
                "slow_loop applied revision=%s triggers=%s edits=%s",
                revision.revision_id,
                [t.value for t in triggers],
                [e.type for e in selected.edits],
            )
            return SlowLoopUpdateResult(
                updated=True,
                revision=revision,
                trigger_reasons=triggers,
                diagnosis=diagnosis,
                message="applied",
            )
        except Exception as exc:  # noqa: BLE001
            revision.status = GlobalPlanRevisionStatus.FAILED
            revision.metadata["error"] = str(exc)
            state.plan_revision_history.append(revision)
            if self.config.budget.failure_policy == "keep_previous_plan":
                logger.warning("slow_loop failed; keeping previous plan: %s", exc)
                return SlowLoopUpdateResult(
                    updated=False,
                    revision=revision,
                    trigger_reasons=triggers,
                    diagnosis=diagnosis,
                    message=f"failed: {exc}",
                )
            raise
