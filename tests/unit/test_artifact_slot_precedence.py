"""Artifact slot precedence and equal-priority conflict fail-closed."""

from __future__ import annotations

import pytest

from orchestra.backends.base import ArtifactRef
from orchestra.control.input_assembler import (
    ArtifactSlotConflictPolicy,
    SubtaskInputAssembler,
    SubtaskInputAssemblyError,
)
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.schemas.artifacts import ProblemArtifact, RepositoryChangeArtifact
from orchestra.storage.artifacts import FileArtifactStore


def _problem(task_id: str, title: str):
    return create_artifact(
        ProblemArtifact(
            question_id=task_id,
            title=title,
            statement=title,
            difficulty="easy",
            platform="fixture",
        ),
        producer_node_id="__input__",
        task_id=task_id,
    )


def _repo(task_id: str, node: str, summary: str):
    return create_artifact(
        RepositoryChangeArtifact(
            workspace_ref="ws",
            thread_id=f"th-{node}",
            changed_files=["x.py"],
            patch=f"# {summary}\n",
            final_response=summary,
            source_node=node,
        ),
        producer_node_id=node,
        task_id=task_id,
    )


@pytest.mark.asyncio
async def test_only_committed_artifacts_are_assembled(tmp_path):
    store = FileArtifactStore(tmp_path)
    assembler = SubtaskInputAssembler(store)
    root = _problem("t", "root")
    cand = _repo("t", "s1", "candidate-only")
    await store.put(root)
    await store.put(cand)
    plan = TaskPlan(
        task_id="t",
        plan_version=1,
        decomposition_rationale="x",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="s1",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
        ],
    )
    state = TaskExecutionState.from_plan(plan)
    # S1 failed canonical commit — only candidate_artifacts present.
    state.subtasks["s1"].status = SubtaskStatus.FAILED
    state.subtasks["s1"].candidate_artifacts = [
        ArtifactRef(
            slot="plan",
            artifact_id=cand.artifact_id,
            artifact_type="RepositoryChangeArtifact",
        )
    ]
    assembled = await assembler.assemble(
        task_plan=plan,
        task_state=state,
        subtask=plan.subtasks[1],
        root_artifacts=ArtifactBundle(slots={"problem": root}),
    )
    assert "plan" not in assembled.slots


@pytest.mark.asyncio
async def test_artifact_slot_priority_is_deterministic(tmp_path):
    store = FileArtifactStore(tmp_path)
    assembler = SubtaskInputAssembler(store)
    root = _problem("t", "root-problem")
    dep = _repo("t", "s1", "dep")
    explicit = _repo("t", "s1", "explicit")
    await store.put(root)
    await store.put(dep)
    await store.put(explicit)
    plan = TaskPlan(
        task_id="t",
        plan_version=1,
        decomposition_rationale="x",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="s1",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                input_artifacts=[
                    ArtifactRef(
                        slot="plan",
                        artifact_id=explicit.artifact_id,
                        artifact_type="RepositoryChangeArtifact",
                    )
                ],
                expected_outputs=[],
            ),
        ],
    )
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].committed_artifacts = [
        ArtifactRef(
            slot="plan",
            artifact_id=dep.artifact_id,
            artifact_type="RepositoryChangeArtifact",
        )
    ]
    # Without explicit: implicit overrides root for slot plan.
    plan_no_sel = plan.subtasks[1].model_copy(update={"input_artifacts": []})
    assembled_imp = await assembler.assemble(
        task_plan=plan,
        task_state=state,
        subtask=plan_no_sel,
        root_artifacts=ArtifactBundle(slots={"plan": root, "problem": root}),
    )
    assert assembled_imp.slots["plan"].artifact_id == dep.artifact_id
    assert assembled_imp.slot_sources["plan"].source == "implicit_dependency"

    assembled_exp = await assembler.assemble(
        task_plan=plan,
        task_state=state,
        subtask=plan.subtasks[1],
        root_artifacts=ArtifactBundle(slots={"plan": root, "problem": root}),
    )
    assert assembled_exp.slots["plan"].artifact_id == explicit.artifact_id
    assert assembled_exp.slot_sources["plan"].source == "explicit_selector"


@pytest.mark.asyncio
async def test_equal_priority_slot_conflict_fails_closed(tmp_path):
    store = FileArtifactStore(tmp_path)
    assembler = SubtaskInputAssembler(
        store, conflict_policy=ArtifactSlotConflictPolicy.ERROR
    )
    root = _problem("t", "root")
    a = _repo("t", "s1", "a")
    b = _repo("t", "s2", "b")
    await store.put(root)
    await store.put(a)
    await store.put(b)
    plan = TaskPlan(
        task_id="t",
        plan_version=1,
        decomposition_rationale="x",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="s1",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="s2",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="s3",
                dependencies=["s1", "s2"],
                keystone_harness_id="repository_test_harness",
                local_graph_template="configs/graphs/codex_single_implementer.yaml",
                budget=BudgetSpec(),
                expected_outputs=[],
            ),
        ],
    )
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s2"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].committed_artifacts = [
        ArtifactRef(
            slot="plan",
            artifact_id=a.artifact_id,
            artifact_type="RepositoryChangeArtifact",
        )
    ]
    state.subtasks["s2"].committed_artifacts = [
        ArtifactRef(
            slot="plan",
            artifact_id=b.artifact_id,
            artifact_type="RepositoryChangeArtifact",
        )
    ]
    with pytest.raises(SubtaskInputAssemblyError) as exc:
        await assembler.assemble(
            task_plan=plan,
            task_state=state,
            subtask=plan.subtasks[2],
            root_artifacts=ArtifactBundle(slots={"problem": root}),
        )
    assert "plan" in str(exc.value)
    assert a.artifact_id in str(exc.value)
    assert b.artifact_id in str(exc.value)
