"""Low-privilege worker entry point for pinned LiveCodeBench evaluation."""

import json
import os
import pwd
import resource
import sys
import time
from pathlib import Path

from orchestra.config import SandboxLimits
from orchestra.sandbox.lcb_protocol import (
    FinalEvaluationStatus,
    FinalWorkerResult,
    PrivateFinalWorkerRequest,
    PublicWorkerRequest,
    WorkerMode,
    WorkerTestCase,
)
from orchestra.sandbox.result import SandboxExecutionResult, VisibleTestResult

SENSITIVE_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
SENSITIVE_EXACT_NAMES = frozenset(
    {"API_KEY", "SECRET", "PASSWORD", "TOKEN", "ACCESS_TOKEN", "REFRESH_TOKEN"}
)


def is_sensitive_environment_name(name: str) -> bool:
    upper = name.upper()
    if upper in SENSITIVE_EXACT_NAMES:
        return True
    return any(upper.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def redact_environment() -> list[str]:
    redacted = []
    for name in list(os.environ):
        if is_sensitive_environment_name(name):
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


def _security_setup(request) -> tuple[dict, object]:
    if request.num_process_evaluate != 1:
        raise ValueError("Official LCB worker requires num_process_evaluate=1")
    redacted = redact_environment()
    observed_limits = apply_resource_limits(request.limits)
    repository = str(Path(request.repository_path).resolve())
    if repository not in sys.path:
        sys.path.insert(0, repository)
    from lcb_runner.evaluation.compute_code_generation_metrics import check_correctness

    metadata = {
        "redacted_environment_names": redacted,
        "sensitive_env_present": [
            name for name in os.environ if is_sensitive_environment_name(name)
        ],
        "resource_limits": observed_limits,
        "num_process_evaluate": 1,
        "worker_identity": drop_privileges(),
    }
    return metadata, check_correctness


def _compile_error(code: str) -> str | None:
    try:
        compile(code, "<generated>", "exec")
    except SyntaxError as exc:
        return f"{exc.msg} at line {exc.lineno}"
    return None


def _checker_sample(
    tests: list[WorkerTestCase], function_name: str | None
) -> dict[str, str]:
    return {
        "input_output": json.dumps(
            {
                "inputs": [test.input for test in tests],
                "outputs": [test.output for test in tests],
                "fn_name": function_name,
            }
        )
    }


EXECUTION_DIR_NAME = "execution"


def _prepare_execution_workspace(request_path: Path) -> tuple[dict, Path]:
    workspace_root = request_path.parent
    raw = json.loads(request_path.read_text(encoding="utf-8"))
    request_path.unlink()
    execution_dir = workspace_root / EXECUTION_DIR_NAME
    execution_dir.mkdir()
    os.chdir(execution_dir)
    return raw, execution_dir


def evaluate_public(request: PublicWorkerRequest) -> SandboxExecutionResult:
    metadata, check_correctness = _security_setup(request)
    metadata["public_test_count"] = len(request.task.public_test_cases)
    compile_error = _compile_error(request.code)
    if compile_error:
        return SandboxExecutionResult(
            compiled=False,
            passed_count=0,
            total_count=len(request.task.public_test_cases),
            runtime_errors=0,
            timeouts=0,
            stderr_summary=compile_error,
            duration_ms=0,
            worker_metadata=metadata,
        )
    tests = [
        WorkerTestCase.model_validate(test.model_dump())
        for test in request.task.public_test_cases
    ]
    started = time.perf_counter()
    results, checker_metadata = check_correctness(
        _checker_sample(tests, request.task.function_name),
        request.code,
        timeout=max(1, int(request.per_test_timeout_seconds)),
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
    return SandboxExecutionResult(
        compiled=True,
        passed_count=sum(item.passed for item in visible_results),
        total_count=len(tests),
        runtime_errors=sum(item.runtime_error is not None for item in visible_results),
        timeouts=sum(item.timed_out for item in visible_results),
        stderr_summary=(
            str(checker_metadata.get("error") or checker_metadata.get("error_message"))
            if any(item.runtime_error or item.timed_out for item in visible_results)
            else None
        ),
        per_test_visible_results=visible_results,
        duration_ms=elapsed,
        worker_metadata=metadata,
    )


def _private_final_status(results: list) -> FinalEvaluationStatus:
    if results and all(value is True or value == 1 for value in results):
        return FinalEvaluationStatus.PASSED
    if any(value in {-1, -3} for value in results):
        return FinalEvaluationStatus.CODE_TIMEOUT
    return FinalEvaluationStatus.WRONG_ANSWER


def evaluate_private_final(request: PrivateFinalWorkerRequest) -> FinalWorkerResult:
    metadata, check_correctness = _security_setup(request)
    if _compile_error(request.code):
        return FinalWorkerResult(
            passed=False,
            pass_at_1=0.0,
            status=FinalEvaluationStatus.WRONG_ANSWER,
            worker_metadata=metadata,
        )
    results, _checker_metadata = check_correctness(
        _checker_sample(request.test_cases, request.function_name),
        request.code,
        timeout=max(1, int(request.per_test_timeout_seconds)),
        debug=False,
    )
    status = _private_final_status(results)
    passed = status is FinalEvaluationStatus.PASSED
    return FinalWorkerResult(
        passed=passed,
        pass_at_1=float(passed),
        status=status,
        worker_metadata=metadata,
    )


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: python -m orchestra.sandbox.lcb_worker REQUEST RESULT", file=sys.stderr)
        return 2
    request_path = Path(sys.argv[1]).resolve()
    result_path = Path(sys.argv[2]).resolve()
    raw, _execution_dir = _prepare_execution_workspace(request_path)
    mode = WorkerMode(raw["mode"])
    if mode is WorkerMode.PUBLIC:
        result = evaluate_public(PublicWorkerRequest.model_validate(raw))
    else:
        result = evaluate_private_final(PrivateFinalWorkerRequest.model_validate(raw))
    result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
