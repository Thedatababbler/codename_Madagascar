"""Deterministic M5 Slow Loop smoke (no real API keys)."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from orchestra.communication.delivery import CommunicationDeliveryEngine
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
from orchestra.ir.artifacts import create_artifact
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore


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

    store = FileArtifactStore(output / "artifacts")
    source = create_artifact(
        FinalAnswerArtifact(answer="m5-smoke-payload", source_node="s1"),
        producer_node_id="s1",
        task_id="m5_smoke",
    )
    await store.put(source)
    state.subtasks["s1"].final_output_artifact_id = source.artifact_id

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
    pre_version = state.communication_plan.version
    pre_tokens = state.communication_plan.payload_contracts[0].max_tokens
    result = await ctrl.maybe_update(
        task_plan=state.task_plan,
        state=state,
        context=ctx,
        leased_subtask_ids={"s2"},
    )

    # Assert actual post-revision delivery feasibility, not only version churn.
    engine = CommunicationDeliveryEngine(store)
    delivery = await engine.deliver_for_target(
        task_plan=state.task_plan,
        task_state=state,
        communication_plan=state.communication_plan,
        target_subtask_id="s3",
    )
    post_contract = next(
        c for c in state.communication_plan.payload_contracts if c.payload_id == "large"
    )
    applied_hist = [
        r
        for r in state.plan_revision_history
        if getattr(r, "revision_id", None) == state.active_plan_revision_id
    ]
    slow_hist = [
        r
        for r in state.slow_loop_history
        if r.record_id == state.active_plan_revision_id
    ]
    summary = {
        "updated": result.updated,
        "s2_unchanged": state.subtasks["s2"].lease_status == "leased",
        "communication_version": state.communication_plan.version,
        "communication_version_bumped": state.communication_plan.version > pre_version,
        "payload_max_tokens_before": pre_tokens,
        "payload_max_tokens_after": post_contract.max_tokens,
        "payload_shrunk": post_contract.max_tokens < pre_tokens,
        "active_revision": state.active_plan_revision_id,
        "post_revision_delivery_blocked": bool(delivery.blocked),
        "post_revision_delivery_ok": not bool(delivery.blocked),
        "plan_revision_history_count": len(applied_hist),
        "slow_loop_history_count": len(slow_hist),
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
    ok = bool(
        summary.get("updated")
        and summary.get("post_revision_delivery_ok")
        and summary.get("payload_shrunk")
        and summary.get("plan_revision_history_count") == 1
        and summary.get("slow_loop_history_count") == 1
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
