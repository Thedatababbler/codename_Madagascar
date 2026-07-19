"""Compile CommunicationPlan into an indexed structure for delivery."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.aggregation import AggregationRule
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.validation import (
    CommunicationValidationMode,
    validate_communication_plan,
)
from orchestra.decomposition.schemas import TaskPlan


class CompiledCommunicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    plan_hash: str
    payloads_by_target: dict[str, list[PayloadContract]] = Field(default_factory=dict)
    delivery_rules_by_payload: dict[str, list[DeliveryRule]] = Field(default_factory=dict)
    aggregation_rules_by_target: dict[str, list[AggregationRule]] = Field(
        default_factory=dict
    )
    context_budgets: dict[str, int] = Field(default_factory=dict)
    payloads_by_id: dict[str, PayloadContract] = Field(default_factory=dict)
    scoped_target_subtask_id: str | None = None


def communication_plan_hash(plan: CommunicationPlan) -> str:
    payload = plan.model_dump(mode="json")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _aggregation_target(
    rule: AggregationRule,
    payloads_by_id: dict[str, PayloadContract],
) -> str | None:
    meta = str(rule.metadata.get("target_subtask_id") or "")
    if meta:
        return meta
    targets: set[str] = set()
    for pid in rule.source_payload_ids:
        contract = payloads_by_id.get(pid)
        if contract is not None:
            targets.add(contract.target_subtask_id)
    if len(targets) == 1:
        return next(iter(targets))
    return None


class CommunicationPlanCompiler:
    def compile(
        self,
        *,
        task_plan: TaskPlan,
        communication_plan: CommunicationPlan,
        target_subtask_id: str | None = None,
        validation_mode: CommunicationValidationMode | str = (
            CommunicationValidationMode.STRUCTURAL
        ),
        current_state: Any | None = None,
        parent_communication_plan: CommunicationPlan | None = None,
        completed_subtask_ids: set[str] | None = None,
    ) -> CompiledCommunicationPlan:
        del completed_subtask_ids
        validate_communication_plan(
            task_plan=task_plan,
            communication_plan=communication_plan,
            mode=validation_mode,
            current_state=current_state,
            parent_communication_plan=parent_communication_plan,
        )

        # Full-plan hash/version always (even when scoped).
        full_hash = communication_plan_hash(communication_plan)
        by_id: dict[str, PayloadContract] = {
            c.payload_id: c for c in communication_plan.payload_contracts
        }

        contracts = list(communication_plan.payload_contracts)
        if target_subtask_id is not None:
            contracts = [c for c in contracts if c.target_subtask_id == target_subtask_id]

        by_target: dict[str, list[PayloadContract]] = {}
        scoped_ids: dict[str, PayloadContract] = {}
        for contract in sorted(contracts, key=lambda c: c.payload_id):
            scoped_ids[contract.payload_id] = contract
            by_target.setdefault(contract.target_subtask_id, []).append(contract)

        payload_id_set = set(scoped_ids)
        rules_by_payload: dict[str, list[DeliveryRule]] = {}
        for rule in sorted(
            communication_plan.delivery_schedule, key=lambda r: r.rule_id
        ):
            if target_subtask_id is not None and rule.payload_id not in payload_id_set:
                continue
            rules_by_payload.setdefault(rule.payload_id, []).append(rule)

        agg_by_target: dict[str, list[AggregationRule]] = {}
        for rule in communication_plan.aggregation_rules:
            agg_target = _aggregation_target(rule, by_id)
            if target_subtask_id is not None:
                if agg_target is not None and agg_target != target_subtask_id:
                    continue
                if rule.source_payload_ids and not set(rule.source_payload_ids).issubset(
                    set(by_id)  # may reference full plan payloads
                ):
                    # Keep only if all source payloads target the scoped target.
                    src_targets = {
                        by_id[pid].target_subtask_id
                        for pid in rule.source_payload_ids
                        if pid in by_id
                    }
                    if src_targets and src_targets != {target_subtask_id}:
                        continue
                if (
                    agg_target is None
                    and rule.source_payload_ids
                    and not set(rule.source_payload_ids) & payload_id_set
                ):
                    continue
            key = agg_target or target_subtask_id or rule.rule_id
            agg_by_target.setdefault(str(key), []).append(rule)

        return CompiledCommunicationPlan(
            version=communication_plan.version,
            plan_hash=full_hash,
            payloads_by_target=by_target,
            delivery_rules_by_payload=rules_by_payload,
            aggregation_rules_by_target=agg_by_target,
            context_budgets=dict(communication_plan.context_budgets),
            payloads_by_id=scoped_ids if target_subtask_id is not None else by_id,
            scoped_target_subtask_id=target_subtask_id,
        )
