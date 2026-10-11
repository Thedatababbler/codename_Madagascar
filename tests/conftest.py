import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestra.config import SandboxLimits
from orchestra.sandbox.lcb_official import OfficialLCBSandbox
from orchestra.schemas.artifacts import PublicExample
from orchestra.schemas.task import AgentVisibleLCBTask

# The repositories under tests/fixtures are inputs to integration tests, several
# of them deliberately broken so an agent has something to repair. Collecting
# them as our own tests made a bare `pytest` report failures that say nothing
# about this codebase.
collect_ignore_glob = ["fixtures/*"]

# Sandbox-only rule (2026-10-11): the sandbox cannot be turned off. The one exemption is this test process, for
# driver-side code that starts no agent (harness tests in tmp directories): orchestra.sandbox.policy honours
# ADAMAS_SANDBOX_TEST_UNCONFINED only inside pytest during a test. Agent launches ignore it; tests of the sandbox
# itself unset it.
os.environ.setdefault("ADAMAS_SANDBOX_TEST_UNCONFINED", "1")


@pytest.fixture(autouse=True)
def _no_writes_to_the_real_exemption_log(tmp_path, monkeypatch):
    """Tests never append to memory/transfer/leak_exemptions.yaml (they did until 2026-10-11)."""
    from orchestra.memory import validate

    monkeypatch.setattr(validate, "DEFAULT_EXEMPTION_LOG", tmp_path / "leak_exemptions.yaml")


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
