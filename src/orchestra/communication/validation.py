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


def _target_status_label(state: Any, target_id: str) -> tuple[str, str]:
    sub = state.subtasks.get(target_id)
    if sub is None:
        return ("missing", "missing")
    return (str(sub.status.value), str(sub.lease_status))


def _reject_immutable(
    *,
    entity_type: str,
    entity_id: str,
    target_id: str,
    state: Any,
) -> None:
    status, lease = _target_status_label(state, target_id)
    raise CommunicationPlanValidationError(
        f"IMMUTABLE_COMMUNICATION_HISTORY: {entity_type} {entity_id} "
        f"targets {status} subtask {target_id} (lease={lease})"
    )


def validate_proposed_communication_revision(
    *,
    task_plan: TaskPlan,
    communication_plan: CommunicationPlan,
    current_state: TaskExecutionState | Any,
    parent_communication_plan: CommunicationPlan | None = None,
    changed_payload_ids: set[str] | None = None,
) -> None:
    """Reject any communication delta that touches frozen targets."""
    from orchestra.communication.delta import (
        AggregationTargetAmbiguous,
        CommunicationTargetResolver,
        diff_communication_plans,
        immutable_communication_targets,
    )

    del changed_payload_ids  # superseded by full CommunicationPlanDelta
    validate_communication_structure(
        task_plan=task_plan, communication_plan=communication_plan
    )
    parent = parent_communication_plan or current_state.communication_plan
    delta = diff_communication_plans(parent, communication_plan)
    frozen = immutable_communication_targets(current_state)
    resolver = CommunicationTargetResolver()

    def _check_payload(pid: str, plan: CommunicationPlan) -> None:
        tgt = resolver.payload_target(pid, plan)
        if tgt is not None and tgt in frozen:
            _reject_immutable(
                entity_type="payload_contract",
                entity_id=pid,
                target_id=tgt,
                state=current_state,
            )

    def _check_rule(rid: str, plan: CommunicationPlan) -> None:
        tgt = resolver.delivery_rule_target(rid, plan)
        if tgt is not None and tgt in frozen:
            _reject_immutable(
                entity_type="delivery_rule",
                entity_id=rid,
                target_id=tgt,
                state=current_state,
            )

    def _check_agg(rid: str, plan: CommunicationPlan) -> None:
        try:
            targets = resolver.aggregation_rule_targets(
                rid, plan, require_unique=True
            )
        except AggregationTargetAmbiguous as exc:
            raise CommunicationPlanValidationError(str(exc)) from exc
        for tgt in targets:
            if tgt in frozen:
                _reject_immutable(
                    entity_type="aggregation_rule",
                    entity_id=rid,
                    target_id=tgt,
                    state=current_state,
                )

    for pid in (
        delta.added_payload_ids
        | delta.changed_payload_ids
        | delta.removed_payload_ids
    ):
        plan = (
            parent
            if pid in delta.removed_payload_ids
            else communication_plan
        )
        _check_payload(pid, plan)

    for rid in delta.added_rule_ids | delta.changed_rule_ids | delta.removed_rule_ids:
        plan = parent if rid in delta.removed_rule_ids else communication_plan
        # Removals resolve against parent; additions/changes against proposed.
        if rid in delta.removed_rule_ids:
            _check_rule(rid, parent)
        else:
            _check_rule(rid, communication_plan)

    for rid in (
        delta.added_aggregation_rule_ids
        | delta.changed_aggregation_rule_ids
        | delta.removed_aggregation_rule_ids
    ):
        plan = (
            parent
            if rid in delta.removed_aggregation_rule_ids
            else communication_plan
        )
        _check_agg(rid, plan)

    for tgt in delta.changed_context_budget_targets:
        if tgt in frozen:
            _reject_immutable(
                entity_type="context_budget",
                entity_id=tgt,
                target_id=tgt,
                state=current_state,
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
