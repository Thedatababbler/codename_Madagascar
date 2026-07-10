import asyncio

import pytest

from orchestra.adapters.livecodebench.final_evaluator import FinalLCBEvaluator
from orchestra.adapters.livecodebench.loader import PrivateTestRepository
from orchestra.config import SandboxLimits
from orchestra.runtime.state import NodeStatus, RuntimeState
from orchestra.sandbox.lcb_official import FinalLCBWorker
from orchestra.schemas.artifacts import PublicExample
from orchestra.schemas.task import PrivateTaskData, PrivateTestCase


@pytest.mark.asyncio
async def test_final_evaluator_does_not_leak_hidden_test_data(lcb_repository_path):
    repository = PrivateTestRepository()
    repository.add(
        PrivateTaskData(
            question_id="echo",
            public_tests=[PublicExample(input="1\n", output="1\n")],
            private_tests=[
                PrivateTestCase(
                    input="PRIVATE_MARKER_INPUT",
                    output="PRIVATE_MARKER_OUTPUT",
                )
            ],
        )
    )
    evaluator = FinalLCBEvaluator(
        private_repository=repository,
        evaluator_commit="test",
        worker=FinalLCBWorker(
            repository_path=lcb_repository_path,
            limits=SandboxLimits(),
            worker_grace_seconds=5,
            max_worker_wall_seconds=30,
        ),
        sandbox_semaphore=asyncio.Semaphore(1),
        per_test_timeout_seconds=2,
    )
    state = RuntimeState(
        run_id="r",
        task_id="echo",
        graph_id="g",
        graph_hash="h",
        contract_hash="c",
        node_status={"freeze": NodeStatus.SUCCEEDED},
        final_output_artifact_id="final",
        frozen=True,
    )
    result = await evaluator.evaluate_frozen_run(
        runtime_state=state,
        final_code="print(input())",
        lcb_problem_ref="echo",
    )
    encoded = result.model_dump_json()
    assert "PRIVATE_MARKER_INPUT" not in encoded
    assert "PRIVATE_MARKER_OUTPUT" not in encoded
    assert "private_test" not in encoded.lower()
