import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from orchestra.sandbox.base import CodeSandbox
from orchestra.sandbox.result import SandboxExecutionResult, VisibleTestResult
from orchestra.schemas.artifacts import ProblemArtifact


class DockerUnavailableError(RuntimeError):
    pass


class DockerSandbox(CodeSandbox):
    def __init__(
        self,
        *,
        image: str = "python:3.11-slim",
        timeout_seconds: int = 10,
        memory_mb: int = 512,
    ) -> None:
        if shutil.which("docker") is None:
            raise DockerUnavailableError("docker executable is unavailable")
        self.image = image
        self.timeout_seconds = timeout_seconds
        self.memory_mb = memory_mb

    def _command(self, workdir: str, *command: str) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--cpus",
            "1",
            "--memory",
            f"{self.memory_mb}m",
            "--pids-limit",
            "64",
            "--env",
            "PYTHONIOENCODING=utf-8",
            "--mount",
            f"type=bind,src={workdir},dst=/work,readonly",
            self.image,
            *command,
        ]

    async def _run(self, workdir: str, stdin: str, *command: str):
        process = await asyncio.create_subprocess_exec(
            *self._command(workdir, *command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode()), timeout=self.timeout_seconds
            )
            return process.returncode, stdout.decode(), stderr.decode(), False
        except TimeoutError:
            process.kill()
            await process.wait()
            return None, "", "timeout", True

    async def evaluate_public(
        self, problem: ProblemArtifact, code: str
    ) -> SandboxExecutionResult:
        if any(item.testtype == "functional" for item in problem.public_examples):
            raise NotImplementedError(
                "Functional-style Docker execution requires the pinned official checker image"
            )
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="orchestra-docker-") as tmp:
            Path(tmp, "solution.py").write_text(code, encoding="utf-8")
            rc, _out, err, timed_out = await self._run(
                tmp, "", "python", "-m", "py_compile", "/work/solution.py"
            )
            if rc != 0 or timed_out:
                return SandboxExecutionResult(
                    compiled=False,
                    passed_count=0,
                    total_count=len(problem.public_examples),
                    runtime_errors=1,
                    timeouts=int(timed_out),
                    stderr_summary=err[-1000:],
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            results = []
            for index, example in enumerate(problem.public_examples):
                rc, stdout, stderr, timed_out = await self._run(
                    tmp, example.input, "python", "/work/solution.py"
                )
                passed = rc == 0 and stdout.strip() == example.output.strip()
                results.append(
                    VisibleTestResult(
                        index=index,
                        passed=passed,
                        timed_out=timed_out,
                        runtime_error=stderr[-1000:] if rc not in (0, None) else None,
                        actual_output=stdout[-2000:],
                    )
                )
            return SandboxExecutionResult(
                compiled=True,
                passed_count=sum(item.passed for item in results),
                total_count=len(results),
                runtime_errors=sum(item.runtime_error is not None for item in results),
                timeouts=sum(item.timed_out for item in results),
                stderr_summary=None,
                per_test_visible_results=results,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
