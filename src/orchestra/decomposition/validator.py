"""TaskPlan DAG and constraint validation."""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

from orchestra.decomposition.schemas import DecompositionLimits, TaskPlan


class TaskPlanValidationError(ValueError):
    """Raised when a TaskPlan fails structural or limit checks."""


_KNOWN_HARNESS_IDS = frozenset(
    {"public_code_harness", "identity_harness", "none", "repository_test_harness"}
)


def _dependency_depth(subtask_ids: set[str], deps: dict[str, list[str]]) -> int:
    memo: dict[str, int] = {}

    def depth(node: str, stack: set[str]) -> int:
        if node in memo:
            return memo[node]
        if node in stack:
            return -1
        stack.add(node)
        parents = deps.get(node, [])
        if not parents:
            value = 0
        else:
            child_depths = [depth(parent, stack) for parent in parents]
            if any(d < 0 for d in child_depths):
                value = -1
            else:
                value = 1 + max(child_depths)
        stack.remove(node)
        memo[node] = value
        return value

    depths = [depth(node, set()) for node in subtask_ids]
    if any(d < 0 for d in depths):
        return -1
    return max(depths) if depths else 0


def validate_task_plan(
    plan: TaskPlan,
    *,
    limits: DecompositionLimits | None = None,
    known_harness_ids: set[str] | frozenset[str] | None = None,
    require_graph_files: bool = True,
) -> None:
    """Validate TaskPlan structure. Raises TaskPlanValidationError on failure."""
    limits = limits or DecompositionLimits()
    harness_ids = known_harness_ids or _KNOWN_HARNESS_IDS
    errors: list[str] = []

    n = len(plan.subtasks)
    if n < limits.min_subtasks:
        errors.append(f"too few subtasks: {n} < {limits.min_subtasks}")
    if n > limits.max_subtasks:
        errors.append(f"too many subtasks: {n} > {limits.max_subtasks}")

    ids = [item.subtask_id for item in plan.subtasks]
    if len(ids) != len(set(ids)):
        errors.append("duplicate subtask_id values")

    id_set = set(ids)
    deps: dict[str, list[str]] = {}
    for item in plan.subtasks:
        for dep in item.dependencies:
            if dep not in id_set:
                errors.append(
                    f"subtask {item.subtask_id!r} depends on missing {dep!r}"
                )
        deps[item.subtask_id] = list(item.dependencies)

        if not item.keystone_harness_id.strip():
            errors.append(f"subtask {item.subtask_id!r} missing keystone_harness_id")
        elif item.keystone_harness_id not in harness_ids:
            errors.append(
                f"subtask {item.subtask_id!r} unknown harness "
                f"{item.keystone_harness_id!r}"
            )

        if not item.local_graph_template.strip():
            errors.append(f"subtask {item.subtask_id!r} missing local_graph_template")
        elif require_graph_files and not Path(item.local_graph_template).exists():
            errors.append(
                f"subtask {item.subtask_id!r} local_graph_template not found: "
                f"{item.local_graph_template}"
            )

        # Budget positivity is enforced by BudgetSpec validators; re-check for clarity.
        if item.budget.max_llm_calls < 1 or item.budget.max_steps < 1:
            errors.append(f"subtask {item.subtask_id!r} has non-positive budget")
        if item.budget.timeout_seconds <= 0:
            errors.append(f"subtask {item.subtask_id!r} has non-positive timeout")

    if not id_set:
        if errors:
            raise TaskPlanValidationError("; ".join(errors))
        raise TaskPlanValidationError("task plan has no subtasks")

    sources = [sid for sid, parents in deps.items() if not parents]
    if not sources:
        errors.append("no source subtask (every node has dependencies)")

    dependents: dict[str, list[str]] = defaultdict(list)
    for sid, parents in deps.items():
        for parent in parents:
            dependents[parent].append(sid)
    terminals = [sid for sid in id_set if not dependents.get(sid)]
    if not terminals:
        errors.append("no terminal subtask")

    # Cycle detection via Kahn topological sort.
    indegree = {sid: len(deps[sid]) for sid in id_set}
    queue: deque[str] = deque(sources)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in dependents.get(node, []):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(id_set):
        errors.append("cyclic subtask dependencies")

    depth = _dependency_depth(id_set, deps)
    if depth < 0:
        errors.append("cyclic subtask dependencies")
    elif depth > limits.max_dependency_depth:
        errors.append(
            f"dependency depth {depth} exceeds max_dependency_depth "
            f"{limits.max_dependency_depth}"
        )

    if errors:
        raise TaskPlanValidationError("; ".join(sorted(set(errors))))
