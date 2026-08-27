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
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.graph_invariants import assert_graph_invariants
from orchestra.realbench.milestone_planner import AgentDraft, MilestoneDraft
from orchestra.roles.pool import RolePool, default_role_pool
from orchestra.roles.templates import (
    FALLBACK_TEMPLATE_ID,
    SubgraphTemplate,
    default_templates,
)
from orchestra.tools.repository_tools import REPOSITORY_TOOL_IDS

CONTRACT_PREFIX = "rbdyn"
GENERATED_CONTRACTS_DIRNAME = "contracts"
GENERATED_GRAPHS_DIRNAME = "graphs"

_CODEX_TOOLS: list[str] = []
_SMOLAGENTS_TOOLS: list[str] = list(REPOSITORY_TOOL_IDS)

# The authored suite is a directory no design document mentions, and the shipping
# rule tells an agent to ship only what the documents describe. Left implicit,
# that rule is read as an instruction to delete the suite: the builders in the
# first test-first run each removed it before the gate could copy it out, which
# left the milestone with no behavioural score at all. The author is therefore
# told, in as many words, that this one directory is expected to be there.
#
# Only the author. Every later agent runs after custody has moved the suite out
# of the workspace, so there is nothing for them to preserve, read, or delete —
# and nothing to say to them about it. Naming a directory they cannot see would
# only invite them to look for it.
_AUTHORED_SUITE_EXPECTED = (
    "`spec_tests/` is the exception to the rule above: it is written for this "
    "milestone rather than described by the documents, and it must be present in "
    "the repository when you stop. It is then moved out of the workspace and "
    "becomes the fixed yardstick this milestone is scored against; no later "
    "agent sees it.\n"
)


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
    #: Names this profile in a compiled graph's metadata. Two CodeProjectEval
    #: profiles share a ``label``, so the label cannot identify one, and a
    #: plan-layer candidate recompiling a milestone has to reach the same prompt
    #: the original agents were given.
    profile_id: str = ""


REALBENCH_PROMPT_PROFILE = DatasetPromptProfile(
    profile_id="realbench",
    label="RealBench Python repository",
    read_first="Read TASK.md, REQUIREMENTS.md and public_design/ before editing.\n",
    acceptance=(
        "After you stop, AdaMAS runs a {role}-level acceptance check "
        "outside this repository: it compiles the declared packages, imports every "
        "module of the public tree at its documented path, and asserts the "
        "UML-exported symbols are importable from there. That check is "
        "structural: it asks whether the symbols exist, not whether they behave. "
        "Symbols must be reachable at the exact module path public_design "
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
    profile_id="codeprojecteval",
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

# One Codex agent, design documents only. Used as the single-agent baseline:
# no AdaMAS gate description, no authored-suite language, no hidden-suite
# language. The dataset may still ship a visible `check_tests/` directory;
# this profile does not name it, so the agent is not pointed at a yardstick.
CODEPROJECTEVAL_SOLO_BASELINE_PROFILE = DatasetPromptProfile(
    profile_id="codeprojecteval_solo_baseline",
    label="CodeProjectEval Python repository",
    read_first=(
        "Read docs/PRD.md, docs/architecture_design.md, the UML documents and "
        "docs/directory_tree.txt before editing.\n"
    ),
    acceptance="",
    shipping=(
        "Ship only files docs/directory_tree.txt describes: no notes, plans, logs, "
        "scratch directories, or duplicate copies of the package. Files you invent "
        "do not exist when your code is evaluated elsewhere, so nothing may import "
        "them.\n"
    ),
)


#: Every profile a compiled graph may name, so one can be recovered from its id.
PROMPT_PROFILES: dict[str, DatasetPromptProfile] = {
    profile.profile_id: profile
    for profile in (
        REALBENCH_PROMPT_PROFILE,
        CODEPROJECTEVAL_PROMPT_PROFILE,
        CODEPROJECTEVAL_SOLO_BASELINE_PROFILE,
    )
}


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
    pool: RolePool | None = None,
) -> str:
    role = (pool or default_role_pool()).get(agent.role)
    if role is not None and not role.edits_repository:
        editing = (
            "You do not edit this repository. Read it, and write your findings "
            "into your final answer; the agent after you acts on that text and "
            "sees nothing else of your work."
        )
    elif agent_backend == "smolagents_code":
        editing = (
            "Modify files with the provided repository tools "
            "(list/read/write/apply_workspace_patch); only real workspace edits "
            "count, never a prose patch."
        )
    else:
        editing = (
            "Edit the repository directly in your workspace; only the git diff counts."
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
    role_block = f"{role.prompt.strip()}\n\n" if role is not None else ""
    title = role.title if role is not None else agent.role_id
    edits = role is None or role.edits_repository
    shipping = profile.shipping if edits else ""
    suite = _AUTHORED_SUITE_EXPECTED if edits and agent.role == "test_author" else ""
    return (
        f"You are the {title} on milestone `{milestone.milestone_id}` of a "
        f"{profile.label}.\n\n"
        f"{role_block}"
        f"Milestone objective: {milestone.objective}\n"
        f"{risk}"
        f"Your mandate: {agent.mandate}\n"
        f"{focus_text}"
        f"{acceptance_text}"
        f"{editing}\n"
        f"{profile.read_first}"
        "Contracts frozen by earlier milestones are load-bearing: other modules "
        "import them. Extend them, do not redesign or rename them.\n"
        f"{profile.acceptance.format(role=milestone.gate_level)}"
        f"{shipping}"
        f"{suite}"
        "Do not access parent directories, look for reference implementations, or "
        "create subagents."
    )


def contract_id_for(
    *, milestone_id: str, role_id: str, namespace: str = ""
) -> str:
    """Contract id for one agent of one milestone.

    ``namespace`` separates several compilations of the same milestone, which a
    plan-layer search needs: two candidates filling the same slot with different
    roles would otherwise overwrite each other's contract on disk, and whichever
    ran second would score the other's prompt.
    """
    parts = [CONTRACT_PREFIX, milestone_id]
    if namespace:
        parts.append(namespace)
    parts.append(role_id)
    return "_".join(parts)[:120]


def materialize_agent_contract(
    *,
    contracts_dir: Path,
    milestone: MilestoneDraft,
    agent: AgentDraft,
    agent_backend: str,
    model_name: str | None = None,
    profile: DatasetPromptProfile = REALBENCH_PROMPT_PROFILE,
    pool: RolePool | None = None,
    contract_namespace: str = "",
) -> dict[str, Any]:
    """Write one generated agent contract and return its roster entry."""
    contract_id = contract_id_for(
        milestone_id=milestone.milestone_id,
        role_id=agent.role_id,
        namespace=contract_namespace,
    )
    system_prompt = _system_prompt(
        milestone=milestone,
        agent=agent,
        agent_backend=agent_backend,
        profile=profile,
        pool=pool,
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
        "node_id": agent.role_id,
        "role": agent.role,
        "slot": agent.slot_id,
        "role_id": agent.role_id,
        "title": payload["role"],
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


def _harness_node(
    *, node_id: str, harness_command: list[str], timeout_seconds: int
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_kind": "harness",
        "harness_id": "repository_test_harness",
        "visibility": "public",
        "command": list(harness_command),
        "timeout_seconds": timeout_seconds,
        "input_slots": {"repository_change": "RepositoryChangeArtifact"},
        "output_slots": {"result": "RepositoryHarnessResultArtifact"},
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
    template: SubgraphTemplate | None = None,
    pool: RolePool | None = None,
    variant: str = "",
) -> dict[str, Any]:
    """Compile a milestone's chosen template into a runnable graph payload.

    The template supplies the shape; the milestone's agents supply which role
    fills each slot. An ``early_gate_after`` template gates midway and wires the
    remaining slots behind a *failing* result, so a milestone that passes first
    time freezes immediately and never pays for them.
    """
    templates = default_templates()
    shape = template or templates.get(milestone.template_id) or templates[FALLBACK_TEMPLATE_ID]
    role_pool = pool or default_role_pool()
    model = model_name or default_model_name(agent_backend)

    agents = list(milestone.agents)
    slots = shape.slots_for(len(agents))
    slot_edges = shape.edges_for(slots)
    by_slot_id = {slot.slot_id: slot for slot in slots}
    # Agents were filled slot-by-slot at plan time, but an optional slot may be
    # absent, so bind by name and fall back to declaration order.
    node_for_slot: dict[str, str] = {}
    bound: list[tuple[str, Any, dict[str, Any]]] = []
    for index, (agent, entry) in enumerate(zip(agents, roster, strict=True)):
        slot_id = agent.slot_id if agent.slot_id in by_slot_id else slots[index].slot_id
        node_id = f"agent_{index + 1}_{agent.role_id}"[:64]
        node_for_slot[slot_id] = node_id
        bound.append((slot_id, agent, entry))

    incoming: dict[str, list[str]] = {}
    for src, dst in slot_edges:
        if src in node_for_slot and dst in node_for_slot:
            incoming.setdefault(dst, []).append(src)

    # An early gate is only meaningful when a slot is actually waiting behind a
    # failure; if the plan dropped that slot, gate once at the end as usual.
    early_slot = (
        shape.early_gate_after
        if shape.early_gate_after in node_for_slot
        and any(by_slot_id[slot_id].runs_if_gate_failed for slot_id, _, _ in bound)
        else None
    )
    probe_id = "repository_tests_probe"

    # A suite the implementer can read is a suite it satisfies completely, which
    # is no way to tell two implementations apart (EXP-20260811-01). So the
    # author's work is taken out of the workspace before anyone can build
    # against it, by a harness step wired between them. Two things have to go:
    # the files, which custody deletes, and the author's own change artifact,
    # whose patch would otherwise quote the whole suite into the next agent's
    # prompt — so consumers wait on the custody report instead of that patch.
    custody_slot = next((s for s, agent, _ in bound if agent.role == "test_author"), None)
    if custody_slot is not None and (
        "--spec-tests" not in harness_command
        or not any(custody_slot in sources for sources in incoming.values())
    ):
        custody_slot = None
    custody_id = "authored_suite_custody"

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for slot_id, agent, entry in bound:
        node_id = node_for_slot[slot_id]
        slot = by_slot_id[slot_id]
        role = role_pool.get(agent.role) or role_pool.require("implementer")
        input_slots: dict[str, str] = {"problem": "ProblemArtifact"}
        # Distinct slot names per upstream: one shared slot would resolve to the
        # first active edge only, so a fan-in agent would silently see one of
        # its two upstream reports.
        sources = incoming.get(slot_id, [])
        for position, source_slot in enumerate(s for s in sources if s != custody_slot):
            name = "upstream_change" if position == 0 else f"upstream_change_{position + 1}"
            input_slots[name] = "RepositoryChangeArtifact"
            edges.append(
                {
                    "edge_id": f"link_{node_for_slot[source_slot]}_to_{node_id}"[:96],
                    "source_node": node_for_slot[source_slot],
                    "source_output": "repository_change",
                    "destination_node": node_id,
                    "destination_input": name,
                }
            )
        if custody_slot is not None and custody_slot in sources:
            # Unconditional, and the reason this node cannot start early: the
            # suite has to be out of the workspace before the implementer looks
            # at it, and an input it must wait for is how that is enforced.
            input_slots["suite_custody"] = "RepositoryHarnessResultArtifact"
            edges.append(
                {
                    "edge_id": f"custody_to_{node_id}"[:96],
                    "source_node": custody_id,
                    "source_output": "result",
                    "destination_node": node_id,
                    "destination_input": "suite_custody",
                }
            )
        if slot.runs_if_gate_failed and early_slot:
            # The only input that cannot resolve unless the probe failed, which
            # is what keeps this node out of the run on the happy path.
            input_slots["gate_report"] = "RepositoryHarnessResultArtifact"
            edges.append(
                {
                    "edge_id": f"gate_fail_to_{node_id}"[:96],
                    "source_node": probe_id,
                    "source_output": "result",
                    "destination_node": node_id,
                    "destination_input": "gate_report",
                    "condition": {"source_field": "passed", "operator": "is_false"},
                }
            )
        backend = _backend_block(
            agent_backend=agent_backend, agent=agent, role=milestone.gate_level
        )
        if not role.edits_repository:
            # A reviewer that reports instead of editing produces no diff, and a
            # backend that demands one would score its correct behaviour a
            # failure.
            backend["require_git_diff"] = False
        node: dict[str, Any] = {
            "node_id": node_id,
            "node_kind": "agent",
            "contract_id": entry["contract_id"],
            "backend": backend,
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
        if agent_backend == "smolagents_code":
            node["tools"] = list(_SMOLAGENTS_TOOLS)
        nodes.append(node)

    if custody_slot is not None:
        nodes.append(
            _harness_node(
                node_id=custody_id,
                harness_command=[*harness_command, "--take-custody"],
                timeout_seconds=harness_timeout_seconds,
            )
        )
        edges.append(
            {
                "edge_id": "author_to_custody",
                "source_node": node_for_slot[custody_slot],
                "source_output": "repository_change",
                "destination_node": custody_id,
                "destination_input": "repository_change",
            }
        )

    terminal_slot = bound[-1][0]
    terminal_agent = node_for_slot[terminal_slot]
    nodes.append(
        _harness_node(
            node_id="repository_tests",
            harness_command=harness_command,
            timeout_seconds=harness_timeout_seconds,
        )
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
            # Listed before the early-gate alternatives: the first active edge
            # carrying a payload wins, and the latest change is the right one.
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

    if early_slot:
        early_agent = node_for_slot[early_slot]
        nodes.insert(
            len(bound),
            _harness_node(
                node_id=probe_id,
                harness_command=harness_command,
                timeout_seconds=harness_timeout_seconds,
            ),
        )
        edges.extend(
            [
                {
                    "edge_id": "early_agent_to_probe",
                    "source_node": early_agent,
                    "source_output": "repository_change",
                    "destination_node": probe_id,
                    "destination_input": "repository_change",
                },
                {
                    "edge_id": "probe_pass_to_freeze",
                    "source_node": probe_id,
                    "source_output": "result",
                    "destination_node": "freeze_change",
                    "destination_input": "gate",
                    "condition": {"source_field": "passed", "operator": "is_true"},
                },
                {
                    "edge_id": "early_agent_to_freeze",
                    "source_node": early_agent,
                    "source_output": "repository_change",
                    "destination_node": "freeze_change",
                    "destination_input": "repository_change",
                },
            ]
        )

    graph_id = f"rb_dynamic_{milestone.milestone_id}" + (f"__{variant}" if variant else "")
    metadata: dict[str, Any] = {
        "gate_level": milestone.gate_level,
        "role": milestone.gate_level,
        "benchmark": benchmark,
        "topology": f"template:{shape.template_id}",
        "template_id": shape.template_id,
        "public_harness_level": milestone.gate_level,
        "agent_backend": agent_backend,
        "milestone_id": milestone.milestone_id,
        "risk_rationale": milestone.risk_rationale,
        "agent_roster": roster,
    }
    if variant:
        # Omitted when empty so a plain compilation's payload — and therefore its
        # content hash — is exactly what it was before variants existed.
        metadata["variant"] = variant
    payload = {
        "graph_id": graph_id[:96],
        "version": "1.0",
        "initial_artifact_slots": {"problem": "ProblemArtifact"},
        "final_output_slot": "final_change",
        "metadata": metadata,
        "nodes": nodes,
        "edges": edges,
    }
    # Raised rather than returned: this layer composes the graph from a template
    # whose own rules were checked at load time, so a violation here is a defect
    # in the compiler and not something a caller can be handed to recover from.
    assert_graph_invariants(
        OrchestraGraph.model_validate(payload),
        pool=role_pool,
        expected_template_id=shape.template_id,
    )
    return payload


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
    contract_namespace: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """Write contracts + graph for one milestone; return (graph path, roster).

    ``contract_namespace`` separates repeated compilations of one milestone, so a
    plan-layer candidate does not overwrite the contracts or the graph of the
    attempt it is being compared against.
    """
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
            contract_namespace=contract_namespace,
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
        variant=contract_namespace,
    )
    stem = milestone.milestone_id + (f"__{contract_namespace}" if contract_namespace else "")
    draft_path = graphs_dir / f"{stem}.draft.json"
    draft_path.write_text(
        json.dumps(milestone.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    # A compiled graph does not contain the milestone it was compiled from: the
    # objective, the acceptance checks and each agent's mandate go into contract
    # prompts and are not recoverable from the nodes. The fast loop is handed a
    # graph and nothing else, so a candidate that wants a different template has
    # to be able to find its way back to the draft, the directory the contracts
    # live in, and the prompt profile the agents were written against.
    graph["metadata"].update(
        {
            "milestone_draft_path": str(draft_path),
            "generated_root": str(root),
            "prompt_profile": profile.profile_id,
        }
    )
    graph_path = graphs_dir / f"{stem}.yaml"
    graph_path.write_text(
        yaml.safe_dump(graph, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return str(graph_path), roster
