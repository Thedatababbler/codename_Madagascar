"""CommunicationPlan validation modes (M5.2)."""

from __future__ import annotations

from collections import defaultdict, deque
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from orchestra.communication.payload import DeliveryTrigger
from orchestra.communication.plan import CommunicationPlan
from orchestra.decomposition.schemas import TaskPlan
from orchestra.ir.artifacts import PAYLOAD_SCHEMAS

if TYPE_CHECKING:
    from orchestra.control.task_state import TaskExecutionState

PRIVATE_ARTIFACT_MARKERS = (
    "Private",
    "Hidden",
    "FinalLCB",
    "private_evaluator",
    "hidden_test",
)


class CommunicationPlanValidationError(ValueError):
    pass


class CommunicationValidationMode(StrEnum):
    STRUCTURAL = "structural"
    PROPOSED_REVISION = "proposed_revision"
    ACTIVE_EXECUTION = "active_execution"


def _ancestors(task_plan: TaskPlan) -> dict[str, set[str]]:
    deps = {s.subtask_id: set(s.dependencies) for s in task_plan.subtasks}
    memo: dict[str, set[str]] = {}

    def walk(sid: str) -> set[str]:
        if sid in memo:
            return memo[sid]
        out: set[str] = set()
        for d in deps.get(sid, set()):
            out.add(d)
            out |= walk(d)
        memo[sid] = out
        return out

    for sid in deps:
        walk(sid)
    return memo


def _has_cycle(edges: dict[str, set[str]], nodes: set[str]) -> bool:
    indeg = {n: 0 for n in nodes}
    for _src, dsts in edges.items():
        for dst in dsts:
            if dst in indeg:
                indeg[dst] += 1
    q = deque([n for n, d in indeg.items() if d == 0])
    seen = 0
    while q:
        n = q.popleft()
        seen += 1
        for dst in edges.get(n, set()):
            indeg[dst] -= 1
            if indeg[dst] == 0:
                q.append(dst)
    return seen != len(nodes)


def _past_or_active_targets(state: Any) -> set[str]:
    """Targets that must not receive *new* proposed communication."""
    from orchestra.control.task_state import SubtaskStatus

    blocked: set[str] = set()
    for sid, sub in state.subtasks.items():
        if sub.lease_status == "leased":
            blocked.add(sid)
            continue
        if sub.status in {
            SubtaskStatus.COMMITTED,
            SubtaskStatus.RUNNING,
            SubtaskStatus.AWAITING_CANONICAL_COMMIT,
            SubtaskStatus.RETRY_PENDING,
            SubtaskStatus.FAILED,
            SubtaskStatus.SKIPPED,
            SubtaskStatus.HARNESS_FAILED,
        }:
            blocked.add(sid)
    return blocked


def _contract_fingerprint(contract) -> dict:
    return contract.model_dump(mode="json")


def validate_communication_structure(
    *,
    task_plan: TaskPlan,
    communication_plan: CommunicationPlan,
) -> None:
    """Structural checks independent of runtime subtask status."""
    subtask_ids = {s.subtask_id for s in task_plan.subtasks}
    payload_ids: set[str] = set()
    ancestors = _ancestors(task_plan)

    for contract in communication_plan.payload_contracts:
        if contract.payload_id in payload_ids:
            raise CommunicationPlanValidationError(
                f"duplicate payload_id: {contract.payload_id}"
            )
        payload_ids.add(contract.payload_id)
        if contract.source_subtask_id not in subtask_ids:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: unknown source {contract.source_subtask_id}"
            )
        if contract.target_subtask_id not in subtask_ids:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: unknown target {contract.target_subtask_id}"
            )
        if contract.source_subtask_id == contract.target_subtask_id:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: source equals target"
            )
        if contract.max_tokens <= 0:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: max_tokens must be > 0"
            )
        if any(m.lower() in contract.artifact_type.lower() for m in PRIVATE_ARTIFACT_MARKERS):
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: private/hidden artifact type forbidden"
            )
        if (
            contract.artifact_type not in PAYLOAD_SCHEMAS
            and not contract.artifact_type.endswith("Artifact")
        ):
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: illegal artifact_type {contract.artifact_type}"
            )
        if contract.is_required():
            src = contract.source_subtask_id
            tgt = contract.target_subtask_id
            if src not in ancestors.get(tgt, set()):
                raise CommunicationPlanValidationError(
                    f"payload {contract.payload_id}: required communication source "
                    f"{src} is not a DAG ancestor of target {tgt}"
                )

    for rule in communication_plan.delivery_schedule:
        if rule.payload_id not in payload_ids:
            raise CommunicationPlanValidationError(
                f"delivery rule {rule.rule_id}: unknown payload {rule.payload_id}"
            )
        if rule.forward_only is False:
            raise CommunicationPlanValidationError(
                f"delivery rule {rule.rule_id}: backward delivery forbidden in M5"
            )
        if rule.trigger is DeliveryTrigger.MANUAL and rule.enabled:
            raise CommunicationPlanValidationError(
                f"delivery rule {rule.rule_id}: MANUAL trigger unsupported in M5"
            )

    for sid, budget in communication_plan.context_budgets.items():
        if sid not in subtask_ids:
            raise CommunicationPlanValidationError(
                f"context budget for unknown subtask {sid}"
            )
        if budget < 0:
            raise CommunicationPlanValidationError(
                f"context budget for {sid} must be >= 0"
            )

    for agg in communication_plan.aggregation_rules:
        for src in agg.source_subtask_ids:
            if src not in subtask_ids:
                raise CommunicationPlanValidationError(
                    f"aggregation {agg.rule_id}: unknown source {src}"
                )
        for pid in agg.source_payload_ids:
            if pid not in payload_ids:
                raise CommunicationPlanValidationError(
                    f"aggregation {agg.rule_id}: unknown payload {pid}"
                )

    edges: dict[str, set[str]] = defaultdict(set)
    for spec in task_plan.subtasks:
        for dep in spec.dependencies:
            edges[dep].add(spec.subtask_id)
    for contract in communication_plan.payload_contracts:
        if contract.is_required():
            edges[contract.source_subtask_id].add(contract.target_subtask_id)
    if _has_cycle(edges, subtask_ids):
        raise CommunicationPlanValidationError(
            "required communication + task dependencies form a cycle"
        )


def validate_proposed_communication_revision(
    *,
    task_plan: TaskPlan,
    communication_plan: CommunicationPlan,
    current_state: TaskExecutionState | Any,
    parent_communication_plan: CommunicationPlan | None = None,
    changed_payload_ids: set[str] | None = None,
) -> None:
    """Reject new/changed contracts that target past or leased subtasks."""
    validate_communication_structure(
        task_plan=task_plan, communication_plan=communication_plan
    )
    blocked = _past_or_active_targets(current_state)
    parent = parent_communication_plan or current_state.communication_plan
    parent_by_id = {c.payload_id: c for c in parent.payload_contracts}
    changed = changed_payload_ids
    if changed is None:
        changed = set()
        for contract in communication_plan.payload_contracts:
            prev = parent_by_id.get(contract.payload_id)
            if prev is None or _contract_fingerprint(prev) != _contract_fingerprint(
                contract
            ):
                changed.add(contract.payload_id)
        for pid in parent_by_id:
            if pid not in {c.payload_id for c in communication_plan.payload_contracts}:
                # Deletion of historical contract targeting blocked is allowed;
                # addition/change is what we gate.
                pass

    for contract in communication_plan.payload_contracts:
        if contract.payload_id not in changed:
            continue
        if contract.target_subtask_id in blocked:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: proposed revision cannot target "
                f"past/leased subtask {contract.target_subtask_id}"
            )


def validate_active_communication_plan(
    *,
    task_plan: TaskPlan,
    communication_plan: CommunicationPlan,
) -> None:
    """Active runtime: structure only; historical completed targets remain valid."""
    validate_communication_structure(
        task_plan=task_plan, communication_plan=communication_plan
    )


def validate_communication_plan(
    *,
    task_plan: TaskPlan,
    communication_plan: CommunicationPlan,
    mode: CommunicationValidationMode | str = CommunicationValidationMode.STRUCTURAL,
    current_state: TaskExecutionState | Any | None = None,
    parent_communication_plan: CommunicationPlan | None = None,
    changed_payload_ids: set[str] | None = None,
    completed_subtask_ids: set[str] | None = None,
) -> None:
    """
    Unified entrypoint.

    ``completed_subtask_ids`` is deprecated. When provided with no explicit mode
    other than the default, PROPOSED_REVISION semantics are applied for backward
    compatibility with older call sites that passed completed targets.
    """
    del completed_subtask_ids  # no longer used to reject historical contracts
    mode_v = CommunicationValidationMode(mode)
    if mode_v is CommunicationValidationMode.STRUCTURAL:
        validate_communication_structure(
            task_plan=task_plan, communication_plan=communication_plan
        )
        return
    if mode_v is CommunicationValidationMode.ACTIVE_EXECUTION:
        validate_active_communication_plan(
            task_plan=task_plan, communication_plan=communication_plan
        )
        return
    if mode_v is CommunicationValidationMode.PROPOSED_REVISION:
        if current_state is None:
            raise CommunicationPlanValidationError(
                "PROPOSED_REVISION validation requires current_state"
            )
        validate_proposed_communication_revision(
            task_plan=task_plan,
            communication_plan=communication_plan,
            current_state=current_state,
            parent_communication_plan=parent_communication_plan,
            changed_payload_ids=changed_payload_ids,
        )
        return
    raise CommunicationPlanValidationError(f"unknown validation mode: {mode}")
