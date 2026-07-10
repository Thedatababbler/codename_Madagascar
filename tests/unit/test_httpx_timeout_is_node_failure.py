import httpx
import pytest

from orchestra.backends.factory import build_structured_llm_registry
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.contracts import AgentContract
from orchestra.ir.nodes import AgentNodeSpec, NodeKind
from orchestra.llm.base_async import AsyncLLMClient
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.sandbox.mock import MockSandbox


class _TimeoutClient(AsyncLLMClient):
    async def generate(self, **kwargs):
        raise httpx.ReadTimeout("simulated read timeout")


@pytest.mark.asyncio
async def test_httpx_read_timeout_becomes_node_failure(tmp_path):
    contract = AgentContract(
        contract_id="direct_coder",
        role="DirectCoder",
        system_prompt_template="x",
        user_prompt_template="{artifacts_json}",
        model="mock",
        temperature=0.0,
        max_tokens=16,
        timeout_seconds=5,
        input_schema="ProblemArtifact",
        output_schema="CodeArtifact",
        parser_id="python_code",
    )
    registry = NodeExecutorRegistry(
        agent_executor=AgentNodeExecutor(
            {"direct_coder": contract},
            build_structured_llm_registry(_TimeoutClient()),
        ),
        harness_executor=HarnessNodeExecutor(MockSandbox()),
    )
    limits = RuntimeLimits()
    context = RunContext(
        run_id="r",
        task_id="t",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )
    node = AgentNodeSpec(
        node_id="direct_coder",
        node_kind=NodeKind.AGENT,
        contract_id="direct_coder",
        input_slots={"problem": "ProblemArtifact"},
        output_slots={"code": "CodeArtifact"},
    )
    result = await registry.execute_safely(node, {}, context)
    assert not result.succeeded
    assert "ReadTimeout" in (result.error or "")
