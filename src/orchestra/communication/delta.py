"""CommunicationPlan delta, target resolution, and immutable-history helpers."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.plan import CommunicationPlan


class CommunicationPlanDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    added_payload_ids: set[str] = Field(default_factory=set)
    changed_payload_ids: set[str] = Field(default_factory=set)
    removed_payload_ids: set[str] = Field(default_factory=set)

    added_rule_ids: set[str] = Field(default_factory=set)
    changed_rule_ids: set[str] = Field(default_factory=set)
    removed_rule_ids: set[str] = Field(default_factory=set)

    added_aggregation_rule_ids: set[str] = Field(default_factory=set)
    changed_aggregation_rule_ids: set[str] = Field(default_factory=set)
    removed_aggregation_rule_ids: set[str] = Field(default_factory=set)

    changed_context_budget_targets: set[str] = Field(default_factory=set)


class AggregationTargetAmbiguous(ValueError):
    """Aggregation rule cannot be resolved to a unique target."""


def _stable_dump(obj: Any) -> str:
    if hasattr(obj, "model_dump"):
        payload = obj.model_dump(mode="json")
    else:
        payload = obj
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _diff_by_id(
    parent_items: list[Any],
    proposed_items: list[Any],
    *,
    id_attr: str,
) -> tuple[set[str], set[str], set[str]]:
    parent_map = {getattr(x, id_attr): x for x in parent_items}
    proposed_map = {getattr(x, id_attr): x for x in proposed_items}
    added = set(proposed_map) - set(parent_map)
    removed = set(parent_map) - set(proposed_map)
    changed = {
        eid
        for eid in set(parent_map) & set(proposed_map)
        if _stable_dump(parent_map[eid]) != _stable_dump(proposed_map[eid])
    }
    return added, changed, removed


def diff_communication_plans(
    parent: CommunicationPlan,
    proposed: CommunicationPlan,
) -> CommunicationPlanDelta:
    added_p, changed_p, removed_p = _diff_by_id(
        list(parent.payload_contracts),
        list(proposed.payload_contracts),
        id_attr="payload_id",
    )
    added_r, changed_r, removed_r = _diff_by_id(
        list(parent.delivery_schedule),
        list(proposed.delivery_schedule),
        id_attr="rule_id",
    )
    added_a, changed_a, removed_a = _diff_by_id(
        list(parent.aggregation_rules),
        list(proposed.aggregation_rules),
        id_attr="rule_id",
    )
    parent_budgets = dict(parent.context_budgets)
    proposed_budgets = dict(proposed.context_budgets)
    budget_keys = set(parent_budgets) | set(proposed_budgets)
    changed_budgets = {
        k
        for k in budget_keys
        if parent_budgets.get(k) != proposed_budgets.get(k)
    }
    return CommunicationPlanDelta(
        added_payload_ids=added_p,
        changed_payload_ids=changed_p,
        removed_payload_ids=removed_p,
        added_rule_ids=added_r,
        changed_rule_ids=changed_r,
        removed_rule_ids=removed_r,
        added_aggregation_rule_ids=added_a,
        changed_aggregation_rule_ids=changed_a,
        removed_aggregation_rule_ids=removed_a,
        changed_context_budget_targets=changed_budgets,
    )


class CommunicationTargetResolver:
    def payload_target(
        self,
        payload_id: str,
        plan: CommunicationPlan,
    ) -> str | None:
        for contract in plan.payload_contracts:
            if contract.payload_id == payload_id:
                return contract.target_subtask_id
        return None

    def delivery_rule_target(
        self,
        rule_id: str,
        plan: CommunicationPlan,
    ) -> str | None:
        for rule in plan.delivery_schedule:
            if rule.rule_id == rule_id:
                return self.payload_target(rule.payload_id, plan)
        return None

    def aggregation_rule_targets(
        self,
        rule_id: str,
        plan: CommunicationPlan,
        *,
        require_unique: bool = False,
    ) -> set[str]:
        rule = next(
            (r for r in plan.aggregation_rules if r.rule_id == rule_id),
            None,
        )
        if rule is None:
            return set()
        meta = str(rule.metadata.get("target_subtask_id") or "")
        if meta:
            return {meta}
        targets: set[str] = set()
        for pid in rule.source_payload_ids:
            tgt = self.payload_target(pid, plan)
            if tgt is not None:
                targets.add(tgt)
        if require_unique and len(targets) != 1:
            raise AggregationTargetAmbiguous(
                f"AGGREGATION_TARGET_AMBIGUOUS: rule {rule_id} resolves to "
                f"{sorted(targets) or 'no targets'}"
            )
        return targets


def immutable_communication_targets(state: Any) -> set[str]:
    """Targets whose communication semantics are frozen after lease/start."""
    from orchestra.control.task_state import SubtaskStatus

    frozen: set[str] = set()
    for sid, sub in state.subtasks.items():
        if sub.lease_status == "leased":
            frozen.add(sid)
            continue
        if sub.status in {
            SubtaskStatus.RUNNING,
            SubtaskStatus.RETRY_PENDING,
            SubtaskStatus.AWAITING_CANONICAL_COMMIT,
            SubtaskStatus.COMMITTED,
            SubtaskStatus.FAILED,
            SubtaskStatus.SKIPPED,
            SubtaskStatus.HARNESS_FAILED,
        }:
            frozen.add(sid)
    return frozen


def is_communication_target_eligible(state: Any, target_id: str) -> bool:
    """Eligible for communication edits: PENDING/READY + UNLEASED."""
    from orchestra.control.task_state import SubtaskStatus

    sub = state.subtasks.get(target_id)
    if sub is None:
        return False
    if sub.lease_status == "leased":
        return False
    return sub.status in {SubtaskStatus.PENDING, SubtaskStatus.READY}
