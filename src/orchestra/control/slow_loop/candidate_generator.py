"""Rule-based Slow Loop candidate generation (≤K, deterministic)."""

from __future__ import annotations

from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.payload import DeliveryRule, DeliveryTrigger, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.agent_node_resolver import FutureAgentNodeResolver
from orchestra.control.slow_loop.communication_safety import enabled_rule_payload_ids
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.graph_materializer import (
    FutureGraphMaterializer,
    GraphMaterializationError,
)
from orchestra.control.slow_loop.schemas import (
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


def _enabled_delivery_rule_edit(
    *,
    payload_id: str,
    existing_rules: list[DeliveryRule],
) -> UpsertDeliveryRuleEdit:
    """Enable/replace a DeliveryRule for ``payload_id`` (delivery-engine aligned)."""
    if existing_rules:
        base = sorted(existing_rules, key=lambda r: (-r.priority, r.rule_id))[0]
        return UpsertDeliveryRuleEdit(
            rule=DeliveryRule(
                rule_id=base.rule_id,
                payload_id=payload_id,
                trigger=base.trigger,
                enabled=True,
                condition=base.condition,
                priority=base.priority,
                forward_only=base.forward_only,
            )
        )
    return UpsertDeliveryRuleEdit(
        rule=DeliveryRule(
            rule_id=f"del_{payload_id}",
            payload_id=payload_id,
            trigger=DeliveryTrigger.ON_SOURCE_COMMIT,
            enabled=True,
            forward_only=True,
        )
    )

# Communication blocks that cannot be safely repaired by future-only edits.
_UNREPAIRABLE_COMM_REASONS = {
    DeliveryFailureReason.REQUIRED_CONDITION_UNSATISFIED.value,
    DeliveryFailureReason.LEDGER_CORRUPTION.value,
    DeliveryFailureReason.PROJECTION_INFEASIBLE.value,
    DeliveryFailureReason.AGGREGATION_CONFLICT.value,
    DeliveryFailureReason.AGGREGATION_RULE_MISSING.value,
    DeliveryFailureReason.AMBIGUOUS_AGGREGATION_RULE.value,
    DeliveryFailureReason.AGGREGATION_INPUT_SET_MISMATCH.value,
    DeliveryFailureReason.AGGREGATION_REQUIRED_INPUT_MISSING.value,
    DeliveryFailureReason.REQUIRED_ARTIFACT_MISSING.value,
    DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED.value,
    DeliveryFailureReason.REQUIRED_FIELD_MISSING.value,
}


class RuleBasedGlobalCandidateGenerator:
    def __init__(self, config: SlowLoopConfig | None = None) -> None:
        self.config = config or SlowLoopConfig()
        self.resolver = FutureAgentNodeResolver()
        self.materializer = FutureGraphMaterializer()

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
        by_spec = {s.subtask_id: s for s in task_plan.subtasks}

        # Candidate A: communication-only minimal fix (optional shrink only).
        edits_a: list[GlobalEdit] = []
        if GlobalDiagnosisReason.CONTEXT_PRESSURE in diagnosis.reasons:
            for sid in diagnosis.affected_future_subtask_ids:
                if sid not in eligible:
                    continue
                for contract in communication_plan.payload_contracts:
                    if contract.target_subtask_id != sid:
                        continue
                    if contract.is_required():
                        continue
                    new_c = PayloadContract(
                        payload_id=contract.payload_id,
                        source_subtask_id=contract.source_subtask_id,
                        target_subtask_id=contract.target_subtask_id,
                        artifact_type=contract.artifact_type,
                        required=False,
                        required_fields=list(contract.required_fields),
                        max_tokens=max(64, contract.max_tokens // 2),
                        metadata={**dict(contract.metadata), "shrunk": True},
                    )
                    edits_a.append(UpsertPayloadContractEdit(contract=new_c))

        if GlobalDiagnosisReason.MISSING_PAYLOAD in diagnosis.reasons or (
            GlobalDiagnosisReason.DELIVERY_FAILURE in diagnosis.reasons
        ):
            # Align with delivery engine: only enabled rules count.
            enabled_ruled = enabled_rule_payload_ids(communication_plan)
            for sid in diagnosis.affected_future_subtask_ids:
                if sid not in eligible:
                    continue
                sub = state.subtasks[sid]
                block_reason = sub.communication_block_reason or ""
                if block_reason in _UNREPAIRABLE_COMM_REASONS:
                    # Leave unrepaired; selector/controller yield NO_SAFE_FUTURE_EDIT.
                    continue

                # Exact repair: required contracts with no enabled DeliveryRule
                # (missing entirely, or only disabled rules present).
                restored_existing = False
                for contract in communication_plan.payload_contracts:
                    if contract.target_subtask_id != sid:
                        continue
                    if not contract.is_required():
                        continue
                    if contract.payload_id in enabled_ruled:
                        continue
                    existing_rules = [
                        r
                        for r in communication_plan.delivery_schedule
                        if r.payload_id == contract.payload_id
                    ]
                    edits_a.append(
                        _enabled_delivery_rule_edit(
                            payload_id=contract.payload_id,
                            existing_rules=existing_rules,
                        )
                    )
                    enabled_ruled.add(contract.payload_id)
                    restored_existing = True

                if restored_existing or block_reason == (
                    DeliveryFailureReason.REQUIRED_RULE_MISSING.value
                ):
                    # Do not create a duplicate auto contract while the required
                    # contract remains unresolved / just repaired.
                    continue

                deps = [d for d in sub.spec.dependencies if d]
                if not deps:
                    continue
                # Already covered by a required contract with an enabled rule.
                if any(
                    c.target_subtask_id == sid
                    and c.is_required()
                    and c.payload_id in enabled_ruled
                    for c in communication_plan.payload_contracts
                ):
                    continue
                src = sorted(deps)[0]
                pid = f"auto_{src}_to_{sid}"
                existing_auto = next(
                    (
                        c
                        for c in communication_plan.payload_contracts
                        if c.payload_id == pid
                    ),
                    None,
                )
                if existing_auto is not None:
                    # Auto contract exists but enabled rule absent → restore/enable.
                    if pid not in enabled_ruled:
                        existing_rules = [
                            r
                            for r in communication_plan.delivery_schedule
                            if r.payload_id == pid
                        ]
                        edits_a.append(
                            _enabled_delivery_rule_edit(
                                payload_id=pid, existing_rules=existing_rules
                            )
                        )
                        enabled_ruled.add(pid)
                    continue
                edits_a.append(
                    UpsertPayloadContractEdit(
                        contract=PayloadContract(
                            payload_id=pid,
                            source_subtask_id=src,
                            target_subtask_id=sid,
                            artifact_type="FinalAnswerArtifact",
                            required=True,
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
                    _enabled_delivery_rule_edit(payload_id=pid, existing_rules=[])
                )
                enabled_ruled.add(pid)

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
                    state=state,
                )
            )

        # Candidate B: scheduling fix
        edits_b: list[GlobalEdit] = []
        if GlobalDiagnosisReason.CANONICAL_CONFLICT_RISK in diagnosis.reasons or (
            GlobalDiagnosisReason.SCHEDULING_CONTENTION in diagnosis.reasons
            or GlobalDiagnosisReason.HARNESS_INSTABILITY in diagnosis.reasons
        ):
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
                    state=state,
                )
            )

        # Candidate C: future backend/model adjustment via real agent nodes.
        edits_c: list[GlobalEdit] = []
        rejection_c: str | None = None
        if GlobalDiagnosisReason.BACKEND_INSTABILITY in diagnosis.reasons or (
            GlobalDiagnosisReason.BUDGET_PRESSURE in diagnosis.reasons
            or GlobalDiagnosisReason.HARNESS_INSTABILITY in diagnosis.reasons
        ):
            pools = self.config.backend_model_pools
            allowed = sorted(
                {
                    b
                    for group in self.config.allowed_backend_assignments.values()
                    for b in group
                }
            )
            if not allowed:
                rejection_c = "NO_ELIGIBLE_FUTURE_AGENT_NODE"
            else:
                for sid in sorted(eligible)[:1]:
                    spec = by_spec.get(sid)
                    if spec is None:
                        continue
                    resolution = self.resolver.resolve(
                        subtask=spec,
                        purpose="backend_adaptation",
                        allowed_backend_ids=set(allowed),
                    )
                    if not resolution.eligible or not resolution.node_id:
                        rejection_c = (
                            resolution.reason or "NO_ELIGIBLE_FUTURE_AGENT_NODE"
                        )
                        continue
                    # Pick an alternate allowlisted backend when possible.
                    current = resolution.current_backend_id
                    alternates = [b for b in allowed if b != current]
                    backend = alternates[0] if alternates else allowed[0]
                    model = None
                    if pools.get(backend):
                        model = sorted(pools[backend])[0]
                    if not alternates and backend == current and not model:
                        rejection_c = "NO_ELIGIBLE_FUTURE_AGENT_NODE"
                        continue
                    edits_c.append(
                        PendingBackendAssignmentEdit(
                            subtask_id=sid,
                            node_id=resolution.node_id,
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
                    state=state,
                    preview_materialize=True,
                )
            )
        elif rejection_c and (
            GlobalDiagnosisReason.BACKEND_INSTABILITY in diagnosis.reasons
            or GlobalDiagnosisReason.BUDGET_PRESSURE in diagnosis.reasons
            or GlobalDiagnosisReason.HARNESS_INSTABILITY in diagnosis.reasons
        ):
            candidates.append(
                GlobalCandidate(
                    candidate_id="cand_backend",
                    diagnosis=diagnosis,
                    edits=[],
                    proposed_task_plan=task_plan,
                    proposed_communication_plan=communication_plan,
                    proposed_scheduling_policy=scheduling_policy,
                    validation_status=GlobalCandidateValidationStatus.REJECTED,
                    rejection_reason=rejection_c,
                    heuristic_score=0.0,
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
        *,
        state: TaskExecutionState,
        preview_materialize: bool = False,
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
        rejection_reason = (
            ("rejected ineligible edits: " + ",".join(rejected)) if rejected else None
        )

        # Required payload feasibility: required max_tokens must fit target budget.
        if status is GlobalCandidateValidationStatus.VALID:
            for target, budget in new_comm.context_budgets.items():
                required = [
                    c
                    for c in new_comm.payload_contracts
                    if c.target_subtask_id == target and c.is_required()
                ]
                needed = sum(c.max_tokens for c in required)
                if budget > 0 and needed > budget:
                    status = GlobalCandidateValidationStatus.INVALID
                    rejection_reason = (
                        f"CONTEXT_BUDGET_INFEASIBLE: required payloads need "
                        f"{needed} > budget {budget} for {target}"
                    )
                    break

        if (
            preview_materialize
            and status is GlobalCandidateValidationStatus.VALID
        ):
            by_new = {s.subtask_id: s for s in new_plan.subtasks}
            for edit in edits:
                if not isinstance(edit, PendingBackendAssignmentEdit):
                    continue
                sub = by_new.get(edit.subtask_id)
                if sub is None:
                    continue
                try:
                    self.materializer.materialize(
                        subtask=sub,
                        revision_id="preview",
                        allowed_backend_pools=self.config.allowed_backend_assignments,
                        backend_model_pools=self.config.backend_model_pools,
                    )
                except GraphMaterializationError as exc:
                    status = GlobalCandidateValidationStatus.INVALID
                    rejection_reason = f"materialization_preview_failed:{exc}"
                    break

        del state
        return GlobalCandidate(
            candidate_id=candidate_id,
            diagnosis=diagnosis,
            edits=edits,
            proposed_task_plan=new_plan,
            proposed_communication_plan=new_comm,
            proposed_scheduling_policy=new_policy,
            validation_status=status,
            rejection_reason=rejection_reason,
            heuristic_score=float(len(edits)),
        )


def eligible_future_subtask_ids(state: TaskExecutionState) -> set[str]:
    return {
        sid
        for sid, sub in state.subtasks.items()
        if sub.status in {SubtaskStatus.PENDING, SubtaskStatus.READY}
        and sub.lease_status == "unleased"
    }
