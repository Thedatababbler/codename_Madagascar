"""Deterministic M5 Slow Loop smoke (no real API keys)."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from orchestra.communication.payload import PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import (
    SlowLoopBudget,
    SlowLoopConfig,
    TaskSchedulingPolicy,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores


async def _run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    plan = TaskPlan(
        task_id="m5_smoke",
        plan_version=1,
        decomposition_rationale="m5 smoke",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="produce large artifact",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="unaffected running",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="receive revised payload",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="large",
                    source_subtask_id="s1",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    max_tokens=9000,
                    metadata={"required": False, "priority": 90},
                )
            ],
            context_budgets={"s3": 1200},
        ),
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.RUNNING
    state.subtasks["s2"].lease_status = "leased"
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
    state.scheduling_policy = TaskSchedulingPolicy()

    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=1,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
    )
    ctx = RunContext(
        run_id="m5_smoke",
        task_id="m5_smoke",
        run_dir=output,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="smoke",
    )
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                min_commits_between_updates=1,
                context_pressure_ratio=0.5,
            ),
        )
    )
    result = await ctrl.maybe_update(
        task_plan=state.task_plan,
        state=state,
        context=ctx,
        leased_subtask_ids={"s2"},
    )
    summary = {
        "updated": result.updated,
        "s2_unchanged": state.subtasks["s2"].lease_status == "leased",
        "communication_version": state.communication_plan.version,
        "active_revision": state.active_plan_revision_id,
        "message": result.message,
    }
    (output / "m5_smoke_result.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M5 Slow Loop deterministic smoke")
    parser.add_argument(
        "--config",
        default="configs/experiments/m5_slow_loop_smoke.yaml",
        help="Optional config path (metadata only for this smoke)",
    )
    parser.add_argument(
        "--output",
        default="outputs/m5_slow_loop_smoke",
        help="Output directory",
    )
    args = parser.parse_args(argv)
    summary = asyncio.run(_run(Path(args.output)))
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("updated") else 1


if __name__ == "__main__":
    raise SystemExit(main())
