"""Rule-driven CommunicationPlan delivery engine with ledger replay."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.aggregation import (
    AggregationConflictError,
    AggregationInput,
    aggregate_payloads,
)
from orchestra.communication.budget import (
    ContextBudgetError,
    FinalDeliveryUnit,
    pack_final_delivery_units,
)
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
    estimate_tokens,
    project_payload,
)
from orchestra.communication.rule_evaluator import (
    DeliveryEvaluationContext,
    DeliveryRuleDecision,
    DeliveryRuleEvaluator,
)
from orchestra.communication.validation import CommunicationValidationMode
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
    final_units: list[FinalDeliveryUnit] = Field(default_factory=list)


class DeliveryPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_subtask_id: str
    deliverable: bool
    blocked: bool = False
    block_reason: DeliveryFailureReason | None = None
    projected_delivery_ids: list[str] = Field(default_factory=list)
    estimated_context_tokens: int = 0
    requires_mutation: bool = False
    batch: DeliveryBatchResult | None = None


class RuleEvaluationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    decision: DeliveryRuleDecision
    reason: str = ""


class RuleSetEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_rule_id: str | None = None
    decisions: list[RuleEvaluationRecord] = Field(default_factory=list)
    satisfied: bool = False
    required_block_reason: DeliveryFailureReason | None = None


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
        target_subtask_id: str | None = None,
    ) -> CompiledCommunicationPlan:
        del task_state
        return self.compiler.compile(
            task_plan=task_plan,
            communication_plan=communication_plan,
            target_subtask_id=target_subtask_id,
            validation_mode=CommunicationValidationMode.ACTIVE_EXECUTION,
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

    def _evaluate_rule_set(
        self,
        *,
        rules: list[DeliveryRule],
        contract: PayloadContract,
        context: DeliveryEvaluationContext,
    ) -> RuleSetEvaluationResult:
        enabled = sorted(rules, key=lambda r: (-r.priority, r.rule_id))
        enabled = [r for r in enabled if r.enabled]
        decisions: list[RuleEvaluationRecord] = []
        if not enabled:
            reason = (
                DeliveryFailureReason.REQUIRED_RULE_MISSING
                if contract.is_required()
                else None
            )
            return RuleSetEvaluationResult(
                decisions=decisions,
                satisfied=False,
                required_block_reason=reason,
            )
        saw_condition_false = False
        saw_unsupported = False
        for rule in enabled:
            result = self.rule_evaluator.evaluate(
                rule=rule, contract=contract, context=context
            )
            decisions.append(
                RuleEvaluationRecord(
                    rule_id=rule.rule_id,
                    decision=result.decision,
                    reason=result.reason,
                )
            )
            if result.decision is DeliveryRuleDecision.SATISFIED:
                return RuleSetEvaluationResult(
                    selected_rule_id=rule.rule_id,
                    decisions=decisions,
                    satisfied=True,
                )
            if result.decision is DeliveryRuleDecision.SKIPPED_CONDITION_FALSE:
                saw_condition_false = True
            if result.decision is DeliveryRuleDecision.UNSUPPORTED_TRIGGER:
                saw_unsupported = True
        if contract.is_required():
            if saw_condition_false and not any(
                d.decision
                in {
                    DeliveryRuleDecision.SKIPPED_TRIGGER_NOT_MET,
                }
                for d in decisions
            ):
                # All enabled rules evaluated; at least one condition-false and
                # none satisfied → required block.
                block = DeliveryFailureReason.REQUIRED_CONDITION_UNSATISFIED
            elif saw_unsupported and all(
                d.decision
                in {
                    DeliveryRuleDecision.UNSUPPORTED_TRIGGER,
                    DeliveryRuleDecision.SKIPPED_CONDITION_FALSE,
                }
                for d in decisions
            ):
                block = DeliveryFailureReason.UNSUPPORTED_TRIGGER
            elif context.source_status is not SubtaskStatus.COMMITTED:
                block = DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED
            elif not context.source_has_committed_artifact:
                block = DeliveryFailureReason.REQUIRED_ARTIFACT_MISSING
            elif saw_condition_false:
                block = DeliveryFailureReason.REQUIRED_CONDITION_UNSATISFIED
            else:
                block = DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED
            return RuleSetEvaluationResult(
                decisions=decisions,
                satisfied=False,
                required_block_reason=block,
            )
        return RuleSetEvaluationResult(decisions=decisions, satisfied=False)

    def _find_aggregation_rule(
        self,
        compiled: CompiledCommunicationPlan,
        *,
        target_subtask_id: str,
        slot: str,
        payload_ids: list[str],
        source_subtask_ids: set[str],
    ):
        """Exact-set aggregation matching (no partial intersection).

        Returns the unique exact match, or None. Raises DeliveryEngineError on
        ambiguous exact matches.
        """
        actual_payloads = set(payload_ids)
        actual_sources = set(source_subtask_ids)
        candidates = []
        for rules in compiled.aggregation_rules_by_target.values():
            candidates.extend(rules)
        # Deduplicate by rule_id (same rule may appear under multiple keys).
        by_id = {r.rule_id: r for r in candidates}
        exact = []
        slot_target_hits = []
        for rule in by_id.values():
            rule_target = str(rule.metadata.get("target_subtask_id") or "")
            rule_slot = rule.target_slot or str(rule.metadata.get("slot") or "")
            if rule_target and rule_target != target_subtask_id:
                continue
            if rule_slot and rule_slot != slot:
                continue
            if not rule_target and not rule_slot:
                # Infer target from compiled payload index when possible.
                if target_subtask_id not in compiled.aggregation_rules_by_target:
                    if rule not in compiled.aggregation_rules_by_target.get(
                        target_subtask_id, []
                    ):
                        # Still allow exact payload/source set match without meta.
                        pass
            slot_target_hits.append(rule)
            payload_ok = True
            source_ok = True
            if rule.source_payload_ids:
                payload_ok = set(rule.source_payload_ids) == actual_payloads
            if rule.source_subtask_ids:
                source_ok = set(rule.source_subtask_ids) == actual_sources
            if not rule.source_payload_ids and not rule.source_subtask_ids:
                continue
            if rule.source_payload_ids and rule.source_subtask_ids:
                if payload_ok and source_ok:
                    exact.append(rule)
            elif rule.source_payload_ids:
                if payload_ok:
                    exact.append(rule)
            elif rule.source_subtask_ids:
                if source_ok:
                    exact.append(rule)
        if len(exact) > 1:
            raise DeliveryEngineError(
                "AMBIGUOUS_AGGREGATION_RULE: multiple exact matches "
                f"for target={target_subtask_id} slot={slot}: "
                + ",".join(sorted(r.rule_id for r in exact)),
                reason=DeliveryFailureReason.AMBIGUOUS_AGGREGATION_RULE,
                target_subtask_id=target_subtask_id,
            )
        if len(exact) == 1:
            return exact[0]
        if slot_target_hits and any(
            (r.source_payload_ids or r.source_subtask_ids) for r in slot_target_hits
        ):
            # Target/slot had candidate rules but input sets mismatched.
            # Callers map None → AGGREGATION_RULE_MISSING; expose mismatch via
            # optional attribute for diagnosis.
            return None
        return None

    async def deliver_for_target(
        self,
        *,
        task_plan: TaskPlan,
        task_state: TaskExecutionState,
        communication_plan: CommunicationPlan,
        target_subtask_id: str,
        persist: bool = True,
    ) -> DeliveryBatchResult:
        compiled = self.compile(
            task_plan=task_plan,
            communication_plan=communication_plan,
            task_state=task_state,
            target_subtask_id=target_subtask_id,
        )
        contracts = compiled.payloads_by_target.get(target_subtask_id, [])
        projections: list[PayloadProjectionResult] = []
        ledger = list(getattr(task_state, "delivery_ledger", []) or [])
        new_records: list[DeliveryRecord] = []
        audit_records: list[DeliveryRecord] = []
        pending_by_slot: dict[
            str, list[tuple[PayloadContract, DeliveryRule, PayloadProjectionResult]]
        ] = {}

        target = task_state.subtasks.get(target_subtask_id)
        target_status = target.status if target else SubtaskStatus.PENDING
        target_leased = bool(target and target.lease_status == "leased")

        for contract in sorted(contracts, key=lambda c: c.payload_id):
            rules = compiled.delivery_rules_by_payload.get(contract.payload_id, [])
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
            ruleset = self._evaluate_rule_set(
                rules=rules, contract=contract, context=ctx
            )
            for rec in ruleset.decisions:
                if rec.decision is DeliveryRuleDecision.SKIPPED_CONDITION_FALSE:
                    audit_records.append(
                        DeliveryRecord(
                            delivery_id=f"del-{uuid.uuid4().hex[:12]}",
                            communication_plan_version=compiled.version,
                            rule_id=rec.rule_id,
                            payload_id=contract.payload_id,
                            source_subtask_id=contract.source_subtask_id,
                            target_subtask_id=target_subtask_id,
                            source_artifact_id="",
                            delivered_at_state_version=task_state.state_version,
                            status=DeliveryStatus.SKIPPED_CONDITION_FALSE,
                        )
                    )
            if not ruleset.satisfied:
                enabled = [r for r in rules if r.enabled]
                if not enabled:
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
                        block_reason=ruleset.required_block_reason
                        or DeliveryFailureReason.REQUIRED_RULE_MISSING,
                    )
                continue

            assert ruleset.selected_rule_id is not None
            rule = next(r for r in rules if r.rule_id == ruleset.selected_rule_id)
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

            if persist:
                await self.artifact_store.put(proj.projected_artifact)
            projections.append(proj)
            pending_by_slot.setdefault(slot, []).append((contract, rule, proj))

        # Build final delivery units (aggregate when needed).
        units: list[FinalDeliveryUnit] = []
        unit_members: dict[
            str, list[tuple[PayloadContract, DeliveryRule, PayloadProjectionResult]]
        ] = {}

        for slot, rows in sorted(pending_by_slot.items()):
            if len(rows) == 1:
                contract, rule, proj = rows[0]
                unit_id = f"unit:{contract.payload_id}"
                tokens = proj.final_estimated_tokens or proj.estimated_tokens
                # Prefer estimate of the injected artifact payload.
                tokens = estimate_tokens(proj.projected_artifact.payload)
                units.append(
                    FinalDeliveryUnit(
                        delivery_unit_id=unit_id,
                        target_subtask_id=target_subtask_id,
                        target_slot=slot,
                        source_payload_ids=[contract.payload_id],
                        source_artifact_ids=[proj.source_artifact_id],
                        artifact=proj.projected_artifact,
                        estimated_tokens=tokens,
                        required=contract.is_required(),
                        priority=int(contract.metadata.get("priority", 100)),
                    )
                )
                unit_members[unit_id] = rows
                continue

            payload_ids = [c.payload_id for c, _, _ in rows]
            source_ids = {c.source_subtask_id for c, _, _ in rows}
            required_any = any(c.is_required() for c, _, _ in rows)
            try:
                agg_rule = self._find_aggregation_rule(
                    compiled,
                    target_subtask_id=target_subtask_id,
                    slot=slot,
                    payload_ids=payload_ids,
                    source_subtask_ids=source_ids,
                )
            except DeliveryEngineError as exc:
                return DeliveryBatchResult(
                    target_subtask_id=target_subtask_id,
                    projections=projections,
                    audit_records=audit_records,
                    blocked=True,
                    block_reason=exc.reason,
                )
            if agg_rule is None:
                return DeliveryBatchResult(
                    target_subtask_id=target_subtask_id,
                    projections=projections,
                    audit_records=audit_records,
                    blocked=True,
                    block_reason=DeliveryFailureReason.AGGREGATION_RULE_MISSING,
                )
            if required_any:
                # All required source payloads for this slot must be present.
                required_ids = {
                    c.payload_id for c, _, _ in rows if c.is_required()
                }
                if not required_ids.issubset(set(payload_ids)):
                    return DeliveryBatchResult(
                        target_subtask_id=target_subtask_id,
                        projections=projections,
                        audit_records=audit_records,
                        blocked=True,
                        block_reason=(
                            DeliveryFailureReason.AGGREGATION_REQUIRED_INPUT_MISSING
                        ),
                    )
            # Replay aggregated artifact if a prior aggregated delivery exists.
            agg_payload_id = f"agg:{agg_rule.rule_id}"
            prior_agg = find_delivered_record(
                ledger,
                communication_plan_version=compiled.version,
                rule_id=agg_rule.rule_id,
                payload_id=agg_payload_id,
                source_artifact_id="+".join(
                    sorted(p.source_artifact_id for _, _, p in rows)
                ),
                target_subtask_id=target_subtask_id,
                target_slot=slot,
            )
            if prior_agg is not None:
                agg_art = await load_prior_delivery(
                    record=prior_agg, artifact_store=self.artifact_store
                )
                tokens = prior_agg.estimated_tokens or estimate_tokens(agg_art.payload)
            else:
                try:
                    agg = aggregate_payloads(
                        rule=agg_rule.model_copy(
                            update={"target_slot": slot or agg_rule.target_slot}
                        ),
                        inputs=AggregationInput(
                            source_payload_ids=[c.payload_id for c, _, _ in rows],
                            source_artifact_ids=[
                                p.source_artifact_id for _, _, p in rows
                            ],
                            projected_artifacts=[
                                p.projected_artifact for _, _, p in rows
                            ],
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
                if persist:
                    await self.artifact_store.put(agg.aggregated_artifact)
                agg_art = agg.aggregated_artifact
                tokens = agg.estimated_tokens
            unit_id = f"unit:{agg_payload_id}:{slot}"
            units.append(
                FinalDeliveryUnit(
                    delivery_unit_id=unit_id,
                    target_subtask_id=target_subtask_id,
                    target_slot=slot,
                    source_payload_ids=payload_ids,
                    source_artifact_ids=[p.source_artifact_id for _, _, p in rows],
                    artifact=agg_art,
                    estimated_tokens=tokens,
                    required=required_any,
                    priority=min(
                        int(c.metadata.get("priority", 100)) for c, _, _ in rows
                    ),
                    aggregation_rule_id=agg_rule.rule_id,
                )
            )
            unit_members[unit_id] = rows

        max_tokens = int(compiled.context_budgets.get(target_subtask_id, 10_000_000))
        try:
            budget = pack_final_delivery_units(
                target_subtask_id=target_subtask_id,
                max_tokens=max_tokens,
                units=units,
            )
        except ContextBudgetError:
            return DeliveryBatchResult(
                target_subtask_id=target_subtask_id,
                projections=projections,
                audit_records=audit_records,
                blocked=True,
                block_reason=DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE,
                final_units=units,
            )

        included = set(budget.included_unit_ids)
        delivered_slots: dict[str, ArtifactEnvelope] = {}
        for unit in units:
            if unit.delivery_unit_id not in included:
                continue
            delivered_slots[unit.target_slot] = unit.artifact
            rows = unit_members.get(unit.delivery_unit_id, [])
            if unit.aggregation_rule_id:
                source_key = "+".join(sorted(unit.source_artifact_ids))
                prior = find_delivered_record(
                    ledger,
                    communication_plan_version=compiled.version,
                    rule_id=unit.aggregation_rule_id,
                    payload_id=f"agg:{unit.aggregation_rule_id}",
                    source_artifact_id=source_key,
                    target_subtask_id=target_subtask_id,
                    target_slot=unit.target_slot,
                )
                if prior is None and persist:
                    new_records.append(
                        DeliveryRecord(
                            delivery_id=f"del-{uuid.uuid4().hex[:12]}",
                            communication_plan_version=compiled.version,
                            rule_id=unit.aggregation_rule_id,
                            payload_id=f"agg:{unit.aggregation_rule_id}",
                            source_subtask_id=rows[0][0].source_subtask_id
                            if rows
                            else "",
                            target_subtask_id=target_subtask_id,
                            source_artifact_id=source_key,
                            projected_artifact_id=unit.artifact.artifact_id,
                            projected_artifact_hash=unit.artifact.content_hash,
                            target_slot=unit.target_slot,
                            delivered_at_state_version=task_state.state_version,
                            status=DeliveryStatus.DELIVERED,
                            estimated_tokens=unit.estimated_tokens,
                        )
                    )
            for contract, rule, proj in rows:
                prior = find_delivered_record(
                    ledger,
                    communication_plan_version=compiled.version,
                    rule_id=rule.rule_id,
                    payload_id=contract.payload_id,
                    source_artifact_id=proj.source_artifact_id,
                    target_subtask_id=target_subtask_id,
                    target_slot=unit.target_slot,
                )
                if prior is not None or not persist:
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
                        target_slot=unit.target_slot,
                        delivered_at_state_version=task_state.state_version,
                        status=DeliveryStatus.DELIVERED,
                        estimated_tokens=proj.final_estimated_tokens
                        or proj.estimated_tokens,
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
            final_units=units,
        )

    async def preflight_for_target(
        self,
        *,
        task_plan: TaskPlan,
        task_state: TaskExecutionState,
        communication_plan: CommunicationPlan,
        target_subtask_id: str,
        persist_projection: bool = True,
    ) -> DeliveryPreflightResult:
        """Validate/deliver communication before lease (recommended: persist)."""
        batch = await self.deliver_for_target(
            task_plan=task_plan,
            task_state=task_state,
            communication_plan=communication_plan,
            target_subtask_id=target_subtask_id,
            persist=persist_projection,
        )
        if batch.blocked:
            return DeliveryPreflightResult(
                target_subtask_id=target_subtask_id,
                deliverable=False,
                blocked=True,
                block_reason=batch.block_reason,
                requires_mutation=persist_projection and bool(batch.new_records),
                batch=batch,
            )
        used = 0
        if batch.budget is not None and hasattr(batch.budget, "used_tokens"):
            used = int(batch.budget.used_tokens)
        return DeliveryPreflightResult(
            target_subtask_id=target_subtask_id,
            deliverable=True,
            blocked=False,
            projected_delivery_ids=[r.delivery_id for r in batch.new_records],
            estimated_context_tokens=used,
            requires_mutation=persist_projection and bool(batch.new_records),
            batch=batch,
        )
