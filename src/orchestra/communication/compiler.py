"""Compile CommunicationPlan into an indexed structure for delivery."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.aggregation import AggregationRule
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.validation import validate_communication_plan
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


def communication_plan_hash(plan: CommunicationPlan) -> str:
    payload = plan.model_dump(mode="json")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class CommunicationPlanCompiler:
    def compile(
        self,
        *,
        task_plan: TaskPlan,
        communication_plan: CommunicationPlan,
        completed_subtask_ids: set[str] | None = None,
    ) -> CompiledCommunicationPlan:
        validate_communication_plan(
            task_plan=task_plan,
            communication_plan=communication_plan,
            completed_subtask_ids=completed_subtask_ids,
        )
        by_target: dict[str, list[PayloadContract]] = {}
        by_id: dict[str, PayloadContract] = {}
        for contract in sorted(
            communication_plan.payload_contracts, key=lambda c: c.payload_id
        ):
            by_id[contract.payload_id] = contract
            by_target.setdefault(contract.target_subtask_id, []).append(contract)

        rules_by_payload: dict[str, list[DeliveryRule]] = {}
        for rule in sorted(
            communication_plan.delivery_schedule, key=lambda r: r.rule_id
        ):
            rules_by_payload.setdefault(rule.payload_id, []).append(rule)

        agg_by_target: dict[str, list[AggregationRule]] = {}
        for rule in communication_plan.aggregation_rules:
            # Aggregation targets terminal or first source's consumers — store by rule_id key.
            key = rule.metadata.get("target_subtask_id") or rule.rule_id
            agg_by_target.setdefault(str(key), []).append(rule)

        return CompiledCommunicationPlan(
            version=communication_plan.version,
            plan_hash=communication_plan_hash(communication_plan),
            payloads_by_target=by_target,
            delivery_rules_by_payload=rules_by_payload,
            aggregation_rules_by_target=agg_by_target,
            context_budgets=dict(communication_plan.context_budgets),
            payloads_by_id=by_id,
        )
