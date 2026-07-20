"""SlowLoopController: observe → diagnose → generate ≤K → validate → select → prepare."""

from __future__ import annotations

import logging
import time

from orchestra.control.slow_loop.candidate_generator import (
    RuleBasedGlobalCandidateGenerator,
    eligible_future_subtask_ids,
)
from orchestra.control.slow_loop.diagnosis import detect_triggers, diagnose
from orchestra.control.slow_loop.observation import (
    advance_observation_watermark,
    build_global_observation,
)
from orchestra.control.slow_loop.revision import (
    build_revision,
    commit_prepared_revision,
    prepare_revision_staging,
)
from orchestra.control.slow_loop.schemas import (
    GlobalCandidateValidationStatus,
    GlobalDiagnosisReason,
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
from orchestra.runtime.task_checkpoint import TaskCheckpointStore

logger = logging.getLogger(__name__)


class SlowLoopController:
    def __init__(
        self,
        *,
        config: SlowLoopConfig | None = None,
        generator: RuleBasedGlobalCandidateGenerator | None = None,
        selector: DeterministicGlobalCandidateSelector | None = None,
        validator: FuturePlanValidator | None = None,
        checkpoint_store: TaskCheckpointStore | None = None,
    ) -> None:
        self.config = config or SlowLoopConfig()
        self.generator = generator or RuleBasedGlobalCandidateGenerator(self.config)
        self.selector = selector or DeterministicGlobalCandidateSelector()
        self.validator = validator or FuturePlanValidator(self.config)
        self.checkpoint_store = checkpoint_store

    async def _checkpoint_watermark(
        self,
        *,
        state: TaskExecutionState,
        context: RunContext,
        observation,
        commit: bool,
    ) -> None:
        """Advance watermark only when checkpoint persistence succeeds.

        Diagnosed no-safe outcomes consume evidence. Infrastructure /
        transaction failures must retain evidence for retry.
        """
        previous = None
        if state.slow_loop_state is not None:
            previous = (
                state.slow_loop_state
                if isinstance(state.slow_loop_state, SlowLoopState)
                else SlowLoopState.model_validate(state.slow_loop_state)
            )
            previous = previous.model_copy(deep=True)
        advance_observation_watermark(state, observation=observation)
        if not commit:
            return
        store = self.checkpoint_store or TaskCheckpointStore(context.run_dir)
        try:
            await store.save(state)
        except Exception:
            # Do not silently consume evidence when persistence fails.
            state.slow_loop_state = previous
            raise

    async def maybe_update(
        self,
        *,
        task_plan: TaskPlan,
        state: TaskExecutionState,
        context: RunContext,
        leased_subtask_ids: set[str],
        task_budget: TaskBudgetRemaining | None = None,
        commit: bool = True,
    ) -> SlowLoopUpdateResult:
        del task_plan  # always use state.task_plan as source of truth
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
            task_plan=state.task_plan,
            task_state=state,
            communication_plan=state.communication_plan,
            triggers=triggers,
            budget=self.config.budget,
        )
        if GlobalDiagnosisReason.NO_SAFE_FUTURE_EDIT in diagnosis.reasons and (
            not diagnosis.affected_future_subtask_ids
            or diagnosis.reasons == [GlobalDiagnosisReason.NO_SAFE_FUTURE_EDIT]
        ):
            await self._checkpoint_watermark(
                state=state,
                context=context,
                observation=observation,
                commit=commit,
            )
            return SlowLoopUpdateResult(
                updated=False,
                trigger_reasons=triggers,
                diagnosis=diagnosis,
                message="NO_SAFE_FUTURE_EDIT",
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
            await self._checkpoint_watermark(
                state=state,
                context=context,
                observation=observation,
                commit=commit,
            )
            return SlowLoopUpdateResult(
                updated=False,
                trigger_reasons=triggers,
                diagnosis=diagnosis,
                message="NO_SAFE_FUTURE_EDIT",
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
        validated = []
        for cand in candidates:
            result = self.validator.validate(
                current_state=state,
                proposed_plan=cand.proposed_task_plan,
                edits=list(cand.edits),
                leased_subtask_ids=set(leased_subtask_ids),
                proposed_scheduling_policy=cand.proposed_scheduling_policy,
                proposed_communication_plan=cand.proposed_communication_plan,
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
            await self._checkpoint_watermark(
                state=state,
                context=context,
                observation=observation,
                commit=commit,
            )
            return SlowLoopUpdateResult(
                updated=False,
                revision=rev,
                trigger_reasons=triggers,
                diagnosis=diagnosis,
                message="NO_SAFE_FUTURE_EDIT",
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
            prepared = prepare_revision_staging(
                run_dir=context.run_dir,
                revision=revision,
                new_plan=selected.proposed_task_plan,
                new_communication=selected.proposed_communication_plan,
                scheduling_policy=selected.proposed_scheduling_policy,
                state=state,
                allowed_backend_pools=self.config.allowed_backend_assignments,
                backend_model_pools=self.config.backend_model_pools,
            )
            # Stamp slow-loop counters onto projected state before commit.
            proj_slow = prepared.projected_state.slow_loop_state
            if not isinstance(proj_slow, SlowLoopState):
                proj_slow = SlowLoopState.model_validate(proj_slow or {})
            proj_slow.updates_applied = slow.updates_applied + 1
            proj_slow.last_update_state_version = state.state_version + 1
            proj_slow.last_update_revision_id = prepared.revision.revision_id
            proj_slow.commits_at_last_update = state.committed_subtask_count or len(
                observation.committed_subtasks
            )
            # Watermark advances atomically with the applied revision checkpoint.
            keys = sorted(
                set(list(proj_slow.handled_evidence_keys) + observation.new_evidence_keys)
            )
            proj_slow.handled_evidence_keys = keys
            proj_slow.last_observed_state_version = state.state_version + 1
            proj_slow.last_observed_delivery_index = len(state.delivery_ledger or [])
            proj_slow.last_observed_commit_record_index = len(
                state.workspace_commit_records or []
            )
            proj_slow.last_observed_fast_loop_history_index = len(
                state.fast_loop_history or []
            )
            prepared.projected_state.slow_loop_state = proj_slow
            prepared.projected_state.slow_loop_history = list(state.slow_loop_history) + [
                GlobalUpdateRecord(
                    record_id=prepared.revision.revision_id,
                    revision=state.global_revision + 1,
                    summary=diagnosis.concise_explanation,
                    metadata={
                        "candidate_id": selected.candidate_id,
                        "triggers": [t.value for t in triggers],
                        "edit_types": [e.type for e in selected.edits],
                        "eligible": sorted(eligible),
                    },
                )
            ]

            if not commit:
                return SlowLoopUpdateResult(
                    updated=False,
                    revision=prepared.revision,
                    prepared=prepared,
                    trigger_reasons=triggers,
                    diagnosis=diagnosis,
                    message="prepared",
                )

            store = self.checkpoint_store or TaskCheckpointStore(context.run_dir)
            await commit_prepared_revision(
                state=state,
                prepared=prepared,
                checkpoint_store=store,
                run_dir=context.run_dir,
            )
            # Mirror slow counters onto live state after swap.
            if isinstance(state.slow_loop_state, SlowLoopState):
                state.slow_loop_state.updates_applied = proj_slow.updates_applied
                state.slow_loop_state.last_update_state_version = (
                    proj_slow.last_update_state_version
                )
                state.slow_loop_state.last_update_revision_id = (
                    proj_slow.last_update_revision_id
                )
                state.slow_loop_state.commits_at_last_update = (
                    proj_slow.commits_at_last_update
                )
            logger.info(
                "slow_loop applied revision=%s triggers=%s edits=%s",
                prepared.revision.revision_id,
                [t.value for t in triggers],
                [e.type for e in selected.edits],
            )
            return SlowLoopUpdateResult(
                updated=True,
                revision=prepared.revision.model_copy(
                    update={"status": GlobalPlanRevisionStatus.APPLIED}
                ),
                prepared=prepared,
                trigger_reasons=triggers,
                diagnosis=diagnosis,
                message="applied",
            )
        except Exception as exc:  # noqa: BLE001
            # Staging/checkpoint transaction failure: retain evidence for retry.
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
