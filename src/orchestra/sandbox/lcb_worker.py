"""Isolated worker entry point for the pinned LiveCodeBench checker."""

import ast
import json
import os
import pwd
import resource
import sys
import time
from pathlib import Path

from pydantic import BaseModel

from orchestra.config import SandboxLimits
from orchestra.sandbox.result import SandboxExecutionResult, VisibleTestResult
from orchestra.schemas.task import AgentVisibleLCBTask

SENSITIVE_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


class WorkerRequest(BaseModel):
    task: AgentVisibleLCBTask
    code: str
    timeout_seconds: float
    repository_path: str
    num_process_evaluate: int = 1
    limits: SandboxLimits


def redact_environment() -> list[str]:
    redacted = []
    for name in list(os.environ):
        if any(marker in name.upper() for marker in SENSITIVE_MARKERS):
            redacted.append(name)
            os.environ.pop(name, None)
    return sorted(redacted)


def _set_limit(kind: int, requested: int) -> None:
    _soft, hard = resource.getrlimit(kind)
    effective = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
    resource.setrlimit(kind, (effective, effective))


def apply_resource_limits(limits: SandboxLimits) -> dict[str, int]:
    _set_limit(resource.RLIMIT_AS, limits.memory_mb * 1024 * 1024)
    _set_limit(resource.RLIMIT_NPROC, limits.max_processes)
    _set_limit(resource.RLIMIT_NOFILE, limits.max_open_files)
    _set_limit(resource.RLIMIT_FSIZE, limits.max_file_size_mb * 1024 * 1024)
    return {
        "memory_bytes": resource.getrlimit(resource.RLIMIT_AS)[0],
        "max_processes": resource.getrlimit(resource.RLIMIT_NPROC)[0],
        "max_open_files": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
        "max_file_size_bytes": resource.getrlimit(resource.RLIMIT_FSIZE)[0],
    }


def drop_privileges() -> dict[str, int]:
    if os.geteuid() == 0:
        nobody = pwd.getpwnam("nobody")
        os.setgroups([])
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)
    return {"uid": os.geteuid(), "gid": os.getegid()}


def _syntax_failure(
    request: WorkerRequest, exc: SyntaxError, metadata: dict
) -> SandboxExecutionResult:
    return SandboxExecutionResult(
        compiled=False,
        passed_count=0,
        total_count=len(request.task.public_test_cases),
        runtime_errors=0,
        timeouts=0,
        stderr_summary=f"{exc.msg} at line {exc.lineno}",
        duration_ms=0,
        worker_metadata=metadata,
    )


def evaluate(request: WorkerRequest) -> SandboxExecutionResult:
    if request.num_process_evaluate != 1:
        raise ValueError("Official LCB worker requires num_process_evaluate=1")
    redacted = redact_environment()
    observed_limits = apply_resource_limits(request.limits)
    metadata = {
        "redacted_environment_names": redacted,
        "sensitive_env_present": [
            name
            for name in os.environ
            if any(marker in name.upper() for marker in SENSITIVE_MARKERS)
        ],
        "resource_limits": observed_limits,
        "num_process_evaluate": 1,
        "public_test_count": len(request.task.public_test_cases),
    }
    try:
        ast.parse(request.code)
    except SyntaxError as exc:
        return _syntax_failure(request, exc, metadata)

    repository = str(Path(request.repository_path).resolve())
    if repository not in sys.path:
        sys.path.insert(0, repository)
    from lcb_runner.evaluation.compute_code_generation_metrics import check_correctness

    metadata["worker_identity"] = drop_privileges()

    sample = {
        "input_output": json.dumps(
            {
                "inputs": [test.input for test in request.task.public_test_cases],
                "outputs": [test.output for test in request.task.public_test_cases],
                "fn_name": request.task.metadata_public.get("func_name"),
            }
        )
    }
    started = time.perf_counter()
    results, checker_metadata = check_correctness(
        sample,
        request.code,
        timeout=max(1, int(request.timeout_seconds)),
        debug=False,
    )
    elapsed = int((time.perf_counter() - started) * 1000)
    visible_results = []
    for index, value in enumerate(results):
        passed = value is True or value == 1
        visible_results.append(
            VisibleTestResult(
                index=index,
                passed=passed,
                timed_out=value in {-1, -3},
                runtime_error=(
                    str(checker_metadata.get("error") or checker_metadata.get("error_message"))
                    if value == -4
                    else None
                ),
                actual_output=(
                    str(checker_metadata.get("output"))
                    if not passed and checker_metadata.get("output") is not None
                    else None
                ),
            )
        )
    metadata["checker_error_code"] = checker_metadata.get("error_code")
    metadata["checker_error_message"] = checker_metadata.get("error_message")
    passed_count = sum(item.passed for item in visible_results)
    timeouts = sum(item.timed_out for item in visible_results)
    runtime_errors = sum(item.runtime_error is not None for item in visible_results)
    return SandboxExecutionResult(
        compiled=True,
        passed_count=passed_count,
        total_count=len(request.task.public_test_cases),
        runtime_errors=runtime_errors,
        timeouts=timeouts,
        stderr_summary=(
            str(checker_metadata.get("error") or checker_metadata.get("error_message"))
            if runtime_errors or timeouts
            else None
        ),
        per_test_visible_results=visible_results,
        duration_ms=elapsed,
        worker_metadata=metadata,
    )


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python -m orchestra.sandbox.lcb_worker REQUEST RESULT", file=sys.stderr)
        return 2
    request_path = Path(sys.argv[1]).resolve()
    result_path = Path(sys.argv[2]).resolve()
    os.chdir(request_path.parent)
    request = WorkerRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
    result = evaluate(request)
    result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
