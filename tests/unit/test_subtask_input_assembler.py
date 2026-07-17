"""SubtaskInputAssembler must select declared dependency artifacts only."""

from __future__ import annotations

import pytest

from orchestra.backends.base import ArtifactRef
from orchestra.control.input_assembler import SubtaskInputAssembler
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.schemas.artifacts import ProblemArtifact, RepositoryChangeArtifact
from orchestra.storage.artifacts import FileArtifactStore


@pytest.mark.asyncio
async def test_downstream_subtask_receives_declared_artifacts(tmp_path):
    store = FileArtifactStore(tmp_path)
    assembler = SubtaskInputAssembler(store)
    problem = create_artifact(
        ProblemArtifact(
            question_id="t",
            title="t",
            statement="x",
            difficulty="easy",
            platform="fixture",
        ),
        producer_node_id="__input__",
        task_id="t",
    )
    upstream = create_artifact(
        RepositoryChangeArtifact(
            workspace_ref="ws",
            thread_id="th-1",
            changed_files=["calculator.py"],
            patch="diff --git a/calculator.py b/calculator.py\n",
            final_response="ok",
            source_node="s1",
        ),
        producer_node_id="s1",
        task_id="t",
    )
    await store.put(problem)
    await store.put(upstream)

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
                input_artifacts=[
                    ArtifactRef(
                        slot="problem",
                        artifact_id=problem.artifact_id,
                        artifact_type="ProblemArtifact",
                    )
                ],
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
                        slot="problem",
                        artifact_id=problem.artifact_id,
                        artifact_type="ProblemArtifact",
                    ),
                    ArtifactRef(
                        slot="upstream_change",
                        artifact_id=upstream.artifact_id,
                        artifact_type="RepositoryChangeArtifact",
                    ),
                ],
                expected_outputs=[],
            ),
        ],
    )
    state = TaskExecutionState.from_plan(plan)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].committed_artifacts = [
        ArtifactRef(
            slot="repo_change",
            artifact_id=upstream.artifact_id,
            artifact_type="RepositoryChangeArtifact",
        )
    ]
    state.subtasks["s1"].final_output_artifact_id = upstream.artifact_id

    root = ArtifactBundle(slots={"problem": problem})
    assembled = await assembler.assemble(
        task_plan=plan,
        task_state=state,
        subtask=plan.subtasks[1],
        root_artifacts=root,
    )
    assert "problem" in assembled.slots
    assert "upstream_change" in assembled.slots
    assert assembled.slots["upstream_change"].artifact_id == upstream.artifact_id
    assert "repo_change" in assembled.slots
