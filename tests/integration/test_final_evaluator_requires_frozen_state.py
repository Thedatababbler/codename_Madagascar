import pytest

from orchestra.adapters.livecodebench.final_evaluator import FinalLCBEvaluator
from orchestra.adapters.livecodebench.loader import PrivateTestRepository
from orchestra.runtime.state import NodeStatus, RuntimeState
from orchestra.schemas.artifacts import PublicExample
from orchestra.schemas.task import PrivateTaskData, PrivateTestCase


def _evaluator():
    repository = PrivateTestRepository()
    repository.add(
        PrivateTaskData(
            question_id="echo",
            public_tests=[PublicExample(input="1\n", output="1\n")],
            private_tests=[PrivateTestCase(input="9\n", output="9\n")],
        )
    )
    return FinalLCBEvaluator(
        repository_path="/root/projects/LiveCodeBench",
        private_repository=repository,
        evaluator_commit="test",
        timeout_seconds=2,
    )


def _state(frozen):
    return RuntimeState(
        run_id="r",
        task_id="echo",
        graph_id="g",
        graph_hash="h",
        contract_hash="c",
        node_status={"freeze": NodeStatus.SUCCEEDED},
        final_output_artifact_id="final" if frozen else None,
        frozen=frozen,
    )


@pytest.mark.asyncio
async def test_final_evaluator_rejects_unfrozen_state():
    with pytest.raises(RuntimeError, match="FINAL_OUTPUT_FROZEN"):
        await _evaluator().evaluate_frozen_run(
            runtime_state=_state(False),
            final_code="print(input())",
            lcb_problem_ref="echo",
        )


@pytest.mark.asyncio
async def test_final_evaluator_runs_only_after_freeze():
    result = await _evaluator().evaluate_frozen_run(
        runtime_state=_state(True),
        final_code="print(input())",
        lcb_problem_ref="echo",
    )
    assert result.passed
