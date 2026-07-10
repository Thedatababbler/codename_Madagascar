import pytest

from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.ir.artifacts import create_artifact
from orchestra.ir.nodes import HarnessNodeSpec, NodeKind
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.sandbox.mock import MockSandbox
from orchestra.schemas.artifacts import CodeArtifact, ProblemArtifact


@pytest.mark.asyncio
async def test_harness_without_public_tests_does_not_auto_pass(tmp_path):
    problem = ProblemArtifact(
        question_id="empty",
        title="No public tests",
        statement="Return 0.",
        difficulty="easy",
        platform="synthetic",
        public_examples=[],
    )
    code = CodeArtifact(code="print(0)")
    limits = RuntimeLimits()
    executor = HarnessNodeExecutor(MockSandbox(), timeout_seconds=6)
    context = RunContext(
        run_id="r",
        task_id="empty",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="c",
    )
    node = HarnessNodeSpec(
        node_id="public_harness",
        node_kind=NodeKind.HARNESS,
        harness_id="public_code_harness",
        input_slots={"problem": "ProblemArtifact", "code": "CodeArtifact"},
        output_slots={"result": "PublicHarnessResultArtifact"},
    )
    result = await executor.execute(
        node,
        {
            "problem": create_artifact(
                problem, producer_node_id="p", task_id="empty"
            ),
            "code": create_artifact(code, producer_node_id="c", task_id="empty"),
        },
        context,
    )
    payload = next(iter(result.outputs.values())).payload
    assert payload["harness_available"] is False
    assert payload["repair_eligible"] is False
    assert payload["passed"] is False
    assert payload["pass_ratio"] == 0.0
    assert "No public tests available" in payload["failure_summary"]["errors"]
