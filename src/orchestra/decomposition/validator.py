"""TaskPlan DAG and constraint validation."""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

from orchestra.decomposition.schemas import DecompositionLimits, SubtaskSpec, TaskPlan
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import NodeKind


class TaskPlanValidationError(ValueError):
    """Raised when a TaskPlan fails structural or limit checks."""


_KNOWN_HARNESS_IDS = frozenset(
    {"public_code_harness", "identity_harness", "none", "repository_test_harness"}
)


def validate_subtask_graph_harness(
    subtask: SubtaskSpec,
    *,
    require_public_keystone: bool = False,
) -> list[str]:
    """Return validation errors for SubtaskSpec ↔ local-graph harness binding."""
    errors: list[str] = []
    graph_path = Path(subtask.local_graph_template)
    if not graph_path.exists():
        return [
            f"subtask {subtask.subtask_id!r} local_graph_template not found: "
            f"{subtask.local_graph_template}"
        ]
    try:
        graph = load_graph(graph_path)
    except Exception as exc:  # noqa: BLE001 — surface load errors as plan errors
        return [
            f"subtask {subtask.subtask_id!r} failed to load graph "
            f"{subtask.local_graph_template}: {exc}"
        ]

    harness_nodes = [n for n in graph.nodes if n.node_kind is NodeKind.HARNESS]
    if require_public_keystone and not harness_nodes:
        errors.append(
            f"subtask {subtask.subtask_id!r} graph has no harness node "
            f"(required public keystone)"
        )
        return errors

    matching = [
        n
        for n in harness_nodes
        if getattr(n, "harness_id", None) == subtask.keystone_harness_id
    ]
    if require_public_keystone and subtask.keystone_harness_id in {"none", "identity_harness"}:
        errors.append(
            f"subtask {subtask.subtask_id!r} keystone_harness_id="
            f"{subtask.keystone_harness_id!r} is not a runnable public keystone"
        )
    if require_public_keystone and not matching:
        errors.append(
            f"subtask {subtask.subtask_id!r} graph missing harness_id="
            f"{subtask.keystone_harness_id!r}"
        )
    for node in matching:
        visibility = getattr(node, "visibility", None)
        if visibility is not None and str(getattr(visibility, "value", visibility)) != "public":
            errors.append(
                f"subtask {subtask.subtask_id!r} harness {node.node_id!r} "
                f"visibility must be public"
            )
        command = getattr(node, "command", None)
        if require_public_keystone and not command:
            errors.append(
                f"subtask {subtask.subtask_id!r} harness {node.node_id!r} "
                "missing command"
            )

    # Require freeze gate from harness passed when a matching harness exists.
    if matching:
        harness_ids = {n.node_id for n in matching}
        freeze_nodes = {
            n.node_id
            for n in graph.nodes
            if n.node_kind is NodeKind.TRANSFORM
            and getattr(n, "transform_id", None) == "freeze_repository_change"
        }
        gated = False
        for edge in graph.edges:
            if (
                edge.source_node in harness_ids
                and edge.destination_node in freeze_nodes
                and edge.condition is not None
                and getattr(edge.condition, "source_field", None) == "passed"
            ):
                gated = True
                break
        if require_public_keystone and freeze_nodes and not gated:
            errors.append(
                f"subtask {subtask.subtask_id!r} harness does not gate "
                "freeze_repository_change via passed condition"
            )
    return errors


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
    require_public_keystone_harness: bool = False,
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
        elif require_graph_files:
            errors.extend(
                validate_subtask_graph_harness(
                    item,
                    require_public_keystone=require_public_keystone_harness,
                )
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
