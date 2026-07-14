"""Isolated smolagents CodeAgent worker process entrypoint."""

from __future__ import annotations

import json
import os
import signal
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _become_process_group_leader() -> None:
    try:
        os.setpgrp()
    except Exception:  # noqa: BLE001 - best effort on platforms without setpgrp
        pass


def _serialize_token_usage(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    }


def _normalize_steps(steps: list[Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    index = 0
    for step in steps:
        step_type = type(step).__name__
        timestamp = datetime.now(UTC).isoformat()
        if step_type == "PlanningStep":
            events.append(
                {
                    "event_type": "planning",
                    "index": index,
                    "timestamp": timestamp,
                    "summary": "planning_step",
                    "token_usage": _serialize_token_usage(
                        getattr(step, "token_usage", None)
                    ),
                    "metadata": {
                        "plan": str(getattr(step, "plan", "") or "")[:500],
                    },
                }
            )
            index += 1
            continue
        if step_type != "ActionStep":
            continue
        token_usage = _serialize_token_usage(getattr(step, "token_usage", None))
        error = getattr(step, "error", None)
        code_action = getattr(step, "code_action", None)
        observations = getattr(step, "observations", None)
        is_final = bool(getattr(step, "is_final_answer", False))
        events.append(
            {
                "event_type": "model_call",
                "index": index,
                "timestamp": timestamp,
                "summary": f"action_step_{getattr(step, 'step_number', index)}",
                "token_usage": token_usage,
                "metadata": {
                    "step_number": getattr(step, "step_number", None),
                    "model_output": str(getattr(step, "model_output", "") or "")[:500],
                },
            }
        )
        index += 1
        if code_action:
            events.append(
                {
                    "event_type": "action",
                    "index": index,
                    "timestamp": timestamp,
                    "summary": "python_action",
                    "token_usage": {},
                    "metadata": {"code_action": str(code_action)[:1000]},
                }
            )
            index += 1
        if error is not None:
            events.append(
                {
                    "event_type": "tool_error",
                    "index": index,
                    "timestamp": timestamp,
                    "summary": type(error).__name__,
                    "token_usage": {},
                    "metadata": {"error": str(error)[:1000]},
                }
            )
            index += 1
        elif observations is not None:
            events.append(
                {
                    "event_type": "observation",
                    "index": index,
                    "timestamp": timestamp,
                    "summary": "observation",
                    "token_usage": {},
                    "metadata": {"observations": str(observations)[:1000]},
                }
            )
            index += 1
        if is_final:
            events.append(
                {
                    "event_type": "final_answer",
                    "index": index,
                    "timestamp": timestamp,
                    "summary": "final_answer",
                    "token_usage": {},
                    "metadata": {
                        "output": str(getattr(step, "action_output", "") or "")[:500]
                    },
                }
            )
            index += 1
    return events


def run_codeagent(request: dict[str, Any]) -> dict[str, Any]:
    from smolagents import CodeAgent

    from orchestra.backends.base import ModelSpec
    from orchestra.backends.smolagents_model import SmolagentsModelFactory
    from orchestra.tools.base import ToolBuildContext
    from orchestra.tools.registry import get_tool_registry

    if request.get("managed_agents") not in (None,):
        return {
            "ok": False,
            "status": "backend_init_failure",
            "error": "managed_agents is forbidden",
            "trace_events": [],
            "usage": {},
            "final_output": None,
            "step_count": 0,
        }

    model_spec = ModelSpec.model_validate(request["model"])
    try:
        model = SmolagentsModelFactory().create(
            model_spec,
            fixture_responses=list(request.get("fixture_responses") or []),
        )
    except Exception as exc:  # noqa: BLE001
        from orchestra.backends.exception_mapping import map_exception_to_status

        status = map_exception_to_status(exc)
        return {
            "ok": False,
            "status": status.value,
            "error": f"{type(exc).__name__}: {exc}",
            "trace_events": [],
            "usage": {},
            "final_output": None,
            "step_count": 0,
        }

    tool_ids = list(request.get("tools") or [])
    try:
        tools = get_tool_registry().build(
            tool_ids,
            ToolBuildContext(
                run_id=request.get("run_id"),
                task_id=request.get("task_id"),
                node_id=request.get("node_id"),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": "invalid_request",
            "error": f"{type(exc).__name__}: {exc}",
            "trace_events": [],
            "usage": {},
            "final_output": None,
            "step_count": 0,
        }

    backend_config = request.get("backend_config") or {}
    max_steps = int(request.get("max_steps") or backend_config.get("max_steps") or 8)
    trace_buffer: list[dict[str, Any]] = []

    def trace_callback(memory_step, agent):  # noqa: ANN001
        del agent
        trace_buffer.extend(_normalize_steps([memory_step]))

    instructions = request.get("instruction") or None
    agent_kwargs = {
        "tools": tools,
        "model": model,
        "max_steps": max_steps,
        "planning_interval": backend_config.get("planning_interval"),
        "additional_authorized_imports": list(
            backend_config.get("additional_authorized_imports") or []
        ),
        "executor_type": backend_config.get("executor_type") or "local",
        "use_structured_outputs_internally": bool(
            backend_config.get("use_structured_outputs_internally", True)
        ),
        "return_full_result": True,
        "step_callbacks": [trace_callback],
        "instructions": instructions,
        "managed_agents": None,
    }
    try:
        agent = CodeAgent(**agent_kwargs)
    except TypeError:
        # Older/newer kwargs drift: retry without managed_agents / instructions.
        fallback = dict(agent_kwargs)
        fallback.pop("managed_agents", None)
        try:
            agent = CodeAgent(**fallback)
        except TypeError:
            fallback.pop("instructions", None)
            agent = CodeAgent(**fallback)
        if getattr(agent, "managed_agents", None):
            return {
                "ok": False,
                "status": "backend_init_failure",
                "error": "managed_agents must remain unset",
                "trace_events": [],
                "usage": {},
                "final_output": None,
                "step_count": 0,
            }

    task = request.get("task") or request.get("rendered_context") or ""
    try:
        result = agent.run(task, max_steps=max_steps, return_full_result=True)
    except Exception as exc:  # noqa: BLE001 - never crash parent runtime
        from orchestra.backends.exception_mapping import map_exception_to_status

        status = map_exception_to_status(exc)
        return {
            "ok": False,
            "status": status.value,
            "error": f"{type(exc).__name__}: {exc}",
            "trace_events": trace_buffer,
            "usage": {},
            "final_output": None,
            "step_count": len(getattr(agent, "memory", {}).steps)
            if hasattr(getattr(agent, "memory", None), "steps")
            else 0,
            "traceback": traceback.format_exc()[-2000:],
        }

    steps = list(getattr(result, "steps", []) or [])
    events = trace_buffer or _normalize_steps(steps)
    token_usage = _serialize_token_usage(getattr(result, "token_usage", None))
    if not token_usage:
        prompt = sum(e.get("token_usage", {}).get("prompt_tokens", 0) for e in events)
        completion = sum(
            e.get("token_usage", {}).get("completion_tokens", 0) for e in events
        )
        token_usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    state = getattr(result, "state", "success")
    output = getattr(result, "output", None)
    # Preserve full final output; OutputContract parsing happens in the backend.
    # Serialize structured final_answer payloads as JSON so json parsers work.
    if output is None:
        final_output = None
    elif isinstance(output, (dict, list)):
        final_output = json.dumps(output, ensure_ascii=False)
    else:
        final_output = str(output)
    status = "success"
    if state == "max_steps_error":
        status = "max_steps_exceeded"
    return {
        "ok": status == "success",
        "status": status,
        "error": None if status == "success" else f"CodeAgent finished with {status}",
        "final_output": final_output,
        "trace_events": events,
        "usage": token_usage,
        "step_count": len(steps),
        "backend_metadata": {
            "state": state,
            "max_steps": max_steps,
            "executor_type": backend_config.get("executor_type") or "local",
        },
    }


def worker_main(request_path: str, result_path: str) -> None:
    _become_process_group_leader()
    try:
        request = json.loads(Path(request_path).read_text(encoding="utf-8"))
        hang_seconds = request.get("__test_hang_seconds__")
        if hang_seconds is not None:
            import time

            time.sleep(float(hang_seconds))
        result = run_codeagent(request)
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "status": "infra_error",
            "error": f"{type(exc).__name__}: {exc}",
            "trace_events": [],
            "usage": {},
            "final_output": None,
            "step_count": 0,
            "traceback": traceback.format_exc()[-2000:],
        }
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")


def _spawn_target(request_path: str, result_path: str) -> None:
    worker_main(request_path, result_path)


def run_worker_process(
    request: dict[str, Any],
    *,
    wall_timeout_seconds: float,
) -> dict[str, Any]:
    """Spawn an isolated worker using multiprocessing start method 'spawn'."""
    import multiprocessing as mp
    import tempfile
    import time

    ctx = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="orchestra-smolagents-") as tmp:
        request_path = Path(tmp) / "request.json"
        result_path = Path(tmp) / "result.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        process = ctx.Process(
            target=_spawn_target,
            args=(str(request_path), str(result_path)),
            daemon=False,
        )
        process.start()
        deadline = time.monotonic() + wall_timeout_seconds
        while process.is_alive() and time.monotonic() < deadline:
            process.join(timeout=0.2)
        if process.is_alive():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                process.kill()
            process.join(timeout=5)
            return {
                "ok": False,
                "status": "timeout",
                "error": f"Worker wall timeout after {wall_timeout_seconds}s",
                "trace_events": [],
                "usage": {},
                "final_output": None,
                "step_count": 0,
                "backend_metadata": {"worker_wall_timeout": True},
            }
        if not result_path.exists():
            return {
                "ok": False,
                "status": "infra_error",
                "error": f"Worker exited without result (code={process.exitcode})",
                "trace_events": [],
                "usage": {},
                "final_output": None,
                "step_count": 0,
            }
        return json.loads(result_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m orchestra.backends.workers.smolagents_worker REQ OUT")
        return 2
    worker_main(args[0], args[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
