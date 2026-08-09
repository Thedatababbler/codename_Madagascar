"""The role pool and the subgraph templates a planner selects from."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from orchestra.ir.compiler import GraphCompiler
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import OrchestraGraph
from orchestra.realbench.milestone_planner import parse_plan_payload, render_planner_prompt
from orchestra.realbench.subgraph_builder import (
    materialize_milestone_subgraph,
    prepare_generated_root,
)
from orchestra.roles.pool import RolePoolError, load_role_pool, parse_role
from orchestra.roles.templates import (
    SubgraphTemplateError,
    load_templates,
    parse_template,
    validate_against_pool,
)
from orchestra.runtime.scheduler import Scheduler
from orchestra.runtime.state import NodeStatus, RuntimeState


def _pool():
    return load_role_pool("configs/roles")


def _templates():
    return load_templates("configs/subgraph_templates", pool=_pool())


def test_every_shipped_role_carries_a_prompt_and_a_capability_line() -> None:
    pool = _pool()
    assert len(pool) >= 8
    for role in pool:
        assert role.prompt.strip(), role.role_id
        assert role.capability.strip(), role.role_id
    # The pool is useless for review templates without read-only members.
    assert any(not role.edits_repository for role in pool)


def test_role_without_a_prompt_is_rejected() -> None:
    with pytest.raises(RolePoolError):
        parse_role({"role_id": "ghost", "capability": "does things"})


def test_templates_load_and_name_only_pool_roles() -> None:
    templates = _templates()
    assert {"solo", "chain", "gate_then_repair", "review_then_fix"} <= set(templates)
    for template in templates.values():
        assert template.when_to_use.strip(), template.template_id


def test_template_may_not_fan_out_to_two_agents_that_edit() -> None:
    """Parallel writers would race on the milestone's single shared workspace."""
    template = parse_template(
        {
            "template_id": "racy",
            "slots": [
                {"id": "author", "default_role": "implementer"},
                {"id": "left", "default_role": "implementer"},
                {"id": "right", "default_role": "integrator"},
            ],
            "edges": [
                {"from": "author", "to": "left"},
                {"from": "author", "to": "right"},
            ],
        }
    )
    with pytest.raises(SubgraphTemplateError, match="parallel"):
        validate_against_pool(template, _pool())


def test_early_gate_requires_a_slot_that_waits_for_failure() -> None:
    with pytest.raises(SubgraphTemplateError, match="pointless"):
        parse_template(
            {
                "template_id": "pointless_gate",
                "slots": [{"id": "author", "default_role": "implementer"}],
                "edges": [],
                "early_gate_after": "author",
            }
        )


def test_planner_prompt_offers_the_pool_and_the_catalogue() -> None:
    from orchestra.realbench.milestone_planner import PlanningBrief

    brief = PlanningBrief(
        task_id="demo",
        documents=[("PRD", "build a thing")],
        modules=["pkg.mod"],
        exports={},
        acceptance_note="",
    )
    prompt = render_planner_prompt(brief=brief, agent_backend="codex_sdk")
    assert "`gate_then_repair`" in prompt
    assert "`spec_auditor`" in prompt
    assert "read-only" in prompt


def test_unknown_role_falls_back_to_the_slot_default() -> None:
    """An invented role must degrade to a runnable plan, not fail one."""
    plan = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "m",
                    "objective": "build",
                    "template_id": "review_then_fix",
                    "agents": [
                        {"slot": "author", "role": "implementer", "mandate": "a"},
                        {"slot": "reviewer", "role": "chief_vibes_officer", "mandate": "b"},
                        {"slot": "fixer", "role": "gate_repairer", "mandate": "c"},
                    ],
                }
            ]
        }
    )
    roles = [agent.role for agent in plan.milestones[0].agents]
    assert roles == ["implementer", "contract_critic", "gate_repairer"]


def test_role_outside_a_slots_allowed_set_falls_back_too() -> None:
    plan = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "m",
                    "objective": "build",
                    "template_id": "parallel_audit",
                    "agents": [
                        {"slot": "author", "role": "implementer", "mandate": "a"},
                        # A writing role in a read-only slot would race the audit.
                        {"slot": "spec_review", "role": "implementer", "mandate": "b"},
                        {"slot": "contract_review", "role": "contract_critic", "mandate": "c"},
                        {"slot": "fixer", "role": "gate_repairer", "mandate": "d"},
                    ],
                }
            ]
        },
        max_agents=4,
    )
    assert plan.milestones[0].agents[1].role == "spec_auditor"


def test_a_template_never_synthesises_an_agent_the_planner_did_not_ask_for() -> None:
    """A required slot describes the template's shape, not a budget entitlement.

    Filling one would hand the milestone an extra agent turn nobody planned,
    which in an A/B arm reads as a result rather than as unequal compute.
    """
    plan = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "m",
                    "objective": "build",
                    "template_id": "chain",
                    "agents": [{"slot": "first", "role": "implementer", "mandate": "a"}],
                }
            ]
        }
    )
    assert len(plan.milestones[0].agents) == 1


def test_a_dropped_repair_slot_falls_back_to_gating_once_at_the_end() -> None:
    payload = _materialize(
        "gate_then_repair", [{"slot": "author", "role": "implementer", "mandate": "a"}]
    )
    node_ids = [node["node_id"] for node in payload["nodes"]]
    assert "repository_tests_probe" not in node_ids
    assert "repository_tests" in node_ids
    _compile(payload)


def _materialize(template_id: str, agents: list[dict]) -> dict:
    plan = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "m",
                    "objective": "build it",
                    "risk_rationale": "r",
                    "template_id": template_id,
                    "agents": agents,
                }
            ]
        },
        max_agents=4,
    )
    root = prepare_generated_root(
        Path(tempfile.mkdtemp()), base_contracts_dir="configs/contracts"
    )
    path, _ = materialize_milestone_subgraph(
        generated_root=root,
        milestone=plan.milestones[0],
        agent_backend="codex_sdk",
        harness_command=["python", "check.py"],
    )
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _compile(payload: dict):
    generated = Path(payload["metadata"]["agent_roster"][0]["contract_path"]).parent
    compiler = GraphCompiler(
        contracts=load_contracts(str(generated)),
        harness_ids={"repository_test_harness"},
        transform_ids={"freeze_repository_change"},
        selector_ids=set(),
        backend_ids={"codex_sdk", "smolagents_code", "structured_llm"},
    )
    return compiler.compile(OrchestraGraph(**payload))


def test_generated_template_graphs_compile() -> None:
    for template_id, agents in [
        ("solo", [{"slot": "author", "role": "implementer", "mandate": "a"}]),
        (
            "gate_then_repair",
            [
                {"slot": "author", "role": "implementer", "mandate": "a"},
                {"slot": "repairer", "role": "gate_repairer", "mandate": "b"},
            ],
        ),
        (
            "parallel_audit",
            [
                {"slot": "author", "role": "implementer", "mandate": "a"},
                {"slot": "spec_review", "role": "spec_auditor", "mandate": "b"},
                {"slot": "contract_review", "role": "contract_critic", "mandate": "c"},
                {"slot": "fixer", "role": "gate_repairer", "mandate": "d"},
            ],
        ),
    ]:
        _compile(_materialize(template_id, agents))


def test_a_read_only_reviewer_is_not_asked_for_a_diff() -> None:
    payload = _materialize(
        "review_then_fix",
        [
            {"slot": "author", "role": "implementer", "mandate": "a"},
            {"slot": "reviewer", "role": "contract_critic", "mandate": "b"},
            {"slot": "fixer", "role": "gate_repairer", "mandate": "c"},
        ],
    )
    by_id = {node["node_id"]: node for node in payload["nodes"]}
    reviewer = next(key for key in by_id if "contract_critic" in key)
    author = next(key for key in by_id if "implementer" in key)
    assert by_id[reviewer]["backend"]["require_git_diff"] is False
    assert by_id[author]["backend"]["require_git_diff"] is True


def test_a_fan_in_agent_receives_both_upstream_reports() -> None:
    """One shared slot would resolve to whichever edge won the race."""
    payload = _materialize(
        "parallel_audit",
        [
            {"slot": "author", "role": "implementer", "mandate": "a"},
            {"slot": "spec_review", "role": "spec_auditor", "mandate": "b"},
            {"slot": "contract_review", "role": "contract_critic", "mandate": "c"},
            {"slot": "fixer", "role": "gate_repairer", "mandate": "d"},
        ],
    )
    fixer = next(n for n in payload["nodes"] if "gate_repairer" in n["node_id"])
    upstream = [slot for slot in fixer["input_slots"] if slot.startswith("upstream")]
    assert sorted(upstream) == ["upstream_change", "upstream_change_2"]


def _state(graph: OrchestraGraph) -> RuntimeState:
    state = RuntimeState(
        run_id="run",
        task_id="task",
        graph_id=graph.graph_id,
        graph_hash="hash",
        contract_hash="hash",
        node_status={node.node_id: NodeStatus.PENDING for node in graph.nodes},
        initial_artifacts={"problem": "artifact-problem"},
    )
    state.active_edges.update(
        edge.edge_id for edge in graph.edges if edge.condition is None
    )
    return state


def test_a_passing_early_gate_freezes_without_waking_the_repairer() -> None:
    payload = _materialize(
        "gate_then_repair",
        [
            {"slot": "author", "role": "implementer", "mandate": "a"},
            {"slot": "repairer", "role": "gate_repairer", "mandate": "b"},
        ],
    )
    graph = OrchestraGraph(**payload)
    scheduler = Scheduler(max_parallel_nodes=4)
    state = _state(graph)
    author = next(n.node_id for n in graph.nodes if "implementer" in n.node_id)
    repairer = next(n.node_id for n in graph.nodes if "gate_repairer" in n.node_id)

    state.node_status[author] = NodeStatus.SUCCEEDED
    state.node_outputs[author] = {"repository_change": "change-1"}
    state.node_status["repository_tests_probe"] = NodeStatus.SUCCEEDED
    state.node_outputs["repository_tests_probe"] = {"result": "probe-pass"}
    state.active_edges.add("probe_pass_to_freeze")
    state.inactive_edges.add("gate_fail_to_" + repairer)

    ready = scheduler.find_ready_nodes(graph, state)
    assert "freeze_change" in ready
    assert repairer not in ready
    resolved = scheduler.resolve_input_ids(graph, state, "freeze_change")
    assert resolved["repository_change"] == "change-1"


def test_a_failing_early_gate_wakes_the_repairer_and_freezes_its_change() -> None:
    payload = _materialize(
        "gate_then_repair",
        [
            {"slot": "author", "role": "implementer", "mandate": "a"},
            {"slot": "repairer", "role": "gate_repairer", "mandate": "b"},
        ],
    )
    graph = OrchestraGraph(**payload)
    scheduler = Scheduler(max_parallel_nodes=4)
    state = _state(graph)
    author = next(n.node_id for n in graph.nodes if "implementer" in n.node_id)
    repairer = next(n.node_id for n in graph.nodes if "gate_repairer" in n.node_id)

    state.node_status[author] = NodeStatus.SUCCEEDED
    state.node_outputs[author] = {"repository_change": "change-1"}
    state.node_status["repository_tests_probe"] = NodeStatus.SUCCEEDED
    state.node_outputs["repository_tests_probe"] = {"result": "probe-fail"}
    state.active_edges.add("gate_fail_to_" + repairer)
    state.inactive_edges.add("probe_pass_to_freeze")

    assert repairer in scheduler.find_ready_nodes(graph, state)
    assert "freeze_change" not in scheduler.find_ready_nodes(graph, state)

    state.node_status[repairer] = NodeStatus.SUCCEEDED
    state.node_outputs[repairer] = {"repository_change": "change-2"}
    state.node_status["repository_tests"] = NodeStatus.SUCCEEDED
    state.node_outputs["repository_tests"] = {"result": "final-pass"}
    state.active_edges.add("tests_pass_to_freeze")

    resolved = scheduler.resolve_input_ids(graph, state, "freeze_change")
    # The repaired change must win over the one that failed the probe.
    assert resolved["repository_change"] == "change-2"
    assert resolved["gate"] == "final-pass"
