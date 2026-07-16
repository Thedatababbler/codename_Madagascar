import asyncio
import os
import pwd
import resource
import signal
import sys
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from orchestra.config import SandboxLimits
from orchestra.sandbox.base import SandboxBackend
from orchestra.sandbox.lcb_protocol import (
    FinalEvaluationStatus,
    FinalWorkerResult,
    PrivateFinalWorkerRequest,
    PublicWorkerRequest,
    WorkerTestCase,
)
from orchestra.sandbox.result import SandboxExecutionResult
from orchestra.schemas.task import AgentVisibleLCBTask, PrivateTaskData

T = TypeVar("T", bound=BaseModel)


class OfficialLCBSandboxUnavailable(RuntimeError):
    pass


def compute_worker_wall_timeout(
    *,
    per_test_seconds: float,
    test_count: int,
    worker_grace_seconds: float,
    max_worker_wall_seconds: float,
) -> float:
    calculated = (per_test_seconds + 1) * max(1, test_count)
    calculated += worker_grace_seconds
    return min(max_worker_wall_seconds, calculated)


class OfficialLCBProcessRunner:
    """Launches one sanitized, bounded, low-privilege official-checker worker."""

    def __init__(
        self,
        *,
        repository_path: str,
        limits: SandboxLimits,
        worker_grace_seconds: float,
        max_worker_wall_seconds: float,
    ) -> None:
        self.repository_path = str(Path(repository_path).resolve())
        if not Path(self.repository_path, "lcb_runner").exists():
            raise OfficialLCBSandboxUnavailable(
                f"LiveCodeBench checkout unavailable: {self.repository_path}"
            )
        self.limits = limits
        self.worker_grace_seconds = worker_grace_seconds
        self.max_worker_wall_seconds = max_worker_wall_seconds

    def wall_timeout(self, *, per_test_seconds: float, test_count: int) -> float:
        return compute_worker_wall_timeout(
            per_test_seconds=per_test_seconds,
            test_count=test_count,
            worker_grace_seconds=self.worker_grace_seconds,
            max_worker_wall_seconds=self.max_worker_wall_seconds,
        )

    def _sanitized_environment(self) -> dict[str, str]:
        allowed = ("PATH", "LANG", "LC_ALL", "PYTHONIOENCODING")
        environment = {name: os.environ[name] for name in allowed if name in os.environ}
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            }
        )
        return environment

    def _preexec_limits(self) -> None:
        from orchestra.sandbox.lcb_worker import effective_nproc_limit

        def set_limit(kind: int, requested: int) -> None:
            _soft, hard = resource.getrlimit(kind)
            effective = (
                requested if hard == resource.RLIM_INFINITY else min(requested, hard)
            )
            resource.setrlimit(kind, (effective, effective))

        # Child still has the parent UID here; worker may later drop to nobody.
        will_drop = os.geteuid() == 0
        set_limit(resource.RLIMIT_AS, self.limits.memory_mb * 1024 * 1024)
        set_limit(
            resource.RLIMIT_NPROC,
            effective_nproc_limit(
                self.limits.max_processes, will_drop_privileges=will_drop
            ),
        )
        set_limit(resource.RLIMIT_NOFILE, self.limits.max_open_files)
        set_limit(resource.RLIMIT_FSIZE, self.limits.max_file_size_mb * 1024 * 1024)

    async def run(
        self,
        *,
        request: BaseModel,
        result_type: type[T],
        test_count: int,
        per_test_timeout_seconds: float,
    ) -> T:
        wall_timeout = self.wall_timeout(
            per_test_seconds=per_test_timeout_seconds,
            test_count=test_count,
        )
        with tempfile.TemporaryDirectory(prefix="orchestra-lcb-worker-") as tmp:
            request_path = Path(tmp, "request.json")
            result_path = Path(tmp, "result.json")
            request_path.write_text(request.model_dump_json(), encoding="utf-8")
            if os.geteuid() == 0:
                nobody = pwd.getpwnam("nobody")
                os.chown(tmp, nobody.pw_uid, nobody.pw_gid)
                os.chmod(tmp, 0o700)
                os.chown(request_path, nobody.pw_uid, nobody.pw_gid)
                os.chmod(request_path, 0o400)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "orchestra.sandbox.lcb_worker",
                str(request_path),
                str(result_path),
                cwd=tmp,
                env=self._sanitized_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                preexec_fn=self._preexec_limits,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=wall_timeout
                )
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                metadata = {
                    "worker_wall_timeout": True,
                    "worker_wall_timeout_seconds": wall_timeout,
                }
                if result_type is SandboxExecutionResult:
                    return result_type.model_validate(
                        {
                            "compiled": True,
                            "passed_count": 0,
                            "total_count": test_count,
                            "runtime_errors": 0,
                            "timeouts": 1,
                            "stderr_summary": "Official LCB worker wall-clock timeout",
                            "duration_ms": int(wall_timeout * 1000),
                            "worker_metadata": metadata,
                        }
                    )
                return result_type.model_validate(
                    {
                        "passed": False,
                        "pass_at_1": 0.0,
                        "status": FinalEvaluationStatus.INFRA_ERROR,
                        "worker_metadata": metadata,
                    }
                )
            if process.returncode != 0 or not result_path.exists():
                raise OfficialLCBSandboxUnavailable(
                    "Official LCB worker failed closed: "
                    + stderr.decode(errors="replace")[-1000:]
                )
            return result_type.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )


class OfficialLCBSandbox(SandboxBackend):
    """Public-test backend. Its request type cannot represent private tests."""

    def __init__(
        self,
        *,
        repository_path: str,
        limits: SandboxLimits | None = None,
        num_process_evaluate: int = 1,
        worker_grace_seconds: float = 5,
        max_worker_wall_seconds: float = 60,
    ) -> None:
        if num_process_evaluate != 1:
            raise ValueError("OfficialLCBSandbox only supports num_process_evaluate=1")
        self.limits = limits or SandboxLimits()
        self.num_process_evaluate = num_process_evaluate
        self.runner = OfficialLCBProcessRunner(
            repository_path=repository_path,
            limits=self.limits,
            worker_grace_seconds=worker_grace_seconds,
            max_worker_wall_seconds=max_worker_wall_seconds,
        )

    def build_request(
        self,
        *,
        task: AgentVisibleLCBTask,
        code: str,
        timeout_seconds: float,
    ) -> PublicWorkerRequest:
        return PublicWorkerRequest(
            task=task,
            code=code,
            per_test_timeout_seconds=timeout_seconds,
            repository_path=self.runner.repository_path,
            num_process_evaluate=self.num_process_evaluate,
            limits=self.limits,
        )

    async def evaluate_public(
        self,
        *,
        task: AgentVisibleLCBTask,
        code: str,
        timeout_seconds: float,
    ) -> SandboxExecutionResult:
        return await self.runner.run(
            request=self.build_request(
                task=task, code=code, timeout_seconds=timeout_seconds
            ),
            result_type=SandboxExecutionResult,
            test_count=len(task.public_test_cases),
            per_test_timeout_seconds=timeout_seconds,
        )


class FinalLCBWorker:
    """Private-final backend returning pass@1 only; no hidden failure details."""

    def __init__(
        self,
        *,
        repository_path: str,
        limits: SandboxLimits | None = None,
        num_process_evaluate: int = 1,
        worker_grace_seconds: float = 5,
        max_worker_wall_seconds: float = 60,
    ) -> None:
        if num_process_evaluate != 1:
            raise ValueError("FinalLCBWorker only supports num_process_evaluate=1")
        self.limits = limits or SandboxLimits()
        self.num_process_evaluate = num_process_evaluate
        self.runner = OfficialLCBProcessRunner(
            repository_path=repository_path,
            limits=self.limits,
            worker_grace_seconds=worker_grace_seconds,
            max_worker_wall_seconds=max_worker_wall_seconds,
        )

    async def evaluate(
        self,
        *,
        question_id: str,
        hidden: PrivateTaskData,
        code: str,
        per_test_timeout_seconds: float,
    ) -> FinalWorkerResult:
        tests = [
            WorkerTestCase.model_validate(test.model_dump())
            for test in [*hidden.public_tests, *hidden.private_tests]
        ]
        request = PrivateFinalWorkerRequest(
            question_id=question_id,
            test_cases=tests,
            function_name=hidden.function_name,
            code=code,
            per_test_timeout_seconds=per_test_timeout_seconds,
            repository_path=self.runner.repository_path,
            num_process_evaluate=self.num_process_evaluate,
            limits=self.limits,
        )
        return await self.runner.run(
            request=request,
            result_type=FinalWorkerResult,
            test_count=len(tests),
            per_test_timeout_seconds=per_test_timeout_seconds,
        )
