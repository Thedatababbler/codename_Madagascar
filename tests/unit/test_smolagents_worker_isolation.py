"""Worker isolation tests for smolagents backend."""

from __future__ import annotations

import time

import pytest

from orchestra.backends.workers.smolagents_worker import run_worker_process


def test_worker_timeout_is_reaped():
    started = time.monotonic()
    result = run_worker_process(
        {"task": "x", "__test_hang_seconds__": 30},
        wall_timeout_seconds=1.0,
    )
    elapsed = time.monotonic() - started
    assert result["status"] == "timeout"
    assert elapsed < 10
    assert result["backend_metadata"]["worker_wall_timeout"] is True


@pytest.mark.asyncio
async def test_worker_exception_does_not_kill_parent(tmp_path):
    from orchestra.backends.base import (
        AgentRequest,
        AgentRunStatus,
        BackendExecutionContext,
        ModelSpec,
        OutputContract,
    )
    from orchestra.backends.smolagents_code import SmolagentsCodeBackend

    def boom(_payload):
        raise RuntimeError("worker boom")

    backend = SmolagentsCodeBackend(worker_runner=boom)
    result = await backend.run(
        AgentRequest(
            request_id="r",
            task_id="t",
            node_id="n",
            role="r",
            instruction="i",
            rendered_context="c",
            model=ModelSpec(name="m"),
            tools=["final_answer"],
            max_steps=1,
            timeout_seconds=5,
            output_contract=OutputContract(
                parser_id="final_answer", output_schema="FinalAnswerArtifact"
            ),
            backend_config={"type": "smolagents_code", "max_steps": 1},
        ),
        BackendExecutionContext(
            run_id="r", task_id="t", node_id="n", trace_dir=str(tmp_path)
        ),
    )
    assert result.status is AgentRunStatus.INFRA_ERROR
    assert "worker boom" in (result.error.message if result.error else "")
