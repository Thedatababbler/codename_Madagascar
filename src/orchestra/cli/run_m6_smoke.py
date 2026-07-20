"""Deterministic M6 Pareto smoke (no real API keys)."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.pareto.archive import ParetoArchive
from orchestra.control.pareto.controller import ParetoGlobalCandidatePolicy
from orchestra.control.pareto.schemas import (
    ObjectiveDirection,
    ObjectiveSource,
    ObjectiveValue,
    ParetoConfig,
    ParetoEvaluationKind,
    ParetoObjectiveVector,
    ParetoOrchestraCandidate,
    ParetoSearchState,
    PreferenceProfile,
)
from orchestra.control.pareto.selector import DeterministicParetoSelector
from orchestra.control.slow_loop.controller import SlowLoopController
from orchestra.control.slow_loop.schemas import SlowLoopBudget, SlowLoopConfig
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"
OBJ = {
    "quality": ObjectiveDirection.MAXIMIZE,
    "cost": ObjectiveDirection.MINIMIZE,
    "latency": ObjectiveDirection.MINIMIZE,
    "risk": ObjectiveDirection.MINIMIZE,
}


def _vec(**kwargs) -> ParetoObjectiveVector:
    return ParetoObjectiveVector(
        values={
            k: ObjectiveValue(value=float(v), source=ObjectiveSource.ESTIMATED, available=True)
            for k, v in kwargs.items()
        },
        evaluation_kind=ParetoEvaluationKind.ESTIMATED,
    )


def _cand(cid: str, **kwargs) -> ParetoOrchestraCandidate:
    return ParetoOrchestraCandidate(
        candidate_id=cid,
        content_hash=cid,
        edit_signature=cid,
        context_id="smoke",
        global_candidate=None,
        objectives=_vec(**kwargs),
    )


async def _run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    # Synthetic estimated frontier: A/B/D retained, C dominated.
    archive = ParetoArchive(
        ParetoConfig(
            objectives={
                "quality": ObjectiveDirection.MAXIMIZE,
                "cost": ObjectiveDirection.MINIMIZE,
                "latency": ObjectiveDirection.MINIMIZE,
                "risk": ObjectiveDirection.MINIMIZE,
            }
        )
    )
    a = _cand("A", quality=1.0, cost=5.0, latency=2.0, risk=0.1)
    b = _cand("B", quality=0.6, cost=1.0, latency=4.0, risk=0.2)
    c = _cand("C", quality=0.5, cost=6.0, latency=3.0, risk=0.3)
    d = _cand("D", quality=0.7, cost=3.0, latency=0.5, risk=0.8)
    for cand in (a, b, c, d):
        archive.insert(cand)
    frontier = {x.content_hash for x in archive.frontier("smoke")}
    assert "C" not in frontier
    assert {"A", "B", "D"} <= frontier

    selected = DeterministicParetoSelector().select(
        [a, b, d], PreferenceProfile(profile_id="balanced_knee"), OBJ
    )
    assert selected is not None

    plan = TaskPlan(
        task_id="m6_smoke",
        plan_version=1,
        decomposition_rationale="m6 smoke",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="done",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="future",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="future",
                dependencies=["s2"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
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
                    required=False,
                    max_tokens=9000,
                    metadata={"slot": "comm:large"},
                )
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r_large", payload_id="large", enabled=True),
            ],
            context_budgets={"s3": 1200},
        ),
        metadata={
            "allowed_backend_assignments": {
                "coding": ["codex_sdk", "smolagents_code"],
                "s2": ["codex_sdk", "smolagents_code"],
                "s3": ["codex_sdk", "smolagents_code"],
            }
        },
    )
    state = TaskExecutionState.from_plan(plan)
    state.communication_plan = plan.communication_plan
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.PENDING
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
    state.pareto_state = ParetoSearchState(enabled=True)

    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    context = RunContext(
        run_id="m6_smoke",
        task_id="m6_smoke",
        run_dir=output,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True, max_candidates=8),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
    )
    ctrl = SlowLoopController(
        config=SlowLoopConfig(
            enabled=True,
            budget=SlowLoopBudget(
                context_pressure_ratio=0.5,
                min_commits_between_updates=1,
                max_candidates_per_update=8,
            ),
            allowed_backend_assignments={
                "coding": ["codex_sdk", "smolagents_code"],
            },
        ),
        candidate_policy=policy,
        checkpoint_store=TaskCheckpointStore(output),
    )
    result = await ctrl.maybe_update(
        task_plan=plan,
        state=state,
        context=context,
        leased_subtask_ids=set(),
    )
    store = TaskCheckpointStore(output)
    await store.save(state)
    loaded = await store.load(
        state.task_id,
        plan_version=state.task_plan.plan_version,
        plan_content_hash=state.task_plan.content_hash(),
        allow_config_drift=True,
    )
    summary = {
        "frontier": sorted(frontier),
        "balanced_selected": selected.content_hash,
        "slow_loop_message": result.message,
        "updated": result.updated,
        "checkpoint_restored": loaded is not None,
        "pareto_enabled": bool(
            loaded and loaded.pareto_state and getattr(loaded.pareto_state, "enabled", False)
        ),
    }
    (output / "m6_smoke_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="configs/experiments/m6_pareto_smoke.yaml",
        help="unused metadata config path (smoke is deterministic)",
    )
    parser.add_argument(
        "--output",
        default="outputs/m6_pareto_smoke",
        type=Path,
    )
    args = parser.parse_args()
    del args.config
    summary = asyncio.run(_run(args.output))
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
