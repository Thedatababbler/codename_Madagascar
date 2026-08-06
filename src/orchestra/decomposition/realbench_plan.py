"""Dynamic RealBench TaskPlan candidate builder (public harness bound).

Produces a milestone DAG from public_design (tree/package UML), not a hardcoded
analyze/implement/verify triple. Each milestone binds ``repository_test_harness``
and a catalog graph whose public check level matches the milestone role.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from orchestra.realbench.public_harness import (
    parse_expected_modules,
    parse_package_exports,
)

Role = Literal["discovery", "implementation", "integration"]

DEFAULT_GRAPH_CATALOG: dict[Role, str] = {
    "discovery": "configs/graphs/codex_realbench_public_discovery.yaml",
    "implementation": "configs/graphs/codex_realbench_public_implementation.yaml",
    "integration": "configs/graphs/codex_realbench_public_integration.yaml",
}

CODEX_GRAPH_CATALOG: dict[Role, str] = dict(DEFAULT_GRAPH_CATALOG)

SMOLAGENTS_GRAPH_CATALOG: dict[Role, str] = {
    "discovery": "configs/graphs/smolagents_realbench_public_discovery.yaml",
    "implementation": "configs/graphs/smolagents_realbench_public_implementation.yaml",
    "integration": "configs/graphs/smolagents_realbench_public_integration.yaml",
}


def graph_catalog_for_backend(backend: str) -> dict[Role, str]:
    """Return the public-graph catalog for a selected agent backend."""
    if backend == "smolagents_code":
        return dict(SMOLAGENTS_GRAPH_CATALOG)
    if backend == "codex_sdk":
        return dict(CODEX_GRAPH_CATALOG)
    raise ValueError(
        f"unsupported RealBench agent backend {backend!r}; "
        "expected 'codex_sdk' or 'smolagents_code'"
    )

KEYSTONE_HARNESS = "repository_test_harness"


def _slug(text: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return cleaned[:48] or fallback


def _top_level_packages(modules: list[str]) -> list[str]:
    roots: list[str] = []
    seen: set[str] = set()
    for mod in modules:
        root = mod.split(".", 1)[0]
        if root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return roots


def _second_level_groups(modules: list[str], root: str) -> list[str]:
    groups: list[str] = []
    seen: set[str] = set()
    prefix = root + "."
    for mod in modules:
        if not mod.startswith(prefix):
            continue
        rest = mod[len(prefix) :]
        group = rest.split(".", 1)[0]
        if not group or group in seen:
            continue
        seen.add(group)
        groups.append(group)
    return groups


def _budget(*, timeout: float = 1200.0) -> dict[str, Any]:
    return {
        "max_llm_calls": 1,
        "max_steps": 3,
        "timeout_seconds": timeout,
    }


def _artifact_io() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    inputs = [
        {
            "slot": "problem",
            "artifact_id": "",
            "artifact_type": "ProblemArtifact",
        }
    ]
    outputs = [
        {
            "parser_id": "repository_change",
            "output_schema": "RepositoryChangeArtifact",
        }
    ]
    return inputs, outputs


def _subtask(
    *,
    subtask_id: str,
    title: str,
    objective: str,
    role: Role,
    dependencies: list[str],
    priority: int,
    graph_catalog: dict[Role, str],
    focus_paths: list[str] | None = None,
) -> dict[str, Any]:
    inputs, outputs = _artifact_io()
    brief = (
        f"# Milestone `{subtask_id}`\n\n"
        f"- role: `{role}`\n"
        f"- title: {title}\n\n"
        f"## Objective\n{objective}\n\n"
        f"## Public harness\n"
        f"Your changes must pass "
        f"`python scripts/adamas_public_check.py --level {role}`.\n"
        "Hidden evaluation tests are unavailable.\n"
    )
    if focus_paths:
        brief += "\n## Focus paths\n" + "\n".join(f"- `{p}`" for p in focus_paths) + "\n"
    return {
        "subtask_id": subtask_id,
        "title": title,
        "objective": objective,
        "dependencies": dependencies,
        "priority": priority,
        "keystone_harness_id": KEYSTONE_HARNESS,
        "local_graph_template": graph_catalog[role],
        "budget": _budget(),
        "input_artifacts": inputs,
        "expected_outputs": outputs,
        "metadata": {
            "role": role,
            "milestone_brief": brief,
            "focus_paths": list(focus_paths or []),
            "public_harness_level": role,
        },
    }


def build_realbench_candidate_plan(
    *,
    task_id: str,
    workspace: Path,
    graph_catalog: dict[str, str] | None = None,
    max_implementation_milestones: int = 2,
) -> dict[str, Any]:
    """Build a dynamic TaskPlan dict from public workspace design artifacts."""
    catalog: dict[Role, str] = {
        "discovery": (graph_catalog or {}).get(
            "discovery", DEFAULT_GRAPH_CATALOG["discovery"]
        ),
        "implementation": (graph_catalog or {}).get(
            "implementation", DEFAULT_GRAPH_CATALOG["implementation"]
        ),
        "integration": (graph_catalog or {}).get(
            "integration", DEFAULT_GRAPH_CATALOG["integration"]
        ),
    }

    tree_text = ""
    tree_path = workspace / "public_design" / "tree.txt"
    if tree_path.is_file():
        tree_text = tree_path.read_text(encoding="utf-8", errors="ignore")
    modules = parse_expected_modules(tree_text)
    packages = _top_level_packages(modules)
    package_json: dict[str, Any] = {}
    pkg_path = workspace / "public_design" / "package.json"
    if pkg_path.is_file():
        import json

        try:
            raw = json.loads(pkg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                package_json = raw
        except json.JSONDecodeError:
            package_json = {}
    exports = parse_package_exports(package_json)

    subtasks: list[dict[str, Any]] = []
    discovery_id = "map_public_api"
    subtasks.append(
        _subtask(
            subtask_id=discovery_id,
            title="Map public API and draft NOTES",
            objective=(
                f"For RealBench task `{task_id}`, read TASK.md, REQUIREMENTS.md, and "
                "public_design/. Write NOTES.md covering modules, public exports, and "
                "implementation order. Create a coherent draft scaffold that keeps "
                f"`python scripts/adamas_public_check.py --level discovery` green."
            ),
            role="discovery",
            dependencies=[],
            priority=100,
            graph_catalog=catalog,
            focus_paths=["NOTES.md", "public_design/", *packages[:6]],
        )
    )

    impl_ids: list[str] = []
    if not packages:
        impl_id = "implement_core"
        impl_ids.append(impl_id)
        subtasks.append(
            _subtask(
                subtask_id=impl_id,
                title="Implement core repository",
                objective=(
                    f"Implement the core repository for `{task_id}` described by "
                    "NOTES.md and public_design/. Prefer completing public modules "
                    "over unrelated files. Must pass "
                    "`python scripts/adamas_public_check.py --level implementation`."
                ),
                role="implementation",
                dependencies=[discovery_id],
                priority=80,
                graph_catalog=catalog,
            )
        )
    elif len(packages) == 1 and len(modules) <= 4:
        root = packages[0]
        impl_id = f"implement_{_slug(root, fallback='core')}"
        impl_ids.append(impl_id)
        subtasks.append(
            _subtask(
                subtask_id=impl_id,
                title=f"Implement package {root}",
                objective=(
                    f"Implement package `{root}` and its public functions/classes for "
                    f"`{task_id}`. Match public UML exports when present "
                    f"({', '.join(list(exports)[:8]) or 'see package.json'}). "
                    "Must pass the implementation public harness."
                ),
                role="implementation",
                dependencies=[discovery_id],
                priority=80,
                graph_catalog=catalog,
                focus_paths=[root],
            )
        )
    else:
        # Multi-package: up to N implementation milestones focusing on groups.
        root = packages[0]
        groups = _second_level_groups(modules, root) or packages
        # Always cover root package surface first.
        chunks: list[list[str]] = []
        primary = [root] + [f"{root}/{g}" for g in groups[:3]]
        chunks.append(primary)
        if len(groups) > 3 and max_implementation_milestones >= 2:
            chunks.append([f"{root}/{g}" for g in groups[3:8]])
        chunks = chunks[: max(1, max_implementation_milestones)]
        prev = discovery_id
        for idx, focus in enumerate(chunks):
            impl_id = f"implement_{_slug(focus[0], fallback=f'part{idx+1}')}"
            # Avoid collisions.
            base = impl_id
            n = 2
            existing = {s["subtask_id"] for s in subtasks}
            while impl_id in existing:
                impl_id = f"{base}_{n}"
                n += 1
            impl_ids.append(impl_id)
            subtasks.append(
                _subtask(
                    subtask_id=impl_id,
                    title=f"Implement milestone {idx + 1}",
                    objective=(
                        f"Implement focus paths for `{task_id}`: "
                        + ", ".join(f"`{p}`" for p in focus)
                        + ". Keep previously committed modules working. "
                        "Must pass the implementation public harness."
                    ),
                    role="implementation",
                    dependencies=[prev],
                    priority=80 - idx,
                    graph_catalog=catalog,
                    focus_paths=focus,
                )
            )
            prev = impl_id

    integration_deps = impl_ids or [discovery_id]
    integration_id = "public_contract_harden"
    subtasks.append(
        _subtask(
            subtask_id=integration_id,
            title="Harden public contract",
            objective=(
                f"Finalize `{task_id}` against public_design/: fix imports, packaging, "
                "cross-module APIs, and UML exports. Must pass "
                "`python scripts/adamas_public_check.py --level integration`."
            ),
            role="integration",
            dependencies=integration_deps,
            priority=10,
            graph_catalog=catalog,
            focus_paths=packages[:8],
        )
    )

    # Chain communication reports along the linear backbone.
    ordered_ids = [s["subtask_id"] for s in subtasks]
    payload_contracts: list[dict[str, Any]] = []
    delivery_schedule: list[dict[str, Any]] = []
    for src, dst in zip(ordered_ids, ordered_ids[1:], strict=False):
        payload_id = f"handoff_{src}_to_{dst}"
        payload_contracts.append(
            {
                "payload_id": payload_id,
                "source_subtask_id": src,
                "target_subtask_id": dst,
                "artifact_type": "RepositoryChangeArtifact",
                "required": False,
                "max_tokens": 16000,
                "metadata": {"slot": f"comm:{payload_id}"},
            }
        )
        delivery_schedule.append(
            {"rule_id": f"deliver_{payload_id}", "payload_id": payload_id, "enabled": True}
        )

    return {
        "task_id": f"rb_{task_id}",
        "plan_version": 1,
        "decomposition_rationale": (
            "Dynamic RealBench milestone plan from public_design tree/package UML. "
            "Each milestone binds repository_test_harness to a role-graded public "
            "check (discovery/implementation/integration). Hidden tests are excluded."
        ),
        "decomposition_status": "ok",
        "subtasks": subtasks,
        "final_aggregation": {
            "strategy": "identity",
            "terminal_subtask_id": integration_id,
        },
        "communication_plan": {
            "version": 1,
            "payload_contracts": payload_contracts,
            "delivery_schedule": delivery_schedule,
            "context_budgets": {sid: 24000 for sid in ordered_ids[1:]},
        },
        "metadata": {
            "benchmark": "realbench",
            "plan_builder": "build_realbench_candidate_plan",
            "top_level_packages": packages,
            "module_count": len(modules),
            "export_keys": sorted(exports)[:32],
            "graph_catalog": catalog,
        },
    }
