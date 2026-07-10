"""Offline fixture tests for smolagents CodeAgent vertical slice."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from orchestra.backends.base import (
    AgentRequest,
    AgentRunStatus,
    BackendExecutionContext,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.smolagents_code import SmolagentsCodeBackend
from orchestra.cli.validate_graph import build_compiler
from orchestra.ir.compiler import GraphCompilationError
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import AgentNodeSpec, SmolagentsCodeBackendConfig
from orchestra.llm.usage import LLMUsage
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.tasks.bbeh import BBEHEvaluator, BBEHTaskAdapter
from orchestra.tools.base import ToolBuildContext, ToolRegistry, UnknownToolError
from orchestra.tools.bbeh_tools import register_bbeh_tools
from orchestra.tools.registry import get_tool_registry, reset_tool_registry_for_tests


def _request(**overrides) -> AgentRequest:
    base = {
        "request_id": "r1",
        "task_id": "t1",
        "node_id": "solver",
        "role": "problem_solver",
        "instruction": "Solve.",
        "rendered_context": "What is 2+2?",
        "model": ModelSpec(name="mock-model"),
        "tools": ["python_math", "calculator", "final_answer"],
        "max_steps": 8,
        "timeout_seconds": 30.0,
        "output_contract": OutputContract(
            parser_id="final_answer",
            output_schema="FinalAnswerArtifact",
            type="final_answer",
            answer_format="single_line",
        ),
        "backend_config": {
            "type": "smolagents_code",
            "max_steps": 8,
            "executor_type": "local",
            "use_structured_outputs_internally": True,
            "additional_authorized_imports": [],
        },
    }
    base.update(overrides)
    return AgentRequest(**base)


def _context(tmp_path) -> BackendExecutionContext:
    return BackendExecutionContext(
        run_id="run",
        task_id="t1",
        node_id="solver",
        trace_dir=str(tmp_path / "traces"),
    )


def _ok_worker(final_output="4", status="success", **extra):
    def runner(_payload):
        return {
            "ok": status == "success",
            "status": status,
            "error": None if status == "success" else f"failed:{status}",
            "final_output": final_output,
            "trace_events": [
                {
                    "event_type": "model_call",
                    "index": 0,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "summary": "step0",
                    "token_usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "metadata": {},
                },
                {
                    "event_type": "final_answer",
                    "index": 1,
                    "summary": "final_answer",
                    "token_usage": {},
                    "metadata": {"output": final_output},
                },
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "step_count": 1,
            "backend_metadata": {"executor_type": "local", **extra},
        }

    return runner


@pytest.mark.asyncio
async def test_normal_one_step_answer(tmp_path):
    backend = SmolagentsCodeBackend(worker_runner=_ok_worker("42"))
    result = await backend.run(_request(), _context(tmp_path))
    assert result.status is AgentRunStatus.SUCCESS
    assert result.output_artifacts[0].payload["answer"] == "42"
    assert result.usage.prompt_tokens == 10
    assert result.trace_events[0].event_type == "model_call"


@pytest.mark.asyncio
async def test_python_action_error_then_recover(tmp_path):
    def runner(_payload):
        return {
            "ok": True,
            "status": "success",
            "error": None,
            "final_output": "7",
            "trace_events": [
                {"event_type": "action", "index": 0, "summary": "python_action"},
                {
                    "event_type": "tool_error",
                    "index": 1,
                    "summary": "NameError",
                    "metadata": {"error": "NameError: x"},
                },
                {"event_type": "action", "index": 2, "summary": "python_action"},
                {"event_type": "observation", "index": 3, "summary": "observation"},
                {"event_type": "final_answer", "index": 4, "summary": "final_answer"},
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 12},
            "step_count": 2,
            "backend_metadata": {},
        }

    result = await SmolagentsCodeBackend(worker_runner=runner).run(
        _request(), _context(tmp_path)
    )
    assert result.status is AgentRunStatus.SUCCESS
    assert any(e.event_type == "tool_error" for e in result.trace_events)
    assert result.output_artifacts[0].payload["answer"] == "7"


@pytest.mark.asyncio
async def test_max_steps_exceeded(tmp_path):
    result = await SmolagentsCodeBackend(
        worker_runner=_ok_worker(final_output=None, status="max_steps_exceeded")
    ).run(_request(max_steps=2), _context(tmp_path))
    assert result.status is AgentRunStatus.MAX_STEPS_EXCEEDED
    assert not result.output_artifacts


@pytest.mark.asyncio
async def test_tool_failure(tmp_path):
    result = await SmolagentsCodeBackend(
        worker_runner=_ok_worker(final_output=None, status="tool_failure")
    ).run(_request(), _context(tmp_path))
    assert result.status is AgentRunStatus.TOOL_FAILURE


@pytest.mark.asyncio
async def test_malformed_final_answer(tmp_path):
    result = await SmolagentsCodeBackend(
        worker_runner=_ok_worker(final_output="   \n")
    ).run(_request(), _context(tmp_path))
    assert result.status is AgentRunStatus.OUTPUT_CONTRACT_FAILURE


@pytest.mark.asyncio
async def test_worker_wall_timeout(tmp_path):
    result = await SmolagentsCodeBackend(
        worker_runner=_ok_worker(status="timeout", final_output=None)
    ).run(_request(), _context(tmp_path))
    assert result.status is AgentRunStatus.TIMEOUT


@pytest.mark.asyncio
async def test_model_initialization_failure(tmp_path):
    result = await SmolagentsCodeBackend(
        worker_runner=_ok_worker(status="backend_init_failure", final_output=None)
    ).run(_request(), _context(tmp_path))
    assert result.status is AgentRunStatus.BACKEND_INIT_FAILURE


def test_unknown_tool_rejected_at_compile_and_registry():
    registry = ToolRegistry()
    register_bbeh_tools(registry)
    with pytest.raises(UnknownToolError):
        registry.build(["web_search"], ToolBuildContext())
    graph = load_graph("configs/graphs/bbeh_single_codeagent.yaml")
    node = graph.nodes[0]
    assert isinstance(node, AgentNodeSpec)
    bad = graph.model_copy(
        update={
            "nodes": [
                node.model_copy(update={"tools": ["python_math", "web_search"]}),
            ]
        }
    )
    with pytest.raises(GraphCompilationError, match="Unknown tool"):
        build_compiler("configs/contracts").compile(bad)


def test_managed_agents_cannot_be_configured():
    with pytest.raises(ValidationError, match="managed_agents"):
        SmolagentsCodeBackendConfig.model_validate(
            {"type": "smolagents_code", "managed_agents": [{"name": "x"}]}
        )


@pytest.mark.asyncio
async def test_trace_and_usage_propagate(tmp_path):
    backend = SmolagentsCodeBackend(worker_runner=_ok_worker("9"))
    result = await backend.run(_request(), _context(tmp_path))
    assert isinstance(result.usage, LLMUsage)
    assert result.usage.total_tokens == 15
    assert [e.event_type for e in result.trace_events] == [
        "model_call",
        "final_answer",
    ]
    assert "run_context" not in BackendExecutionContext.model_fields


def test_bbeh_evaluator_and_adapter(tmp_path):
    path = tmp_path / "instances.jsonl"
    path.write_text(
        json.dumps(
            {
                "task_id": "bbeh_demo:0",
                "id": 0,
                "query": "What is 1+1?",
                "gt": "2",
                "subset": "demo",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = BBEHTaskAdapter(tmp_path)
    instance = adapter.load_instance("bbeh_demo:0")
    artifact = adapter.build_problem_artifact(instance)
    assert "gt" not in artifact.payload
    assert "2" not in json.dumps(artifact.payload)
    final = FinalAnswerArtifact(answer="2", source_node="solver")
    from orchestra.ir.artifacts import create_artifact

    final_art = create_artifact(final, producer_node_id="solver", task_id="bbeh_demo:0")
    evaluation = adapter.evaluate(
        instance=instance, final_artifact=final_art, execution_success=True
    )
    assert evaluation.execution_success is True
    assert evaluation.answer_correct is True
    assert BBEHEvaluator.evaluate_correctness("(A)", "A")


@pytest.mark.asyncio
async def test_backend_healthcheck_requires_smolagents():
    health = await SmolagentsCodeBackend().healthcheck()
    assert health.healthy is True
    assert health.backend_id == "smolagents_code"


def test_default_tool_registry_allowlist():
    reset_tool_registry_for_tests()
    registry = get_tool_registry()
    assert registry.known_ids() == {"python_math", "calculator", "final_answer"}
