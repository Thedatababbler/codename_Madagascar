import json

import pytest

from orchestra.cli.validate_graph import build_compiler
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
from orchestra.runtime.state import NodeStatus
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import FinalCodeArtifact, ProblemArtifact, PublicExample
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter

CONTRACTS_DIR = "configs/contracts"


def _responses(direct_code="# CORRECT_SOLUTION\nprint(input())"):
    return {
        "direct_coder": [f"```python\n{direct_code}\n```"],
        "solution_coder": ["```python\n# CORRECT_SOLUTION\nprint(input())\n```"],
        "algorithm_analyst": [
            json.dumps(
                {
                    "problem_summary": "Echo.",
                    "algorithm": "Read and print.",
                    "data_structures": [],
                    "correctness_argument": "The same value is printed.",
                    "time_complexity": "O(1)",
                    "space_complexity": "O(1)",
                    "edge_cases": ["zero"],
                    "implementation_notes": [],
                }
            )
        ],
        "edge_case_analyst": [
            json.dumps(
                {
                    "input_output_interpretation": "stdin/stdout",
                    "edge_cases": ["zero"],
                    "overflow_risks": [],
                    "indexing_risks": [],
                    "interface_concerns": [],
                    "likely_failure_modes": [],
                }
            )
        ],
        "single_agent_repair": [
            json.dumps(
                {
                    "diagnosis": "wrong output",
                    "changes": ["echo input"],
                    "revised_code": "# CORRECT_SOLUTION\nprint(input())",
                }
            )
        ],
        "repair_agent": [
            json.dumps(
                {
                    "diagnosis": "wrong output",
                    "changes": ["echo input"],
                    "revised_code": "# CORRECT_SOLUTION\nprint(input())",
                }
            )
        ],
    }


async def _execute(tmp_path, graph_name, responses, *, delay=0.0, llm_limit=8):
    contracts = load_contracts(CONTRACTS_DIR)
    graph = load_graph(f"configs/graphs/{graph_name}.yaml")
    compiled = build_compiler(CONTRACTS_DIR).compile(graph)
    client = MockAsyncLLMClient(responses, delay_seconds=delay)
    sandbox = MockSandbox()
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=4,
        max_parallel_llm_calls=llm_limit,
        max_parallel_sandboxes=1,
    )
    store = FileArtifactStore(tmp_path)
    runtime = NativeAsyncRuntime(
        executors=NodeExecutorRegistry(
            agent_executor=AgentNodeExecutor(client, contracts),
            harness_executor=HarnessNodeExecutor(sandbox),
        ),
        artifact_store=store,
        checkpoint_store=CheckpointStore(tmp_path),
        event_writer=AppendOnlyEventWriter(tmp_path),
    )
    problem = ProblemArtifact(
        question_id="echo",
        title="Echo",
        statement="Echo one value.",
        difficulty="easy",
        platform="synthetic",
        public_examples=[PublicExample(input="7\n", output="7\n")],
    )
    initial = create_artifact(
        problem, producer_node_id="__input__", task_id="echo"
    )
    result = await runtime.execute(
        graph=compiled,
        initial_artifacts=ArtifactBundle(slots={"problem": initial}),
        context=RunContext(
            run_id="test",
            task_id="echo",
            run_dir=tmp_path,
            limits=limits,
            semaphores=RuntimeSemaphores(limits),
            contract_hash="contracts",
        ),
    )
    final = FinalCodeArtifact.model_validate(
        (await store.get(result.state.final_output_artifact_id)).payload
    )
    return result, final, client


@pytest.mark.asyncio
async def test_b0_exactly_one_llm_call(tmp_path):
    result, final, client = await _execute(
        tmp_path, "b0_direct", _responses()
    )
    assert result.state.frozen
    assert sum(client.call_counts.values()) == 1
    assert "CORRECT_SOLUTION" in final.code


@pytest.mark.asyncio
async def test_b1_public_pass_skips_repair(tmp_path):
    result, _final, client = await _execute(
        tmp_path, "b1_single_harness", _responses()
    )
    assert client.call_counts["single_agent_repair"] == 0
    assert result.state.node_status["same_coder_repair"] is NodeStatus.SKIPPED


@pytest.mark.asyncio
async def test_b1_public_failure_repairs_once(tmp_path):
    result, final, client = await _execute(
        tmp_path,
        "b1_single_harness",
        _responses("print('wrong')"),
    )
    assert result.state.frozen
    assert client.call_counts["single_agent_repair"] == 1
    assert "CORRECT_SOLUTION" in final.code


@pytest.mark.asyncio
async def test_b2_parallel_analysis_and_join(tmp_path):
    result, _final, client = await _execute(
        tmp_path, "b2_fixed_mas", _responses(), delay=0.1
    )
    assert result.state.frozen
    assert client.max_active_calls >= 2
    assert result.state.node_status["plan_merger"] is NodeStatus.SUCCEEDED
    assert result.parallel_node_count >= 2


@pytest.mark.asyncio
async def test_llm_semaphore_can_force_sequential_analysis(tmp_path):
    _result, _final, client = await _execute(
        tmp_path, "b2_fixed_mas", _responses(), delay=0.05, llm_limit=1
    )
    assert client.max_active_calls == 1


@pytest.mark.asyncio
async def test_resume_after_frozen_checkpoint_does_not_repeat_llm(tmp_path):
    result, _final, client = await _execute(
        tmp_path, "b0_direct", _responses()
    )
    assert result.state.checkpoint_count == 3
    before = sum(client.call_counts.values())
    resumed, _final2, _same_client = await _execute(
        tmp_path, "b0_direct", {"direct_coder": []}
    )
    assert resumed.state.frozen
    assert before == 1
