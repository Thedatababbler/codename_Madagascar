"""Resume must not re-execute committed subtasks."""

from __future__ import annotations

import json

import pytest

from orchestra.backends.factory import build_structured_llm_registry
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.single_subtask import SingleSubtaskCompatibilityRunner
from orchestra.control.task_state import SubtaskStatus
from orchestra.decomposition.fallback import build_single_subtask_plan
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.llm.mock_async import MockAsyncLLMClient
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import ProblemArtifact, PublicExample
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter

GRAPH = "configs/graphs/b0_direct.yaml"
CONTRACTS = "configs/contracts"


def _problem():
    return ProblemArtifact(
        question_id="echo",
        title="Echo",
        statement="Echo one value.",
        difficulty="easy",
        platform="synthetic",
        public_examples=[PublicExample(input="7\n", output="7\n")],
    )


@pytest.mark.asyncio
async def test_subtask_resume_skips_committed(tmp_path):
    contracts = load_contracts(CONTRACTS)
    responses = {
        "direct_coder": ["```python\n# CORRECT_SOLUTION\nprint(input())\n```"],
    }
    client = MockAsyncLLMClient(responses)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=4,
        max_parallel_llm_calls=8,
        max_parallel_sandboxes=1,
    )
    store = FileArtifactStore(tmp_path)
    runtime = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(
                contracts, build_structured_llm_registry(client)
            ),
            harness_executor=HarnessNodeExecutor(MockSandbox()),
        ),
        artifact_store=store,
        checkpoint_store=CheckpointStore(tmp_path),
        event_writer=AppendOnlyEventWriter(tmp_path),
    )
    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime,
        artifact_store=store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
    )
    plan = build_single_subtask_plan(
        task_id="echo",
        objective="echo",
        local_graph_template=GRAPH,
        keystone_harness_id="none",
    )
    initial = create_artifact(_problem(), producer_node_id="__input__", task_id="echo")
    context = RunContext(
        run_id="test",
        task_id="echo",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="contracts",
    )

    state1, result1 = await runner.run(
        plan=plan,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context,
    )
    assert result1 is not None
    assert state1.frozen
    assert state1.subtasks["main"].status is SubtaskStatus.COMMITTED
    assert client.call_counts["direct_coder"] == 1

    # Exhaust scripted responses so a re-run would fail if attempted.
    client = MockAsyncLLMClient({"direct_coder": []})
    runtime2 = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(
                contracts, build_structured_llm_registry(client)
            ),
            harness_executor=HarnessNodeExecutor(MockSandbox()),
        ),
        artifact_store=store,
        checkpoint_store=CheckpointStore(tmp_path),
        event_writer=AppendOnlyEventWriter(tmp_path),
    )
    runner2 = SingleSubtaskCompatibilityRunner(
        runtime=runtime2,
        artifact_store=store,
        task_checkpoint_store=TaskCheckpointStore(tmp_path),
        contracts_dir=CONTRACTS,
    )
    state2, result2 = await runner2.run(
        plan=plan,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context,
    )
    assert result2 is None
    assert state2.frozen
    assert state2.subtasks["main"].status is SubtaskStatus.COMMITTED
    assert sum(client.call_counts.values()) == 0
    assert (tmp_path / "tasks" / "echo" / "task_execution.json").exists()
    payload = json.loads(
        (tmp_path / "tasks" / "echo" / "task_execution.json").read_text(encoding="utf-8")
    )
    assert payload["subtasks"]["main"]["status"] == "committed"


@pytest.mark.asyncio
async def test_graph_still_compiles_under_runner_contracts():
    compiled = build_compiler(CONTRACTS).compile(load_graph(GRAPH))
    assert compiled.final_producers
