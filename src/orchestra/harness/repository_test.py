"""Repository test harness — runs pytest (or configured command) in the workspace."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.ir.nodes import HarnessNodeSpec
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult
from orchestra.schemas.artifacts import (
    RepositoryChangeArtifact,
    RepositoryHarnessResultArtifact,
)


class RepositoryTestHarnessExecutor:
    """Independent AdaMAS harness; does not trust Codex self-reported test results."""

    def __init__(self, *, default_timeout_seconds: float = 60.0) -> None:
        self.default_timeout_seconds = default_timeout_seconds

    async def execute(
        self,
        node: HarnessNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        change_key = next(
            (
                key
                for key, art in inputs.items()
                if art.artifact_type == "RepositoryChangeArtifact"
            ),
            None,
        )
        if change_key is None:
            return NodeExecutionResult.failed(
                node.node_id,
                ValueError("repository_test_harness requires RepositoryChangeArtifact"),
                int((time.perf_counter() - started) * 1000),
            )
        change = RepositoryChangeArtifact.model_validate(inputs[change_key].payload)
        workspace = context.workspace_ref or change.workspace_ref
        if not workspace:
            return NodeExecutionResult.failed(
                node.node_id,
                ValueError("repository_test_harness requires workspace_ref"),
                int((time.perf_counter() - started) * 1000),
            )
        cwd = Path(workspace)
        if not cwd.exists():
            return NodeExecutionResult.failed(
                node.node_id,
                FileNotFoundError(f"workspace missing: {cwd}"),
                int((time.perf_counter() - started) * 1000),
            )
        command = list(node.command or ["python", "-m", "pytest", "-q"])
        timeout = float(node.timeout_seconds or self.default_timeout_seconds)

        async def _run() -> tuple[int, str, str]:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return 124, "", f"harness timed out after {timeout}s"
            return (
                int(proc.returncode or 0),
                stdout_b.decode("utf-8", errors="replace"),
                stderr_b.decode("utf-8", errors="replace"),
            )

        async with context.semaphores.sandbox:
            exit_code, stdout, stderr = await _run()
        duration_ms = int((time.perf_counter() - started) * 1000)
        payload = RepositoryHarnessResultArtifact(
            passed=exit_code == 0,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_summary=stdout[-4000:],
            stderr_summary=stderr[-4000:],
            changed_files=list(change.changed_files),
        )
        output_slot = next(iter(node.output_slots))
        artifact = create_artifact(
            payload,
            producer_node_id=node.node_id,
            task_id=context.task_id,
            parent_artifact_ids=[inputs[change_key].artifact_id],
        )
        return NodeExecutionResult(
            node_id=node.node_id,
            succeeded=True,
            outputs={output_slot: artifact},
            latency_ms=duration_ms,
        )
