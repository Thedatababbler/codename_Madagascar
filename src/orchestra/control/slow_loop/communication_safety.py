"""Hard feasibility helpers for active required communication blocks.

Active required blocks are M5 safety constraints, not soft Pareto objectives.
A Slow Loop candidate selected during blocked-wave recovery must structurally
resolve every such block (or the path must fall through to M5 rule-based repair).
"""

from __future__ import annotations

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


def missing_required_rule_payload_ids(
    *,
    communication_plan: CommunicationPlan,
    target_subtask_id: str,
) -> list[str]:
    """Required contracts targeting ``target`` that currently have no DeliveryRule."""
    ruled = {r.payload_id for r in communication_plan.delivery_schedule}
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
    """Proposed plan must add a DeliveryRule for each exact missing payload_id."""
    missing = missing_required_rule_payload_ids(
        communication_plan=current, target_subtask_id=target_subtask_id
    )
    if not missing:
        # Block reason present but contracts already have rules — treat as unresolved
        # unless every required contract for the target has a rule in the proposal.
        required_pids = [
            c.payload_id
            for c in current.payload_contracts
            if c.target_subtask_id == target_subtask_id and c.is_required()
        ]
        if not required_pids:
            return False
        proposed_rules = {r.payload_id for r in proposed.delivery_schedule}
        return all(pid in proposed_rules for pid in required_pids)
    proposed_rules = {r.payload_id for r in proposed.delivery_schedule}
    return all(pid in proposed_rules for pid in missing)


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
                    f"target={sid} reason={reason} missing_rules={missing}"
                )
            continue
        if reason == DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE.value:
            # Require a concrete communication change for the blocked target.
            if proposed_communication_plan.model_dump(mode="json") == current.model_dump(
                mode="json"
            ):
                errors.append(
                    "REQUIRED_BLOCK_UNRESOLVED: "
                    f"target={sid} reason={reason} (communication unchanged)"
                )
            continue
        # Unrepairable / non-communication-edit blocks cannot be admitted via
        # scheduling/backend/context-only candidates.
        errors.append(
            f"REQUIRED_BLOCK_UNRESOLVED: target={sid} reason={reason} "
            "(not safely repairable by admitted candidate class)"
        )
    return (not errors), errors
