"""Deterministic offline CodeAgent integration (real worker, no mocked results)."""

from __future__ import annotations

import json

import pytest

from orchestra.backends.base import (
    AgentRequest,
    AgentRunStatus,
    BackendExecutionContext,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.smolagents_code import SmolagentsCodeBackend
from orchestra.backends.workers.smolagents_worker import run_codeagent


def _fixture_response(answer: str = "4") -> str:
    return json.dumps({"thought": "compute with tools", "code": f"final_answer({answer})"})


def _request(**overrides) -> AgentRequest:
    base = {
        "request_id": "offline-1",
        "task_id": "offline_task",
        "node_id": "solver",
        "role": "problem_solver",
        "instruction": "Solve the arithmetic question.",
        "rendered_context": "What is 2+2? Use final_answer.",
        "model": ModelSpec(provider="fixture", name="scripted-offline"),
        "tools": ["final_answer"],
        "max_steps": 3,
        "timeout_seconds": 60.0,
        "output_contract": OutputContract(
            parser_id="final_answer",
            output_schema="FinalAnswerArtifact",
            type="final_answer",
            answer_format="single_line",
        ),
        "backend_config": {
            "type": "smolagents_code",
            "max_steps": 3,
            "executor_type": "local",
            "use_structured_outputs_internally": True,
            "additional_authorized_imports": [],
            "fixture_responses": [_fixture_response("4")],
        },
    }
    base.update(overrides)
    return AgentRequest(**base)


@pytest.mark.asyncio
async def test_real_codeagent_worker_with_scripted_model(tmp_path):
    """Spawn the real worker process; do not mock the worker result payload."""
    backend = SmolagentsCodeBackend()  # default runner = spawn worker
    result = await backend.run(
        _request(),
        BackendExecutionContext(
            run_id="offline-run",
            task_id="offline_task",
            node_id="solver",
            trace_dir=str(tmp_path / "traces"),
        ),
    )
    assert result.status is AgentRunStatus.SUCCESS
    assert result.final_output == "4"
    assert result.output_artifacts[0].payload["answer"] == "4"
    assert result.output_artifacts[0].payload["raw_output"] == "4"
    assert result.step_count >= 1
    assert any(event.event_type == "final_answer" for event in result.trace_events)


def test_run_codeagent_maps_generation_failure():
    raw = run_codeagent(
        {
            "task": "fail",
            "model": {"provider": "fixture", "name": "scripted"},
            "tools": ["final_answer"],
            "max_steps": 2,
            "backend_config": {
                "type": "smolagents_code",
                "max_steps": 2,
                "executor_type": "local",
                "use_structured_outputs_internally": True,
                "additional_authorized_imports": [],
            },
            "fixture_responses": ["__RAISE_GENERATION__"],
        }
    )
    assert raw["status"] == "model_failure"
    assert "AgentGenerationError" in (raw["error"] or "")


def test_run_codeagent_maps_parsing_failure():
    raw = run_codeagent(
        {
            "task": "fail",
            "model": {"provider": "fixture", "name": "scripted"},
            "tools": ["final_answer"],
            "max_steps": 2,
            "backend_config": {
                "type": "smolagents_code",
                "max_steps": 2,
                "executor_type": "local",
                "use_structured_outputs_internally": True,
                "additional_authorized_imports": [],
            },
            "fixture_responses": ["__RAISE_PARSING__"],
        }
    )
    assert raw["status"] == "action_parse_failure"


def test_run_codeagent_maps_tool_failure():
    raw = run_codeagent(
        {
            "task": "fail",
            "model": {"provider": "fixture", "name": "scripted"},
            "tools": ["final_answer"],
            "max_steps": 2,
            "backend_config": {
                "type": "smolagents_code",
                "max_steps": 2,
                "executor_type": "local",
                "use_structured_outputs_internally": True,
                "additional_authorized_imports": [],
            },
            "fixture_responses": ["__RAISE_TOOL__"],
        }
    )
    assert raw["status"] == "tool_failure"
