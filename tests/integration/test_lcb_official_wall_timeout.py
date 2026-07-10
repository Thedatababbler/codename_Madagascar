from datetime import UTC, datetime

import pytest

from orchestra.schemas.artifacts import PublicExample
from orchestra.schemas.task import AgentVisibleLCBTask


@pytest.fixture
def multi_test_echo_task():
    return AgentVisibleLCBTask(
        question_id="echo-multi",
        question_title="Echo Multi",
        question_content="Read one integer and print it.",
        platform="synthetic",
        contest_date=datetime(2025, 1, 1, tzinfo=UTC),
        starter_code="",
        difficulty="easy",
        public_test_cases=[
            PublicExample(input="1\n", output="1\n"),
            PublicExample(input="2\n", output="2\n"),
            PublicExample(input="3\n", output="3\n"),
        ],
        metadata_public={},
    )


@pytest.mark.asyncio
async def test_multi_test_worker_completes_before_wall_timeout(
    official_sandbox, multi_test_echo_task
):
    result = await official_sandbox.evaluate_public(
        task=multi_test_echo_task,
        code="print(input())",
        timeout_seconds=2,
    )
    assert result.compiled
    assert result.passed_count == 3
    assert result.total_count == 3
    assert not result.worker_metadata.get("worker_wall_timeout")
