import asyncio

import pytest

from orchestra.adapters.livecodebench.final_evaluator import (
    FinalEvaluationStatus,
    FinalLCBEvaluator,
)
from orchestra.adapters.livecodebench.loader import PrivateTestRepository
from orchestra.config import SandboxLimits
from orchestra.runtime.state import NodeStatus, RuntimeState
from orchestra.sandbox.lcb_official import FinalLCBWorker
from orchestra.schemas.artifacts import PublicExample
from orchestra.schemas.task import PrivateTaskData, PrivateTestCase


@pytest.mark.asyncio
async def test_worker_wall_timeout_is_reported_as_infra_error(lcb_repository_path):
    repository = PrivateTestRepository()
    repository.add(
        PrivateTaskData(
            question_id="echo",
            public_tests=[PublicExample(input="1\n", output="1\n")],
            private_tests=[PrivateTestCase(input="2\n", output="2\n")],
        )
    )
    evaluator = FinalLCBEvaluator(
        private_repository=repository,
        evaluator_commit="test",
        worker=FinalLCBWorker(
            repository_path=lcb_repository_path,
            limits=SandboxLimits(),
            worker_grace_seconds=0,
            max_worker_wall_seconds=0.2,
        ),
        sandbox_semaphore=asyncio.Semaphore(1),
        per_test_timeout_seconds=6,
        max_infra_retries=1,
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
        final_code="while True:\n    pass",
        lcb_problem_ref="echo",
    )
    assert result.status is FinalEvaluationStatus.INFRA_ERROR
    assert result.passed is False
    assert result.pass_at_1 == 0.0
    assert result.infra_retries == 1
