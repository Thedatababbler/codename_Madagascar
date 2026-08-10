"""The fast loop's ceilings must come from the plan, not from constants.

Every one of these ceilings can end a tuning run early without saying so, and
the wall clock can do worse than end it: the candidate generator clamps each
candidate's timeout to it, so an undersized value tunes under a deadline the
baseline never ran with.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from orchestra.cli.run_codeprojecteval_decomp import _fast_loop_budget
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import AgentNodeSpec


def _graph_with_agents(tmp_path: Path, count: int, name: str) -> str:
    """A subgraph carrying `count` agent nodes, built off a real one."""
    graph = load_graph("configs/graphs/codex_single_implementer.yaml")
    agent = next(n for n in graph.nodes if isinstance(n, AgentNodeSpec))
    others = [n for n in graph.nodes if not isinstance(n, AgentNodeSpec)]
    agents = [
        agent.model_copy(update={"node_id": f"{agent.node_id}_{i}"})
        for i in range(count)
    ]
    graph = graph.model_copy(update={"nodes": [*agents, *others]})
    path = tmp_path / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump(graph.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    return str(path)


def _plan(tmp_path: Path, widths: list[int], timeouts: list[float]) -> TaskPlan:
    subtasks = [
        SubtaskSpec(
            subtask_id=f"m{i}",
            title=f"milestone {i}",
            objective="do the thing",
            keystone_harness_id="repository_test_harness",
            local_graph_template=_graph_with_agents(tmp_path, width, f"m{i}"),
            budget=BudgetSpec(timeout_seconds=timeout),
            dependencies=[f"m{i - 1}"] if i else [],
        )
        for i, (width, timeout) in enumerate(zip(widths, timeouts, strict=True))
    ]
    return TaskPlan(
        task_id="t",
        decomposition_rationale="r",
        decomposition_status="ok",
        subtasks=subtasks,
        final_aggregation={
            "strategy": "identity",
            "terminal_subtask_id": subtasks[-1].subtask_id,
        },
    )


def test_calls_are_counted_per_agent_not_per_candidate(tmp_path: Path) -> None:
    """A candidate on a four-agent milestone spends four calls, not one."""
    plan = _plan(tmp_path, widths=[4, 2], timeouts=[900.0, 900.0])

    budget = _fast_loop_budget(2, plan)

    # Two candidates on the widest milestone, plus the original run.
    assert budget.max_total_backend_calls >= 2 * 4
    # The old sizing was candidates * 2, which cannot even pay for one candidate.
    assert budget.max_total_backend_calls > 2 * 2


def test_the_wall_clock_leaves_room_for_every_candidate(tmp_path: Path) -> None:
    plan = _plan(tmp_path, widths=[2], timeouts=[1800.0])

    budget = _fast_loop_budget(2, plan)

    # Original attempt plus two candidates, each able to use the full milestone
    # deadline. Anything less silently shortens the candidates' timeout.
    assert budget.max_wall_time_seconds >= 1800 * 3


def test_attempts_allow_the_original_plus_each_candidate(tmp_path: Path) -> None:
    plan = _plan(tmp_path, widths=[1], timeouts=[600.0])

    assert _fast_loop_budget(3, plan).max_attempts_per_subtask >= 4


def test_zero_candidates_spends_nothing(tmp_path: Path) -> None:
    """The A/B arms run with the loop off and must stay that way."""
    plan = _plan(tmp_path, widths=[3], timeouts=[900.0])

    budget = _fast_loop_budget(0, plan)

    assert budget.max_candidates == 0
    assert budget.max_total_backend_calls == 0


def test_an_unreadable_subgraph_does_not_fail_the_run(tmp_path: Path) -> None:
    """Sizing a budget is not worth losing a run over."""
    plan = _plan(tmp_path, widths=[2], timeouts=[600.0])
    broken = plan.subtasks[0].model_copy(update={"local_graph_template": "nope.yaml"})
    plan = plan.model_copy(update={"subtasks": [broken]})

    budget = _fast_loop_budget(2, plan)

    assert budget.max_candidates == 2
    assert budget.max_total_backend_calls >= 2
