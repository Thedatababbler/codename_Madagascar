"""Rule-driven CommunicationPlan delivery engine with ledger replay."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.aggregation import (
    AggregationConflictError,
    AggregationInput,
    aggregate_payloads,
)
from orchestra.communication.budget import ContextBudgetError, pack_context_budget
from orchestra.communication.compiler import (
    CommunicationPlanCompiler,
    CompiledCommunicationPlan,
)
from orchestra.communication.ledger import (
    DeliveryFailureReason,
    DeliveryRecord,
    DeliveryStatus,
    find_delivered_record,
)
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.projection import (
    PayloadProjectionInfeasible,
    PayloadProjectionResult,
    project_payload,
)
from orchestra.communication.rule_evaluator import (
    DeliveryEvaluationContext,
    DeliveryRuleDecision,
    DeliveryRuleEvaluator,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan
from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.storage.artifacts import ArtifactStore

PRIVATE_TYPE_MARKERS = (
    "private",
    "hidden",
    "candidate",
    "discarded",
    "privateevaluator",
    "finallcb",
)


class DeliveryEngineError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason: DeliveryFailureReason,
        target_subtask_id: str,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.target_subtask_id = target_subtask_id


class DeliveryBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_subtask_id: str
    projections: list[PayloadProjectionResult] = Field(default_factory=list)
    budget: object | None = None
    new_records: list[DeliveryRecord] = Field(default_factory=list)
    delivered_slots: dict[str, ArtifactEnvelope] = Field(default_factory=dict)
    audit_records: list[DeliveryRecord] = Field(default_factory=list)
    blocked: bool = False
    block_reason: DeliveryFailureReason | None = None


def _is_private_or_candidate(art: ArtifactEnvelope) -> bool:
    at = (art.artifact_type or "").lower().replace("_", "")
    if any(m in at for m in PRIVATE_TYPE_MARKERS):
        return True
    producer = (art.producer_node_id or "").lower()
    return any(m in producer for m in ("private", "hidden", "candidate"))


async def load_prior_delivery(
    *,
    record: DeliveryRecord,
    artifact_store: ArtifactStore,
) -> ArtifactEnvelope:
    if not record.projected_artifact_id:
        raise DeliveryEngineError(
            "DELIVERY_LEDGER_CORRUPTION: missing projected_artifact_id",
            reason=DeliveryFailureReason.LEDGER_CORRUPTION,
            target_subtask_id=record.target_subtask_id,
        )
    try:
        art = await artifact_store.get(record.projected_artifact_id)
    except KeyError as exc:
        raise DeliveryEngineError(
            f"DELIVERY_LEDGER_CORRUPTION: projected artifact "
            f"{record.projected_artifact_id} missing",
            reason=DeliveryFailureReason.LEDGER_CORRUPTION,
            target_subtask_id=record.target_subtask_id,
        ) from exc
    if (
        record.projected_artifact_hash
        and art.content_hash != record.projected_artifact_hash
    ):
        raise DeliveryEngineError(
            "DELIVERY_LEDGER_CORRUPTION: projected artifact hash mismatch",
            reason=DeliveryFailureReason.LEDGER_CORRUPTION,
            target_subtask_id=record.target_subtask_id,
        )
    return art


class CommunicationDeliveryEngine:
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store
        self.compiler = CommunicationPlanCompiler()
        self.rule_evaluator = DeliveryRuleEvaluator()

    def compile(
        self,
        *,
        task_plan: TaskPlan,
        communication_plan: CommunicationPlan,
        task_state: TaskExecutionState,
    ) -> CompiledCommunicationPlan:
        completed = {
            sid
            for sid, sub in task_state.subtasks.items()
            if sub.status
            in {
                SubtaskStatus.COMMITTED,
                SubtaskStatus.FAILED,
                SubtaskStatus.SKIPPED,
            }
        }
        return self.compiler.compile(
            task_plan=task_plan,
            communication_plan=communication_plan,
            completed_subtask_ids=completed,
        )

    async def _resolve_source_artifact(
        self,
        *,
        contract: PayloadContract,
        task_state: TaskExecutionState,
    ) -> ArtifactEnvelope | None:
        source = task_state.subtasks.get(contract.source_subtask_id)
        if source is None or source.status is not SubtaskStatus.COMMITTED:
            return None
        for ref in source.committed_artifacts:
            if (
                not contract.artifact_type
                or ref.artifact_type == contract.artifact_type
            ):
                art = await self.artifact_store.get(ref.artifact_id)
                if _is_private_or_candidate(art):
                    continue
                return art
        if source.final_output_artifact_id:
            art = await self.artifact_store.get(source.final_output_artifact_id)
            if not _is_private_or_candidate(art):
                return art
        return None

    def _slot_for(self, contract: PayloadContract) -> str:
        return str(contract.metadata.get("slot") or f"comm:{contract.payload_id}")

    def _find_aggregation_rule(
        self,
        compiled: CompiledCommunicationPlan,
        *,
        target_subtask_id: str,
        slot: str,
        payload_ids: list[str],
        source_subtask_ids: set[str],
    ):
        candidates = []
        for rules in compiled.aggregation_rules_by_target.values():
            candidates.extend(rules)
        matched = []
        for rule in candidates:
            target_meta = str(rule.metadata.get("target_subtask_id") or "")
            rule_slot = rule.target_slot or str(rule.metadata.get("slot") or "")
            targets_here = (
                target_meta == target_subtask_id
                or rule_slot == slot
                or target_subtask_id in compiled.aggregation_rules_by_target
                and rule in compiled.aggregation_rules_by_target.get(target_subtask_id, [])
            )
            if not targets_here and target_meta and target_meta != target_subtask_id:
                continue
            if rule.source_payload_ids:
                if set(rule.source_payload_ids) & set(payload_ids):
                    matched.append(rule)
            elif rule.source_subtask_ids:
                if set(rule.source_subtask_ids) & source_subtask_ids:
                    matched.append(rule)
            elif targets_here:
                matched.append(rule)
        if not matched:
            return None
        return sorted(matched, key=lambda r: r.rule_id)[0]

    async def deliver_for_target(
        self,
        *,
        task_plan: TaskPlan,
        task_state: TaskExecutionState,
        communication_plan: CommunicationPlan,
        target_subtask_id: str,
    ) -> DeliveryBatchResult:
        compiled = self.compile(
            task_plan=task_plan,
            communication_plan=communication_plan,
            task_state=task_state,
        )
        contracts = compiled.payloads_by_target.get(target_subtask_id, [])
        projections: list[PayloadProjectionResult] = []
        ledger = list(getattr(task_state, "delivery_ledger", []) or [])
        new_records: list[DeliveryRecord] = []
        audit_records: list[DeliveryRecord] = []
        delivered_slots: dict[str, ArtifactEnvelope] = {}
        pending_by_slot: dict[
            str, list[tuple[PayloadContract, DeliveryRule, PayloadProjectionResult]]
        ] = {}

        target = task_state.subtasks.get(target_subtask_id)
        target_status = target.status if target else SubtaskStatus.PENDING
        target_leased = bool(target and target.lease_status == "leased")

        for contract in sorted(contracts, key=lambda c: c.payload_id):
            rules = compiled.delivery_rules_by_payload.get(contract.payload_id, [])
            enabled_rules = [r for r in rules if r.enabled]
            if not enabled_rules:
                audit_records.append(
                    DeliveryRecord(
                        delivery_id=f"del-{uuid.uuid4().hex[:12]}",
                        communication_plan_version=compiled.version,
                        rule_id="",
                        payload_id=contract.payload_id,
                        source_subtask_id=contract.source_subtask_id,
                        target_subtask_id=target_subtask_id,
                        source_artifact_id="",
                        delivered_at_state_version=task_state.state_version,
                        status=DeliveryStatus.SKIPPED_NO_RULE,
                        failure_reason=(
                            DeliveryFailureReason.REQUIRED_RULE_MISSING
                            if contract.is_required()
                            else None
                        ),
                    )
                )
                if contract.is_required():
                    return DeliveryBatchResult(
                        target_subtask_id=target_subtask_id,
                        audit_records=audit_records,
                        blocked=True,
                        block_reason=DeliveryFailureReason.REQUIRED_RULE_MISSING,
                    )
                continue

            rule = sorted(enabled_rules, key=lambda r: (-r.priority, r.rule_id))[0]
            source = task_state.subtasks.get(contract.source_subtask_id)
            source_status = source.status if source else SubtaskStatus.PENDING
            source_art = await self._resolve_source_artifact(
                contract=contract, task_state=task_state
            )
            ctx = DeliveryEvaluationContext(
                task_id=task_state.task_id,
                state_version=task_state.state_version,
                source_subtask_id=contract.source_subtask_id,
                target_subtask_id=target_subtask_id,
                source_status=source_status,
                target_status=target_status,
                source_artifact_ids=(
                    [source_art.artifact_id] if source_art is not None else []
                ),
                active_communication_version=compiled.version,
                target_leased=target_leased,
                source_has_committed_artifact=source_art is not None,
            )
            decision = self.rule_evaluator.evaluate(
                rule=rule, contract=contract, context=ctx
            )
            if decision.decision is DeliveryRuleDecision.SKIPPED_CONDITION_FALSE:
                audit_records.append(
                    DeliveryRecord(
                        delivery_id=f"del-{uuid.uuid4().hex[:12]}",
                        communication_plan_version=compiled.version,
                        rule_id=rule.rule_id,
                        payload_id=contract.payload_id,
                        source_subtask_id=contract.source_subtask_id,
                        target_subtask_id=target_subtask_id,
                        source_artifact_id="",
                        delivered_at_state_version=task_state.state_version,
                        status=DeliveryStatus.SKIPPED_CONDITION_FALSE,
                    )
                )
                continue
            if decision.decision is DeliveryRuleDecision.UNSUPPORTED_TRIGGER:
                if contract.is_required():
                    return DeliveryBatchResult(
                        target_subtask_id=target_subtask_id,
                        audit_records=audit_records,
                        blocked=True,
                        block_reason=DeliveryFailureReason.UNSUPPORTED_TRIGGER,
                    )
                continue
            if decision.decision is not DeliveryRuleDecision.SATISFIED:
                if contract.is_required():
                    reason = (
                        DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED
                        if source_status is not SubtaskStatus.COMMITTED
                        else DeliveryFailureReason.REQUIRED_ARTIFACT_MISSING
                    )
                    return DeliveryBatchResult(
                        target_subtask_id=target_subtask_id,
                        audit_records=audit_records,
                        blocked=True,
                        block_reason=reason,
                    )
                continue

            assert source_art is not None
            slot = self._slot_for(contract)
            prior = find_delivered_record(
                ledger,
                communication_plan_version=compiled.version,
                rule_id=rule.rule_id,
                payload_id=contract.payload_id,
                source_artifact_id=source_art.artifact_id,
                target_subtask_id=target_subtask_id,
                target_slot=slot,
            )
            if prior is not None:
                replayed = await load_prior_delivery(
                    record=prior, artifact_store=self.artifact_store
                )
                proj = PayloadProjectionResult(
                    payload_id=contract.payload_id,
                    source_artifact_id=source_art.artifact_id,
                    projected_artifact=replayed,
                    estimated_tokens=prior.estimated_tokens or 1,
                    truncated=prior.truncated,
                    final_estimated_tokens=prior.estimated_tokens or 1,
                    original_estimated_tokens=prior.estimated_tokens or 1,
                )
                projections.append(proj)
                pending_by_slot.setdefault(slot, []).append((contract, rule, proj))
                continue

            try:
                proj = project_payload(
                    contract=contract,
                    source=source_art,
                    task_id=task_state.task_id,
                )
            except PayloadProjectionInfeasible as exc:
                if contract.is_required():
                    return DeliveryBatchResult(
                        target_subtask_id=target_subtask_id,
                        audit_records=audit_records,
                        blocked=True,
                        block_reason=exc.reason,
                    )
                audit_records.append(
                    DeliveryRecord(
                        delivery_id=f"del-{uuid.uuid4().hex[:12]}",
                        communication_plan_version=compiled.version,
                        rule_id=rule.rule_id,
                        payload_id=contract.payload_id,
                        source_subtask_id=contract.source_subtask_id,
                        target_subtask_id=target_subtask_id,
                        source_artifact_id=source_art.artifact_id,
                        delivered_at_state_version=task_state.state_version,
                        status=DeliveryStatus.FAILED,
                        failure_reason=exc.reason,
                    )
                )
                continue

            await self.artifact_store.put(proj.projected_artifact)
            projections.append(proj)
            pending_by_slot.setdefault(slot, []).append((contract, rule, proj))

        # Resolve slots (aggregation when multiple payloads share a slot).
        budget_projections: list[PayloadProjectionResult] = []
        slot_bindings: list[
            tuple[str, DeliveryRule, PayloadContract, PayloadProjectionResult]
        ] = []

        for slot, rows in sorted(pending_by_slot.items()):
            if len(rows) == 1:
                contract, rule, proj = rows[0]
                budget_projections.append(proj)
                slot_bindings.append((slot, rule, contract, proj))
                continue

            payload_ids = [c.payload_id for c, _, _ in rows]
            source_ids = {c.source_subtask_id for c, _, _ in rows}
            agg_rule = self._find_aggregation_rule(
                compiled,
                target_subtask_id=target_subtask_id,
                slot=slot,
                payload_ids=payload_ids,
                source_subtask_ids=source_ids,
            )
            if agg_rule is None:
                return DeliveryBatchResult(
                    target_subtask_id=target_subtask_id,
                    projections=projections,
                    audit_records=audit_records,
                    blocked=True,
                    block_reason=DeliveryFailureReason.AGGREGATION_RULE_MISSING,
                )
            try:
                agg = aggregate_payloads(
                    rule=agg_rule.model_copy(
                        update={"target_slot": slot or agg_rule.target_slot}
                    ),
                    inputs=AggregationInput(
                        source_payload_ids=[c.payload_id for c, _, _ in rows],
                        source_artifact_ids=[p.source_artifact_id for _, _, p in rows],
                        projected_artifacts=[p.projected_artifact for _, _, p in rows],
                    ),
                    task_id=task_state.task_id,
                )
            except AggregationConflictError:
                return DeliveryBatchResult(
                    target_subtask_id=target_subtask_id,
                    projections=projections,
                    audit_records=audit_records,
                    blocked=True,
                    block_reason=DeliveryFailureReason.AGGREGATION_CONFLICT,
                )
            await self.artifact_store.put(agg.aggregated_artifact)
            delivered_slots[slot] = agg.aggregated_artifact
            for contract, rule, proj in rows:
                budget_projections.append(proj)
                slot_bindings.append((slot, rule, contract, proj))

        try:
            budget = pack_context_budget(
                target_subtask_id=target_subtask_id,
                compiled=compiled,
                projections=budget_projections or projections,
            )
        except ContextBudgetError:
            return DeliveryBatchResult(
                target_subtask_id=target_subtask_id,
                projections=projections,
                audit_records=audit_records,
                blocked=True,
                block_reason=DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE,
            )

        included = set(budget.included_payload_ids)
        for slot, rule, contract, proj in slot_bindings:
            if slot in delivered_slots:
                # Aggregated slot already filled; still ledger member deliveries.
                pass
            elif proj.payload_id not in included:
                if contract.is_required():
                    return DeliveryBatchResult(
                        target_subtask_id=target_subtask_id,
                        projections=projections,
                        budget=budget,
                        audit_records=audit_records,
                        blocked=True,
                        block_reason=DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE,
                    )
                continue
            else:
                delivered_slots[slot] = proj.projected_artifact

            prior = find_delivered_record(
                ledger,
                communication_plan_version=compiled.version,
                rule_id=rule.rule_id,
                payload_id=contract.payload_id,
                source_artifact_id=proj.source_artifact_id,
                target_subtask_id=target_subtask_id,
                target_slot=slot,
            )
            if prior is not None:
                continue
            new_records.append(
                DeliveryRecord(
                    delivery_id=f"del-{uuid.uuid4().hex[:12]}",
                    communication_plan_version=compiled.version,
                    rule_id=rule.rule_id,
                    payload_id=contract.payload_id,
                    source_subtask_id=contract.source_subtask_id,
                    target_subtask_id=target_subtask_id,
                    source_artifact_id=proj.source_artifact_id,
                    projected_artifact_id=proj.projected_artifact.artifact_id,
                    projected_artifact_hash=proj.projected_artifact.content_hash,
                    target_slot=slot,
                    delivered_at_state_version=task_state.state_version,
                    status=DeliveryStatus.DELIVERED,
                    estimated_tokens=proj.final_estimated_tokens or proj.estimated_tokens,
                    truncated=proj.truncated,
                )
            )

        return DeliveryBatchResult(
            target_subtask_id=target_subtask_id,
            projections=projections,
            budget=budget,
            new_records=new_records,
            delivered_slots=delivered_slots,
            audit_records=audit_records,
        )
