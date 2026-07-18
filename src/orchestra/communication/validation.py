"""CommunicationPlan structural validation (M5)."""

from __future__ import annotations

from collections import defaultdict, deque

from orchestra.communication.payload import DeliveryTrigger
from orchestra.communication.plan import CommunicationPlan
from orchestra.decomposition.schemas import TaskPlan
from orchestra.ir.artifacts import PAYLOAD_SCHEMAS

PRIVATE_ARTIFACT_MARKERS = (
    "Private",
    "Hidden",
    "FinalLCB",
    "private_evaluator",
    "hidden_test",
)


class CommunicationPlanValidationError(ValueError):
    pass


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


def validate_communication_plan(
    *,
    task_plan: TaskPlan,
    communication_plan: CommunicationPlan,
    completed_subtask_ids: set[str] | None = None,
) -> None:
    completed = completed_subtask_ids or set()
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
        if contract.target_subtask_id in completed:
            raise CommunicationPlanValidationError(
                f"payload {contract.payload_id}: delivery targets completed "
                f"subtask {contract.target_subtask_id}"
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
        # Required communication must have executable precedence.
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

    # Combined dependency graph: task deps + required communication edges.
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
