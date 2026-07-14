"""Single-subtask IR path matches direct NativeAsyncRuntime results."""

from __future__ import annotations

import pytest

from orchestra.backends.factory import build_structured_llm_registry
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.single_subtask import SingleSubtaskCompatibilityRunner
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
from orchestra.schemas.artifacts import FinalCodeArtifact, ProblemArtifact, PublicExample
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter

GRAPH = "configs/graphs/b0_direct.yaml"
CONTRACTS = "configs/contracts"


def _responses():
    return {
        "direct_coder": ["```python\n# CORRECT_SOLUTION\nprint(input())\n```"],
    }


def _problem():
    return ProblemArtifact(
        question_id="echo",
        title="Echo",
        statement="Echo one value.",
        difficulty="easy",
        platform="synthetic",
        public_examples=[PublicExample(input="7\n", output="7\n")],
    )


def _runtime(tmp_path, client):
    contracts = load_contracts(CONTRACTS)
    store = FileArtifactStore(tmp_path)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=4,
        max_parallel_llm_calls=8,
        max_parallel_sandboxes=1,
    )
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
    return runtime, store, limits


@pytest.mark.asyncio
async def test_single_subtask_matches_direct_runtime(tmp_path):
    direct_dir = tmp_path / "direct"
    ir_dir = tmp_path / "ir"
    direct_dir.mkdir()
    ir_dir.mkdir()

    client_direct = MockAsyncLLMClient(_responses())
    runtime_d, store_d, limits = _runtime(direct_dir, client_direct)
    graph = load_graph(GRAPH)
    compiled = build_compiler(CONTRACTS).compile(graph)
    initial = create_artifact(_problem(), producer_node_id="__input__", task_id="echo")
    context_d = RunContext(
        run_id="direct",
        task_id="echo",
        run_dir=direct_dir,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="contracts",
    )
    result_d = await runtime_d.execute(
        graph=compiled,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context_d,
    )
    final_d = FinalCodeArtifact.model_validate(
        (await store_d.get(result_d.state.final_output_artifact_id)).payload
    )

    client_ir = MockAsyncLLMClient(_responses())
    runtime_i, store_i, limits_i = _runtime(ir_dir, client_ir)
    runner = SingleSubtaskCompatibilityRunner(
        runtime=runtime_i,
        artifact_store=store_i,
        task_checkpoint_store=TaskCheckpointStore(ir_dir),
        contracts_dir=CONTRACTS,
    )
    plan = build_single_subtask_plan(
        task_id="echo",
        objective="echo",
        local_graph_template=GRAPH,
        keystone_harness_id="none",
    )
    context_i = RunContext(
        run_id="ir",
        task_id="echo",
        run_dir=ir_dir,
        limits=limits_i,
        semaphores=RuntimeSemaphores(limits_i),
        contract_hash="contracts",
    )
    state, result_i = await runner.run(
        plan=plan,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=context_i,
    )
    assert result_i is not None
    final_i = FinalCodeArtifact.model_validate(
        (await store_i.get(result_i.state.final_output_artifact_id)).payload
    )

    assert result_d.state.frozen is True
    assert state.frozen is True
    assert result_i.state.frozen is True
    assert final_d.code == final_i.code
    assert "CORRECT_SOLUTION" in final_i.code
    assert client_direct.call_counts["direct_coder"] == 1
    assert client_ir.call_counts["direct_coder"] == 1
