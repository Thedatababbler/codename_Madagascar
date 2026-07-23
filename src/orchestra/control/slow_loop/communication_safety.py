"""Hard feasibility helpers for active required communication blocks.

Active required blocks are M5 safety constraints, not soft Pareto objectives.
A Slow Loop candidate selected during blocked-wave recovery must structurally
resolve every such block (or the path must fall through to M5 rule-based repair).

Delivery-engine alignment: a required payload is ruled only when it has at
least one *enabled* DeliveryRule (disabled-only schedules still yield
REQUIRED_RULE_MISSING).
"""

from __future__ import annotations

from typing import Any

from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState

REQUIRED_COMMUNICATION_BLOCK_REASONS = {
    DeliveryFailureReason.REQUIRED_SOURCE_NOT_COMMITTED.value,
    DeliveryFailureReason.REQUIRED_ARTIFACT_MISSING.value,
    DeliveryFailureReason.REQUIRED_FIELD_MISSING.value,
    DeliveryFailureReason.REQUIRED_RULE_MISSING.value,
    DeliveryFailureReason.REQUIRED_CONDITION_UNSATISFIED.value,
    DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value,
    DeliveryFailureReason.PROJECTION_INFEASIBLE.value,
    DeliveryFailureReason.LEDGER_CORRUPTION.value,
}


def active_required_communication_blocks(
    state: TaskExecutionState,
) -> dict[str, str]:
    """Map target_subtask_id → block reason for unleased PENDING/READY targets."""
    blocks: dict[str, str] = {}
    for sid, sub in sorted(state.subtasks.items()):
        if sub.status not in {SubtaskStatus.PENDING, SubtaskStatus.READY}:
            continue
        if sub.lease_status == "leased":
            continue
        reason = sub.communication_block_reason
        if not reason:
            continue
        if reason in REQUIRED_COMMUNICATION_BLOCK_REASONS:
            blocks[sid] = reason
    return blocks


def enabled_rule_payload_ids(communication_plan: CommunicationPlan) -> set[str]:
    """Payload IDs that have at least one enabled DeliveryRule."""
    return {
        r.payload_id
        for r in communication_plan.delivery_schedule
        if bool(getattr(r, "enabled", True))
    }


def missing_required_rule_payload_ids(
    *,
    communication_plan: CommunicationPlan,
    target_subtask_id: str,
) -> list[str]:
    """Required contracts targeting ``target`` with no *enabled* DeliveryRule."""
    ruled = enabled_rule_payload_ids(communication_plan)
    missing: list[str] = []
    for contract in communication_plan.payload_contracts:
        if contract.target_subtask_id != target_subtask_id:
            continue
        if not contract.is_required():
            continue
        if contract.payload_id not in ruled:
            missing.append(contract.payload_id)
    return missing


def proposed_comm_resolves_required_rule_missing(
    *,
    current: CommunicationPlan,
    proposed: CommunicationPlan,
    target_subtask_id: str,
) -> bool:
    """Proposed plan must install an enabled DeliveryRule for each missing payload_id."""
    missing = missing_required_rule_payload_ids(
        communication_plan=current, target_subtask_id=target_subtask_id
    )
    proposed_enabled = enabled_rule_payload_ids(proposed)
    if not missing:
        # Block reason present but every required contract already has an enabled
        # rule in current — treat as unresolved unless proposal still covers them.
        required_pids = [
            c.payload_id
            for c in current.payload_contracts
            if c.target_subtask_id == target_subtask_id and c.is_required()
        ]
        if not required_pids:
            return False
        return all(pid in proposed_enabled for pid in required_pids)
    return all(pid in proposed_enabled for pid in missing)


def target_communication_semantics(
    plan: CommunicationPlan,
    target_subtask_id: str,
) -> dict[str, Any]:
    """Version-excluded semantic communication state for one target.

    Scheduling-only edits still bump ``CommunicationPlan.version`` via
    ``apply_global_edits``; that must not count as resolving a block.
    """
    contracts = [
        c.model_dump(mode="json")
        for c in plan.payload_contracts
        if c.target_subtask_id == target_subtask_id
    ]
    contract_pids = {
        c.payload_id
        for c in plan.payload_contracts
        if c.target_subtask_id == target_subtask_id
    }
    enabled_rules = [
        r.model_dump(mode="json")
        for r in plan.delivery_schedule
        if bool(getattr(r, "enabled", True)) and r.payload_id in contract_pids
    ]
    aggregations: list[dict[str, Any]] = []
    for rule in plan.aggregation_rules:
        meta_target = (rule.metadata or {}).get("target_subtask_id")
        source_hit = any(
            pid in contract_pids for pid in (rule.source_payload_ids or [])
        )
        if meta_target == target_subtask_id or source_hit:
            aggregations.append(rule.model_dump(mode="json"))
    return {
        "contracts": sorted(contracts, key=lambda row: str(row.get("payload_id", ""))),
        "enabled_rules": sorted(
            enabled_rules, key=lambda row: str(row.get("rule_id", ""))
        ),
        "aggregations": sorted(
            aggregations, key=lambda row: str(row.get("rule_id", ""))
        ),
        "context_budget": plan.context_budgets.get(target_subtask_id),
    }


def required_payloads_fit_context_budget(
    plan: CommunicationPlan,
    target_subtask_id: str,
) -> bool:
    """Static required-token feasibility for ``target`` (matches generator gate)."""
    budget = plan.context_budgets.get(target_subtask_id)
    if budget is None:
        return True
    if int(budget) <= 0:
        return False
    needed = sum(
        int(c.max_tokens)
        for c in plan.payload_contracts
        if c.target_subtask_id == target_subtask_id and c.is_required()
    )
    return needed <= int(budget)


def proposed_comm_resolves_context_budget_infeasible(
    *,
    current: CommunicationPlan,
    proposed: CommunicationPlan,
    target_subtask_id: str,
) -> bool:
    """Target-specific semantic change that leaves required payloads feasible."""
    if target_communication_semantics(current, target_subtask_id) == (
        target_communication_semantics(proposed, target_subtask_id)
    ):
        return False
    return required_payloads_fit_context_budget(proposed, target_subtask_id)


def candidate_resolves_active_required_blocks(
    *,
    state: TaskExecutionState,
    proposed_communication_plan: CommunicationPlan,
) -> tuple[bool, list[str]]:
    """Return (ok, errors). Non-resolving candidates are hard-infeasible."""
    blocks = active_required_communication_blocks(state)
    if not blocks:
        return True, []
    errors: list[str] = []
    current = state.communication_plan
    for sid, reason in blocks.items():
        if reason == DeliveryFailureReason.REQUIRED_RULE_MISSING.value:
            if not proposed_comm_resolves_required_rule_missing(
                current=current,
                proposed=proposed_communication_plan,
                target_subtask_id=sid,
            ):
                missing = missing_required_rule_payload_ids(
                    communication_plan=current, target_subtask_id=sid
                )
                errors.append(
                    "REQUIRED_BLOCK_UNRESOLVED: "
                    f"target={sid} reason={reason} missing_enabled_rules={missing}"
                )
            continue
        if reason == DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value:
            if not proposed_comm_resolves_context_budget_infeasible(
                current=current,
                proposed=proposed_communication_plan,
                target_subtask_id=sid,
            ):
                errors.append(
                    "REQUIRED_BLOCK_UNRESOLVED: "
                    f"target={sid} reason={reason} "
                    "(no target-specific feasible communication repair)"
                )
            continue
        # Unrepairable / non-communication-edit blocks cannot be admitted via
        # scheduling/backend/context-only candidates.
        errors.append(
            f"REQUIRED_BLOCK_UNRESOLVED: target={sid} reason={reason} "
            "(not safely repairable by admitted candidate class)"
        )
    return (not errors), errors
