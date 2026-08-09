"""Dynamic RealBench TaskPlan candidate builder (public harness bound).

Two decomposition sources feed the same TaskPlan shape:

* a **planner draft** (risk-first milestones from
  :mod:`orchestra.realbench.milestone_planner`), each milestone compiled into its
  own generated subgraph with a named agent roster;
* the deterministic **public_design** builder used as fail-closed fallback.

Each milestone binds ``repository_test_harness`` to a role-graded public check.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from orchestra.realbench.milestone_contracts import materialize_milestone_contracts
from orchestra.realbench.milestone_planner import MilestoneDraft, MilestonePlanDraft
from orchestra.realbench.public_harness import (
    parse_expected_modules,
    parse_package_exports,
    public_check_command,
)
from orchestra.realbench.subgraph_builder import (
    REALBENCH_PROMPT_PROFILE,
    DatasetPromptProfile,
    materialize_milestone_subgraph,
    materialize_role_graph,
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

_HARNESS_CHECK_TEXT = (
    "AdaMAS runs the acceptance check from outside this repository once you "
    "stop: it compiles the public tree, imports every module at the path "
    "public_design declares, and (at integration level) requires each "
    "UML-exported symbol to be importable from that exact module. Hidden "
    "benchmark tests are unavailable to you.\n"
    "Ship only files described by the public tree — notes, plans, scratch "
    "directories, or duplicate package copies are not carried into evaluation, "
    "so nothing you ship may import them."
)

_HARNESS_BRIEF: dict[str, str] = {
    "discovery": (
        "Scaffold level: the public tree's modules must exist and compile.\n"
        + _HARNESS_CHECK_TEXT
    ),
    "implementation": (
        "Implementation level: every public module must import cleanly.\n"
        + _HARNESS_CHECK_TEXT
    ),
    "integration": (
        "Integration level: public modules import cleanly and expose their "
        "UML-declared symbols.\n" + _HARNESS_CHECK_TEXT
    ),
}


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


def should_split_milestones(
    *,
    modules: list[str],
    packages: list[str],
    exports: dict[str, list[str]],
    force_split: bool | None = None,
) -> bool:
    """Return True only when multi-milestone decomposition is warranted."""
    if force_split is not None:
        return bool(force_split)
    n_mod = len(modules)
    n_pkg = len(packages)
    n_exp = sum(len(v) for v in exports.values())
    n_groups = 0
    if packages:
        n_groups = len(_second_level_groups(modules, packages[0]))
    # Small / single-package repos: keep one long-horizon milestone (vanilla-like).
    if n_pkg <= 1 and n_mod <= 8 and n_exp <= 24 and n_groups <= 3:
        return False
    if n_mod <= 5:
        return False
    return True


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
        f"## Acceptance harness\n{_HARNESS_BRIEF[role]}\n"
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


def _milestone_brief(milestone: MilestoneDraft, roster: list[dict[str, Any]]) -> str:
    lines = [
        f"# Milestone `{milestone.milestone_id}`",
        "",
        f"- role: `{milestone.role}`",
        f"- title: {milestone.title}",
        "",
        "## Objective",
        milestone.objective,
        "",
    ]
    if milestone.risk_rationale:
        lines += [
            "## Why this milestone gates the rest",
            milestone.risk_rationale,
            "",
            "Downstream milestones import what you freeze here. Later agents are "
            "instructed to extend, not redesign, these contracts.",
            "",
        ]
    if milestone.acceptance.criteria:
        lines += ["## Acceptance criteria"]
        lines += [f"- {item}" for item in milestone.acceptance.criteria]
        lines += [""]
    if milestone.acceptance.corner_cases:
        lines += ["## Corner cases"]
        lines += [f"- {item}" for item in milestone.acceptance.corner_cases]
        lines += [""]
    if milestone.focus_paths:
        lines += ["## Focus paths"]
        lines += [f"- `{p}`" for p in milestone.focus_paths]
        lines += [""]
    if roster:
        lines += ["## Agent roster (this subgraph, in order)"]
        lines += [
            f"- `{entry['role_id']}` — {entry['role']} "
            f"(max_tokens={entry['max_tokens']}, max_steps={entry['max_steps']})"
            for entry in roster
        ]
        lines += [""]
    lines += [
        "## Acceptance harness",
        _HARNESS_BRIEF[milestone.role],
        "",
    ]
    return "\n".join(lines)


def _milestone_budget(milestone: MilestoneDraft) -> dict[str, Any]:
    agents = milestone.agents or []
    timeout = sum(agent.timeout_seconds for agent in agents) or 1200.0
    return {
        "max_llm_calls": max(1, len(agents)),
        "max_steps": max([agent.max_steps for agent in agents] or [3]),
        "timeout_seconds": min(timeout, 7200.0),
    }


def realbench_harness_binder(
    *, workspace: Path, harness_dir: Path
) -> Callable[[MilestoneDraft], list[str]]:
    """Freeze RealBench contracts per milestone and return its gate command."""

    def bind(milestone: MilestoneDraft) -> list[str]:
        _, contracts_path = materialize_milestone_contracts(
            Path(workspace),
            harness_dir=Path(harness_dir),
            role=milestone.role,
            focus_paths=list(milestone.focus_paths),
            milestone_id=milestone.milestone_id,
            extra_checks=list(milestone.acceptance.checks) or None,
            acceptance_criteria=list(milestone.acceptance.criteria) or None,
            corner_cases=list(milestone.acceptance.corner_cases) or None,
        )
        return public_check_command(
            harness_dir=Path(harness_dir),
            level=milestone.role,
            contracts_path=contracts_path,
        )

    return bind


def build_plan_from_draft(
    *,
    task_id: str,
    draft: MilestonePlanDraft,
    workspace: Path,
    generated_root: Path,
    harness_dir: Path,
    agent_backend: str,
    model_name: str | None = None,
    harness_binder: Callable[[MilestoneDraft], list[str]] | None = None,
    benchmark: str = "realbench",
    prompt_profile: DatasetPromptProfile = REALBENCH_PROMPT_PROFILE,
    harness_timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Compile a planner draft into a TaskPlan payload with generated subgraphs.

    ``harness_binder`` decides what a milestone's acceptance gate runs, so a
    dataset shipping real developer-visible tests can gate on those instead of
    on contracts derived from a design document.
    """
    subtasks: list[dict[str, Any]] = []
    inputs, outputs = _artifact_io()
    total = len(draft.milestones)
    bind = harness_binder or realbench_harness_binder(
        workspace=Path(workspace), harness_dir=Path(harness_dir)
    )
    for index, milestone in enumerate(draft.milestones):
        graph_path, roster = materialize_milestone_subgraph(
            generated_root=Path(generated_root),
            milestone=milestone,
            agent_backend=agent_backend,
            harness_command=bind(milestone),
            model_name=model_name,
            profile=prompt_profile,
            benchmark=benchmark,
            harness_timeout_seconds=harness_timeout_seconds,
        )
        subtasks.append(
            {
                "subtask_id": milestone.milestone_id,
                "title": milestone.title,
                "objective": milestone.objective,
                "dependencies": list(milestone.depends_on),
                "priority": 100 - index,
                "keystone_harness_id": KEYSTONE_HARNESS,
                "local_graph_template": graph_path,
                "budget": _milestone_budget(milestone),
                "input_artifacts": inputs,
                "expected_outputs": outputs,
                "metadata": {
                    "role": milestone.role,
                    "milestone_brief": _milestone_brief(milestone, roster),
                    "focus_paths": list(milestone.focus_paths),
                    "public_harness_level": milestone.role,
                    "risk_rationale": milestone.risk_rationale,
                    "acceptance": milestone.acceptance.to_dict(),
                    "agent_roster": roster,
                    "milestone_index": index,
                    "milestone_count": total,
                },
            }
        )

    ids = [s["subtask_id"] for s in subtasks]
    dependents = {sid: 0 for sid in ids}
    edges: list[tuple[str, str]] = []
    for sub in subtasks:
        for dep in sub["dependencies"]:
            dependents[dep] = dependents.get(dep, 0) + 1
            edges.append((dep, sub["subtask_id"]))
    terminals = [sid for sid in ids if not dependents.get(sid)]
    terminal_id = terminals[-1] if terminals else ids[-1]

    return _assemble_plan(
        task_id=task_id,
        subtasks=subtasks,
        edges=edges,
        terminal_id=terminal_id,
        rationale=(
            "Risk-first dynamic milestone plan: "
            + (draft.rationale or "planner-authored milestone DAG")
        ),
        metadata={
            "benchmark": benchmark,
            "plan_builder": "build_plan_from_draft",
            "decomposition_source": draft.generator,
            "milestone_split": draft.split,
            "milestone_ids": ids,
            "agent_backend": agent_backend,
            "generated_root": str(generated_root),
            "harness_dir": str(harness_dir),
            "milestone_memory": "prompt_prelude",
        },
    )


def _bind_runner_harness(
    subtasks: list[dict[str, Any]],
    *,
    workspace: Path,
    generated_root: Path,
    harness_dir: Path,
) -> None:
    """Freeze per-milestone contracts and rebind graphs to runner-owned assets."""
    for sub in subtasks:
        role = str(sub["metadata"]["role"])
        _, contracts_path = materialize_milestone_contracts(
            Path(workspace),
            harness_dir=Path(harness_dir),
            role=role,  # type: ignore[arg-type]
            focus_paths=list(sub["metadata"].get("focus_paths") or []),
            milestone_id=sub["subtask_id"],
        )
        sub["local_graph_template"] = materialize_role_graph(
            template_path=sub["local_graph_template"],
            generated_root=Path(generated_root),
            milestone_id=sub["subtask_id"],
            harness_command=public_check_command(
                harness_dir=Path(harness_dir),
                level=role,
                contracts_path=contracts_path,
            ),
        )


def build_realbench_candidate_plan(
    *,
    task_id: str,
    workspace: Path,
    generated_root: Path,
    harness_dir: Path,
    graph_catalog: dict[str, str] | None = None,
    max_implementation_milestones: int = 2,
    force_split: bool | None = None,
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
    split = should_split_milestones(
        modules=modules,
        packages=packages,
        exports=exports,
        force_split=force_split,
    )

    subtasks: list[dict[str, Any]] = []
    if not split:
        # Single long-horizon milestone — prefer whole-repo coherence.
        sole_id = "implement_repository"
        export_hint = ", ".join(list(exports)[:12]) or "see public_design/package.json"
        subtasks.append(
            _subtask(
                subtask_id=sole_id,
                title="Implement full public repository",
                objective=(
                    f"For RealBench task `{task_id}`, read TASK.md, REQUIREMENTS.md "
                    "and public_design/. Implement the complete public repository "
                    "in one pass (scaffold + modules + UML exports). "
                    f"Public exports to honor when present: {export_hint}. "
                    "Must pass the integration acceptance check."
                ),
                role="integration",
                dependencies=[],
                priority=100,
                graph_catalog=catalog,
                focus_paths=packages[:8] or ["."],
            )
        )
        terminal_id = sole_id
        rationale = (
            "Single-milestone RealBench plan (adaptive: split unnecessary). "
            "Binds repository_test_harness to integration-level public contracts."
        )
    else:
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
                        "REQUIREMENTS.md and public_design/. Prefer completing public "
                        "modules over unrelated files. Must pass the implementation "
                        "acceptance check."
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
                        "Must pass the implementation acceptance check."
                    ),
                    role="implementation",
                    dependencies=[discovery_id],
                    priority=80,
                    graph_catalog=catalog,
                    focus_paths=[root],
                )
            )
        else:
            root = packages[0]
            groups = _second_level_groups(modules, root) or packages
            chunks: list[list[str]] = []
            primary = [root] + [f"{root}/{g}" for g in groups[:3]]
            chunks.append(primary)
            if len(groups) > 3 and max_implementation_milestones >= 2:
                chunks.append([f"{root}/{g}" for g in groups[3:8]])
            chunks = chunks[: max(1, max_implementation_milestones)]
            prev = discovery_id
            for idx, focus in enumerate(chunks):
                impl_id = f"implement_{_slug(focus[0], fallback=f'part{idx+1}')}"
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
                            + ". Keep previously committed modules working. Must pass "
                            "the implementation acceptance check."
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
                    f"Finalize `{task_id}` against public_design/: fix imports, "
                    "packaging, cross-module APIs, and UML exports. Must pass the "
                    "integration acceptance check."
                ),
                role="integration",
                dependencies=integration_deps,
                priority=10,
                graph_catalog=catalog,
                focus_paths=packages[:8],
            )
        )
        terminal_id = integration_id
        rationale = (
            "Split RealBench milestone plan from public_design (adaptive: complex tree). "
            "Each milestone binds repository_test_harness to role-graded public contracts."
        )

    _bind_runner_harness(
        subtasks,
        workspace=workspace,
        generated_root=generated_root,
        harness_dir=harness_dir,
    )
    ordered_ids = [s["subtask_id"] for s in subtasks]
    return _assemble_plan(
        task_id=task_id,
        subtasks=subtasks,
        edges=list(zip(ordered_ids, ordered_ids[1:], strict=False)),
        terminal_id=terminal_id,
        rationale=rationale,
        metadata={
            "benchmark": "realbench",
            "plan_builder": "build_realbench_candidate_plan",
            "decomposition_source": "public_design",
            "top_level_packages": packages,
            "module_count": len(modules),
            "export_keys": sorted(exports)[:32],
            "graph_catalog": catalog,
            "milestone_split": split,
            "harness_dir": str(harness_dir),
            "milestone_memory": "prompt_prelude",
        },
    )


def _assemble_plan(
    *,
    task_id: str,
    subtasks: list[dict[str, Any]],
    edges: list[tuple[str, str]],
    terminal_id: str,
    rationale: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Wrap milestone subtasks into the shared TaskPlan payload shape."""
    payload_contracts: list[dict[str, Any]] = []
    delivery_schedule: list[dict[str, Any]] = []
    targets: set[str] = set()
    for src, dst in edges:
        payload_id = f"handoff_{src}_to_{dst}"
        targets.add(dst)
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
        "decomposition_rationale": rationale,
        "decomposition_status": "ok",
        "subtasks": subtasks,
        "final_aggregation": {
            "strategy": "identity",
            "terminal_subtask_id": terminal_id,
        },
        "communication_plan": {
            "version": 1,
            "payload_contracts": payload_contracts,
            "delivery_schedule": delivery_schedule,
            "context_budgets": {sid: 24000 for sid in sorted(targets)},
        },
        "metadata": metadata,
    }
