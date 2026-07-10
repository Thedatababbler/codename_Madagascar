import os
from datetime import UTC, datetime
from pathlib import Path

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
def lcb_repository_path():
    value = os.getenv("LCB_REPOSITORY_PATH")
    if not value:
        pytest.skip("LCB_REPOSITORY_PATH is required for official-checker integration tests")
    path = Path(value)
    if not (path / "lcb_runner").exists():
        pytest.fail(f"Invalid LCB_REPOSITORY_PATH: {path}")
    return str(path)


@pytest.fixture
def official_sandbox(lcb_repository_path):
    return OfficialLCBSandbox(
        repository_path=lcb_repository_path,
        limits=SandboxLimits(
            memory_mb=2048,
            max_processes=32,
            max_open_files=128,
            max_file_size_mb=16,
        ),
        num_process_evaluate=1,
        worker_grace_seconds=5,
        max_worker_wall_seconds=60,
    )
