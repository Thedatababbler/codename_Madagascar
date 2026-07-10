from datetime import UTC, datetime

import pytest

from orchestra.config import SandboxLimits
from orchestra.sandbox.lcb_official import OfficialLCBSandbox
from orchestra.schemas.artifacts import PublicExample
from orchestra.schemas.task import AgentVisibleLCBTask


@pytest.fixture
def visible_echo_task():
    return AgentVisibleLCBTask(
        question_id="echo",
        question_title="Echo",
        question_content="Read one integer and print it.",
        platform="synthetic",
        contest_date=datetime(2025, 1, 1, tzinfo=UTC),
        starter_code="",
        difficulty="easy",
        public_test_cases=[PublicExample(input="7\n", output="7\n")],
        metadata_public={},
    )


@pytest.fixture
def official_sandbox():
    return OfficialLCBSandbox(
        repository_path="/root/projects/LiveCodeBench",
        limits=SandboxLimits(
            memory_mb=2048,
            max_processes=32,
            max_open_files=128,
            max_file_size_mb=16,
        ),
        num_process_evaluate=1,
    )
