import asyncio
import os
import pwd
import resource
import signal
import sys
import tempfile
from pathlib import Path

from orchestra.config import SandboxLimits
from orchestra.sandbox.base import SandboxBackend
from orchestra.sandbox.lcb_worker import WorkerRequest
from orchestra.sandbox.result import SandboxExecutionResult
from orchestra.schemas.task import AgentVisibleLCBTask


class OfficialLCBSandboxUnavailable(RuntimeError):
    pass


class OfficialLCBSandbox(SandboxBackend):
    """Public-test backend using the pinned checker in an isolated worker process."""

    def __init__(
        self,
        *,
        repository_path: str,
        limits: SandboxLimits | None = None,
        num_process_evaluate: int = 1,
    ) -> None:
        self.repository_path = str(Path(repository_path).resolve())
        if not Path(self.repository_path, "lcb_runner").exists():
            raise OfficialLCBSandboxUnavailable(
                f"LiveCodeBench checkout unavailable: {self.repository_path}"
            )
        if num_process_evaluate != 1:
            raise ValueError("OfficialLCBSandbox only supports num_process_evaluate=1")
        self.limits = limits or SandboxLimits()
        self.num_process_evaluate = num_process_evaluate

    def build_request(
        self,
        *,
        task: AgentVisibleLCBTask,
        code: str,
        timeout_seconds: float,
    ) -> WorkerRequest:
        # AgentVisibleLCBTask has extra='forbid' and no private-test field.
        return WorkerRequest(
            task=task,
            code=code,
            timeout_seconds=timeout_seconds,
            repository_path=self.repository_path,
            num_process_evaluate=self.num_process_evaluate,
            limits=self.limits,
        )

    def _sanitized_environment(self) -> dict[str, str]:
        allowed = ("PATH", "LANG", "LC_ALL", "PYTHONIOENCODING")
        environment = {name: os.environ[name] for name in allowed if name in os.environ}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["OPENBLAS_NUM_THREADS"] = "1"
        environment["OMP_NUM_THREADS"] = "1"
        environment["MKL_NUM_THREADS"] = "1"
        environment["NUMEXPR_NUM_THREADS"] = "1"
        return environment

    def _preexec_limits(self) -> None:
        def set_limit(kind: int, requested: int) -> None:
            _soft, hard = resource.getrlimit(kind)
            effective = (
                requested if hard == resource.RLIM_INFINITY else min(requested, hard)
            )
            resource.setrlimit(kind, (effective, effective))

        set_limit(resource.RLIMIT_AS, self.limits.memory_mb * 1024 * 1024)
        set_limit(resource.RLIMIT_NPROC, self.limits.max_processes)
        set_limit(resource.RLIMIT_NOFILE, self.limits.max_open_files)
        set_limit(resource.RLIMIT_FSIZE, self.limits.max_file_size_mb * 1024 * 1024)

    async def evaluate_public(
        self,
        *,
        task: AgentVisibleLCBTask,
        code: str,
        timeout_seconds: float,
    ) -> SandboxExecutionResult:
        request = self.build_request(
            task=task, code=code, timeout_seconds=timeout_seconds
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
                    process.communicate(), timeout=timeout_seconds
                )
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                return SandboxExecutionResult(
                    compiled=True,
                    passed_count=0,
                    total_count=len(task.public_test_cases),
                    runtime_errors=0,
                    timeouts=1,
                    stderr_summary="Official LCB worker wall-clock timeout",
                    duration_ms=int(timeout_seconds * 1000),
                    worker_metadata={"worker_wall_timeout": True},
                )
            if process.returncode != 0 or not result_path.exists():
                raise OfficialLCBSandboxUnavailable(
                    "Official LCB worker failed closed: "
                    + stderr.decode(errors="replace")[-1000:]
                )
            return SandboxExecutionResult.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )
