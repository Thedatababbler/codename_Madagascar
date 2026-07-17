"""Rule-based Slow Loop candidate generation (≤K, deterministic)."""

from __future__ import annotations

from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    GlobalCandidate,
    GlobalCandidateValidationStatus,
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalEdit,
    GlobalObservation,
    PendingBackendAssignmentEdit,
    SchedulingConcurrencyEdit,
    SerializationGroupEdit,
    SlowLoopBudget,
    SlowLoopConfig,
    TaskSchedulingPolicy,
    UpsertDeliveryRuleEdit,
    UpsertPayloadContractEdit,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan


class RuleBasedGlobalCandidateGenerator:
    def __init__(self, config: SlowLoopConfig | None = None) -> None:
        self.config = config or SlowLoopConfig()

    def generate(
        self,
        *,
        task_plan: TaskPlan,
        communication_plan: CommunicationPlan,
        scheduling_policy: TaskSchedulingPolicy,
        state: TaskExecutionState,
        observation: GlobalObservation,
        diagnosis: GlobalDiagnosis,
        eligible: set[str],
    ) -> list[GlobalCandidate]:
        del observation
        budget: SlowLoopBudget = self.config.budget
        candidates: list[GlobalCandidate] = []

        # Candidate A: communication-only minimal fix
        edits_a: list[GlobalEdit] = []
        if GlobalDiagnosisReason.CONTEXT_PRESSURE in diagnosis.reasons:
            for sid in diagnosis.affected_future_subtask_ids:
                if sid not in eligible:
                    continue
                current = communication_plan.context_budgets.get(sid, 2048)
                edits_a.append(
                    ContextBudgetEdit(
                        target_subtask_id=sid,
                        max_tokens=max(256, int(current * 0.7)),
                    )
                )
                for contract in communication_plan.payload_contracts:
                    if contract.target_subtask_id != sid:
                        continue
                    if contract.metadata.get("required"):
                        continue
                    # Shrink optional payload max_tokens.
                    new_c = PayloadContract(
                        payload_id=contract.payload_id,
                        source_subtask_id=contract.source_subtask_id,
                        target_subtask_id=contract.target_subtask_id,
                        artifact_type=contract.artifact_type,
                        required_fields=list(contract.required_fields),
                        max_tokens=max(64, contract.max_tokens // 2),
                        metadata={**dict(contract.metadata), "shrunk": True},
                    )
                    edits_a.append(UpsertPayloadContractEdit(contract=new_c))

        if GlobalDiagnosisReason.MISSING_PAYLOAD in diagnosis.reasons:
            for sid in diagnosis.affected_future_subtask_ids:
                if sid not in eligible:
                    continue
                sub = state.subtasks[sid]
                deps = [d for d in sub.spec.dependencies if d]
                if not deps:
                    continue
                src = sorted(deps)[0]
                pid = f"auto_{src}_to_{sid}"
                edits_a.append(
                    UpsertPayloadContractEdit(
                        contract=PayloadContract(
                            payload_id=pid,
                            source_subtask_id=src,
                            target_subtask_id=sid,
                            artifact_type="FinalAnswerArtifact",
                            required_fields=[],
                            max_tokens=1024,
                            metadata={
                                "required": True,
                                "priority": 10,
                                "slot": f"comm:{pid}",
                            },
                        )
                    )
                )
                edits_a.append(
                    UpsertDeliveryRuleEdit(
                        rule=DeliveryRule(
                            rule_id=f"del_{pid}",
                            payload_id=pid,
                            condition="on_commit",
                            forward_only=True,
                        )
                    )
                )

        if edits_a:
            candidates.append(
                self._build(
                    "cand_comm",
                    edits_a,
                    task_plan,
                    communication_plan,
                    scheduling_policy,
                    diagnosis,
                    eligible,
                )
            )

        # Candidate B: scheduling fix
        edits_b: list[GlobalEdit] = []
        if GlobalDiagnosisReason.CANONICAL_CONFLICT_RISK in diagnosis.reasons:
            group = sorted(eligible)[:2]
            if len(group) >= 2:
                edits_b.append(SerializationGroupEdit(subtask_ids=group))
            edits_b.append(SchedulingConcurrencyEdit(max_concurrent_subtasks=1))
        if GlobalDiagnosisReason.BUDGET_PRESSURE in diagnosis.reasons:
            edits_b.append(
                SchedulingConcurrencyEdit(
                    max_concurrent_subtasks=max(
                        1, scheduling_policy.max_concurrent_subtasks // 2
                    )
                )
            )
        if edits_b:
            candidates.append(
                self._build(
                    "cand_sched",
                    edits_b,
                    task_plan,
                    communication_plan,
                    scheduling_policy,
                    diagnosis,
                    eligible,
                )
            )

        # Candidate C: future backend/model adjustment
        edits_c: list[GlobalEdit] = []
        if GlobalDiagnosisReason.BACKEND_INSTABILITY in diagnosis.reasons or (
            GlobalDiagnosisReason.BUDGET_PRESSURE in diagnosis.reasons
        ):
            pools = self.config.backend_model_pools
            allowed = [
                b
                for group in self.config.allowed_backend_assignments.values()
                for b in group
            ]
            if allowed:
                backend = sorted(allowed)[0]
                model = None
                if pools.get(backend):
                    model = sorted(pools[backend])[0]
                for sid in sorted(eligible)[:1]:
                    # Prefer first agent node id placeholder.
                    edits_c.append(
                        PendingBackendAssignmentEdit(
                            subtask_id=sid,
                            node_id="__future_agent__",
                            backend_id=backend,
                            model_name=model,
                        )
                    )
        if edits_c:
            candidates.append(
                self._build(
                    "cand_backend",
                    edits_c,
                    task_plan,
                    communication_plan,
                    scheduling_policy,
                    diagnosis,
                    eligible,
                )
            )

        return candidates[: budget.max_candidates_per_update]

    def _build(
        self,
        candidate_id: str,
        edits: list[GlobalEdit],
        task_plan: TaskPlan,
        communication_plan: CommunicationPlan,
        scheduling_policy: TaskSchedulingPolicy,
        diagnosis: GlobalDiagnosis,
        eligible: set[str],
    ) -> GlobalCandidate:
        new_plan, new_comm, new_policy, rejected = apply_global_edits(
            task_plan=task_plan,
            communication_plan=communication_plan,
            scheduling_policy=scheduling_policy,
            edits=edits,
            eligible_subtask_ids=eligible,
        )
        status = (
            GlobalCandidateValidationStatus.INVALID
            if rejected
            else GlobalCandidateValidationStatus.VALID
        )
        return GlobalCandidate(
            candidate_id=candidate_id,
            diagnosis=diagnosis,
            edits=edits,
            proposed_task_plan=new_plan,
            proposed_communication_plan=new_comm,
            proposed_scheduling_policy=new_policy,
            validation_status=status,
            rejection_reason=("rejected ineligible edits: " + ",".join(rejected))
            if rejected
            else None,
            heuristic_score=float(len(edits)),
        )


def eligible_future_subtask_ids(state: TaskExecutionState) -> set[str]:
    return {
        sid
        for sid, sub in state.subtasks.items()
        if sub.status in {SubtaskStatus.PENDING, SubtaskStatus.READY}
        and sub.lease_status == "unleased"
    }
