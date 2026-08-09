"""Materialize one runtime subgraph per dynamic milestone.

A milestone subgraph is a chain of agent nodes over a single workspace, gated by
the public acceptance harness:

    agent_1 -> agent_2 -> ... -> repository_tests -> freeze_change

Each agent node owns a generated ``AgentContract`` recording its role, prompt,
token budget and step/time limits, so a milestone's agent roster is inspectable
evidence rather than an implicit prompt string.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from orchestra.realbench.milestone_planner import AgentDraft, MilestoneDraft
from orchestra.tools.repository_tools import REPOSITORY_TOOL_IDS

CONTRACT_PREFIX = "rbdyn"
GENERATED_CONTRACTS_DIRNAME = "contracts"
GENERATED_GRAPHS_DIRNAME = "graphs"

_CODEX_TOOLS: list[str] = []
_SMOLAGENTS_TOOLS: list[str] = list(REPOSITORY_TOOL_IDS)


@dataclass(frozen=True)
class DatasetPromptProfile:
    """Dataset-specific paragraphs of a generated agent's system prompt.

    Milestone structure is dataset-independent, but where the design lives and
    what the acceptance gate runs are not: telling a CodeProjectEval agent to
    read ``public_design/`` would send it looking for files that do not exist.
    """

    label: str
    read_first: str
    acceptance: str
    shipping: str


REALBENCH_PROMPT_PROFILE = DatasetPromptProfile(
    label="RealBench Python repository",
    read_first="Read TASK.md, REQUIREMENTS.md and public_design/ before editing.\n",
    acceptance=(
        "After you stop, AdaMAS runs a {role}-level acceptance check "
        "outside this repository (imports every module of the public tree at its "
        "documented path and asserts the UML-exported symbols are importable from "
        "there). Symbols must be reachable at the exact module path public_design "
        "declares, not only at their definition site.\n"
        "Re-export a package's public surface from its `__init__.py`: consumers "
        "import from the package path, so a subpackage's exported names must also "
        "be reachable at the parent package.\n"
    ),
    shipping=(
        "Ship only files the public tree describes: no notes, plans, logs, "
        "scratch directories, or duplicate copies of the package. Files you invent "
        "do not exist when your code is evaluated elsewhere, so nothing may import "
        "them.\n"
    ),
)

CODEPROJECTEVAL_PROMPT_PROFILE = DatasetPromptProfile(
    label="CodeProjectEval Python repository",
    read_first=(
        "Read docs/PRD.md, docs/architecture_design.md, the UML documents and "
        "docs/directory_tree.txt before editing. The repository also ships a "
        "visible `check_tests/` suite describing expected behaviour.\n"
    ),
    acceptance=(
        "After you stop, AdaMAS runs a {role}-level acceptance check outside this "
        "repository: it compiles the declared packages, imports every module in "
        "docs/directory_tree.txt, and at integration level runs the repository's "
        "own `check_tests/` suite.\n"
        "`check_tests/` is read-only evidence: editing, deleting, skipping or "
        "xfailing any of it fails the milestone outright. A separate held-out "
        "suite decides the final score, so implement the documented behaviour "
        "rather than special-casing the visible tests.\n"
        "Re-export a package's public surface from its `__init__.py`: consumers "
        "import from the package path, so a subpackage's exported names must also "
        "be reachable at the parent package.\n"
    ),
    shipping=(
        "Ship only files docs/directory_tree.txt describes: no notes, plans, logs, "
        "scratch directories, or duplicate copies of the package. Files you invent "
        "do not exist when your code is evaluated elsewhere, so nothing may import "
        "them.\n"
    ),
)


def default_model_name(agent_backend: str) -> str:
    if agent_backend == "smolagents_code":
        return os.getenv("SMOLAGENTS_MODEL", "gpt-5-mini")
    return os.getenv("CODEX_MODEL", "gpt-5.4")


def prepare_generated_root(run_dir: Path, *, base_contracts_dir: str | Path) -> Path:
    """Create a run-scoped generated root seeded with the base contracts."""
    root = Path(run_dir) / "generated"
    contracts_dir = root / GENERATED_CONTRACTS_DIRNAME
    contracts_dir.mkdir(parents=True, exist_ok=True)
    (root / GENERATED_GRAPHS_DIRNAME).mkdir(parents=True, exist_ok=True)
    for path in sorted(Path(base_contracts_dir).glob("*.yaml")):
        shutil.copy2(path, contracts_dir / path.name)
    return root


def generated_contracts_dir(generated_root: Path) -> Path:
    return Path(generated_root) / GENERATED_CONTRACTS_DIRNAME


def _acceptance_block(milestone: MilestoneDraft) -> str:
    lines: list[str] = []
    if milestone.acceptance.criteria:
        lines.append("Acceptance criteria:")
        lines.extend(f"- {item}" for item in milestone.acceptance.criteria)
    if milestone.acceptance.corner_cases:
        lines.append("Corner cases that must not regress:")
        lines.extend(f"- {item}" for item in milestone.acceptance.corner_cases)
    return "\n".join(lines)


def _system_prompt(
    *,
    milestone: MilestoneDraft,
    agent: AgentDraft,
    agent_backend: str,
    profile: DatasetPromptProfile = REALBENCH_PROMPT_PROFILE,
) -> str:
    editing = (
        "Modify files with the provided repository tools "
        "(list/read/write/apply_workspace_patch); only real workspace edits "
        "count, never a prose patch."
        if agent_backend == "smolagents_code"
        else "Edit the repository directly in your workspace; only the git diff counts."
    )
    risk = (
        f"Why this milestone is a gate: {milestone.risk_rationale}\n"
        if milestone.risk_rationale
        else ""
    )
    acceptance = _acceptance_block(milestone)
    acceptance_text = f"\n{acceptance}\n" if acceptance else ""
    focus = agent.focus_paths or milestone.focus_paths
    focus_text = (
        "\nFocus paths: " + ", ".join(f"`{p}`" for p in focus) + "\n" if focus else ""
    )
    return (
        f"You are `{agent.role_id}` on milestone `{milestone.milestone_id}` of a "
        f"{profile.label}.\n"
        f"Milestone objective: {milestone.objective}\n"
        f"{risk}"
        f"Your mandate: {agent.mandate}\n"
        f"{focus_text}"
        f"{acceptance_text}"
        f"{editing}\n"
        f"{profile.read_first}"
        "Contracts frozen by earlier milestones are load-bearing: other modules "
        "import them. Extend them, do not redesign or rename them.\n"
        f"{profile.acceptance.format(role=milestone.role)}"
        f"{profile.shipping}"
        "Do not access parent directories, look for reference implementations, or "
        "create subagents."
    )


def contract_id_for(*, milestone_id: str, role_id: str) -> str:
    return f"{CONTRACT_PREFIX}_{milestone_id}_{role_id}"[:120]


def materialize_agent_contract(
    *,
    contracts_dir: Path,
    milestone: MilestoneDraft,
    agent: AgentDraft,
    agent_backend: str,
    model_name: str | None = None,
    profile: DatasetPromptProfile = REALBENCH_PROMPT_PROFILE,
) -> dict[str, Any]:
    """Write one generated agent contract and return its roster entry."""
    contract_id = contract_id_for(
        milestone_id=milestone.milestone_id, role_id=agent.role_id
    )
    system_prompt = _system_prompt(
        milestone=milestone, agent=agent, agent_backend=agent_backend, profile=profile
    )
    user_prompt = (
        "Task artifacts:\n{artifacts_json}\n\n"
        f"Execute your mandate for milestone `{milestone.milestone_id}`, then stop. "
        "Do not create subagents."
    )
    tools = _SMOLAGENTS_TOOLS if agent_backend == "smolagents_code" else _CODEX_TOOLS
    payload = {
        "contract_id": contract_id,
        "role": agent.title or agent.role_id,
        "system_prompt_template": system_prompt,
        "user_prompt_template": user_prompt,
        "model": model_name or default_model_name(agent_backend),
        "temperature": 0.0,
        "max_tokens": agent.max_tokens,
        "timeout_seconds": agent.timeout_seconds,
        "allowed_tools": list(tools),
        "input_schema": "ProblemArtifact",
        "output_schema": "RepositoryChangeArtifact",
        "parser_id": "repository_change",
        "memory_scope": "local",
    }
    path = Path(contracts_dir) / f"{contract_id}.yaml"
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return {
        "contract_id": contract_id,
        "role_id": agent.role_id,
        "role": payload["role"],
        "contract_path": str(path),
        "prompt_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "prompt_chars": len(system_prompt),
        "max_tokens": agent.max_tokens,
        "max_steps": agent.max_steps,
        "timeout_seconds": agent.timeout_seconds,
        "focus_paths": list(agent.focus_paths),
    }


def _backend_block(*, agent_backend: str, agent: AgentDraft, role: str) -> dict[str, Any]:
    if agent_backend == "smolagents_code":
        return {
            "type": "smolagents_code",
            "max_steps": agent.max_steps,
            "executor_type": "local",
            "use_structured_outputs_internally": True,
            "require_git_diff": True,
            "public_harness_level": role,
            "additional_authorized_imports": [],
        }
    # Codex owns its own turn loop; steps stay 1 and live in the roster metadata.
    return {
        "type": "codex_sdk",
        "thread_policy": "fresh",
        "sandbox": "workspace_write",
        "approval_policy": "never",
        "require_git_diff": True,
        "max_steps": 1,
    }


def build_milestone_graph(
    *,
    milestone: MilestoneDraft,
    agent_backend: str,
    roster: list[dict[str, Any]],
    harness_command: list[str],
    model_name: str | None = None,
    benchmark: str = "realbench",
    harness_timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Build the graph payload chaining a milestone's agents into the gate."""
    model = model_name or default_model_name(agent_backend)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: list[str] = []

    for index, (agent, entry) in enumerate(zip(milestone.agents, roster, strict=True)):
        node_id = f"agent_{index + 1}_{agent.role_id}"[:64]
        node_ids.append(node_id)
        input_slots: dict[str, str] = {"problem": "ProblemArtifact"}
        if index > 0:
            input_slots["upstream_change"] = "RepositoryChangeArtifact"
        nodes.append(
            {
                "node_id": node_id,
                "node_kind": "agent",
                "contract_id": entry["contract_id"],
                "backend": _backend_block(
                    agent_backend=agent_backend, agent=agent, role=milestone.role
                ),
                "model": {
                    "provider": "openai_compatible",
                    "name": model,
                    "temperature": 0.0,
                    "max_tokens": agent.max_tokens,
                },
                "output_contract": {
                    "parser_id": "repository_change",
                    "output_schema": "RepositoryChangeArtifact",
                },
                "input_slots": input_slots,
                "output_slots": {"repository_change": "RepositoryChangeArtifact"},
                "timeout_seconds": agent.timeout_seconds,
            }
        )
        if agent_backend == "smolagents_code":
            nodes[-1]["tools"] = list(_SMOLAGENTS_TOOLS)
        if index > 0:
            edges.append(
                {
                    "edge_id": f"chain_{node_ids[index - 1]}_to_{node_id}",
                    "source_node": node_ids[index - 1],
                    "source_output": "repository_change",
                    "destination_node": node_id,
                    "destination_input": "upstream_change",
                }
            )

    terminal_agent = node_ids[-1]
    nodes.append(
        {
            "node_id": "repository_tests",
            "node_kind": "harness",
            "harness_id": "repository_test_harness",
            "visibility": "public",
            "command": list(harness_command),
            "timeout_seconds": harness_timeout_seconds,
            "input_slots": {"repository_change": "RepositoryChangeArtifact"},
            "output_slots": {"result": "RepositoryHarnessResultArtifact"},
        }
    )
    nodes.append(
        {
            "node_id": "freeze_change",
            "node_kind": "transform",
            "transform_id": "freeze_repository_change",
            "input_slots": {
                "repository_change": "RepositoryChangeArtifact",
                "gate": "RepositoryHarnessResultArtifact",
            },
            "output_slots": {"final_change": "RepositoryChangeArtifact"},
        }
    )
    edges.extend(
        [
            {
                "edge_id": "agent_to_tests",
                "source_node": terminal_agent,
                "source_output": "repository_change",
                "destination_node": "repository_tests",
                "destination_input": "repository_change",
            },
            {
                "edge_id": "agent_to_freeze",
                "source_node": terminal_agent,
                "source_output": "repository_change",
                "destination_node": "freeze_change",
                "destination_input": "repository_change",
            },
            {
                "edge_id": "tests_pass_to_freeze",
                "source_node": "repository_tests",
                "source_output": "result",
                "destination_node": "freeze_change",
                "destination_input": "gate",
                "condition": {"source_field": "passed", "operator": "is_true"},
            },
        ]
    )

    return {
        "graph_id": f"rb_dynamic_{milestone.milestone_id}"[:96],
        "version": "1.0",
        "initial_artifact_slots": {"problem": "ProblemArtifact"},
        "final_output_slot": "final_change",
        "metadata": {
            "role": milestone.role,
            "benchmark": benchmark,
            "topology": "dynamic_milestone_agent_chain",
            "public_harness_level": milestone.role,
            "agent_backend": agent_backend,
            "milestone_id": milestone.milestone_id,
            "risk_rationale": milestone.risk_rationale,
            "agent_roster": roster,
        },
        "nodes": nodes,
        "edges": edges,
    }


def materialize_role_graph(
    *,
    template_path: str | Path,
    generated_root: Path,
    milestone_id: str,
    harness_command: list[str],
) -> str:
    """Copy a static role graph, rebinding its harness to runner-owned assets.

    The fallback (template) plan reuses the checked-in role graphs, whose harness
    command points at a script inside the repository. That script no longer
    exists there, so the copy carries the absolute command instead.
    """
    payload = yaml.safe_load(Path(template_path).read_text(encoding="utf-8")) or {}
    nodes = payload.get("nodes") or []
    for node in nodes:
        if isinstance(node, dict) and node.get("node_kind") == "harness":
            node["command"] = list(harness_command)
    graphs_dir = Path(generated_root) / GENERATED_GRAPHS_DIRNAME
    graphs_dir.mkdir(parents=True, exist_ok=True)
    out = graphs_dir / f"{milestone_id}.yaml"
    out.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return str(out)


def materialize_milestone_subgraph(
    *,
    generated_root: Path,
    milestone: MilestoneDraft,
    agent_backend: str,
    harness_command: list[str],
    model_name: str | None = None,
    profile: DatasetPromptProfile = REALBENCH_PROMPT_PROFILE,
    benchmark: str = "realbench",
    harness_timeout_seconds: int = 180,
) -> tuple[str, list[dict[str, Any]]]:
    """Write contracts + graph for one milestone; return (graph path, roster)."""
    root = Path(generated_root)
    contracts_dir = generated_contracts_dir(root)
    contracts_dir.mkdir(parents=True, exist_ok=True)
    graphs_dir = root / GENERATED_GRAPHS_DIRNAME
    graphs_dir.mkdir(parents=True, exist_ok=True)

    roster = [
        materialize_agent_contract(
            contracts_dir=contracts_dir,
            milestone=milestone,
            agent=agent,
            agent_backend=agent_backend,
            model_name=model_name,
            profile=profile,
        )
        for agent in milestone.agents
    ]
    graph = build_milestone_graph(
        milestone=milestone,
        agent_backend=agent_backend,
        roster=roster,
        harness_command=harness_command,
        model_name=model_name,
        benchmark=benchmark,
        harness_timeout_seconds=harness_timeout_seconds,
    )
    graph_path = graphs_dir / f"{milestone.milestone_id}.yaml"
    graph_path.write_text(
        yaml.safe_dump(graph, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return str(graph_path), roster
