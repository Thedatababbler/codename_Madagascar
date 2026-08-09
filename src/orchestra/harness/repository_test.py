"""Repository test harness — runs pytest (or configured command) in the workspace.

Security boundary (M3.5 / M4):
  This harness executes the configured command as a **subprocess** with
  secret-env redaction and process-group kill on timeout. It is **only**
  approved for **trusted fixtures** that ship an
  ``.adamas_trusted_harness`` marker (see ``tests/fixtures/codex_tiny_repo``).

  It is **not** a security sandbox / low-privilege isolation worker
  (``trusted fixture only; not a security boundary``). Untrusted /
  attacker-controlled repositories must not be pointed at this harness until
  OfficialLCBSandbox, a low-privilege worker, or an equivalent container is
  wired for that path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from pathlib import Path

from orchestra.harness.env_redaction import build_harness_env
from orchestra.harness.progress import parse_progress
from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.ir.nodes import HarnessNodeSpec
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult
from orchestra.schemas.artifacts import (
    RepositoryChangeArtifact,
    RepositoryHarnessResultArtifact,
)

logger = logging.getLogger(__name__)

TRUSTED_MARKER = ".adamas_trusted_harness"
ALLOW_UNTRUSTED_ENV = "ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS"


def workspace_is_trusted(cwd: Path) -> bool:
    """Return True when the workspace opts into the trusted-fixture harness."""
    if (cwd / TRUSTED_MARKER).is_file():
        return True
    # Copies of fixtures keep the marker; also accept explicit escape hatch.
    return os.getenv(ALLOW_UNTRUSTED_ENV, "").strip() in {"1", "true", "yes"}


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
        if not workspace_is_trusted(cwd):
            return NodeExecutionResult.failed(
                node.node_id,
                PermissionError(
                    "repository_test_harness currently supports trusted fixtures only "
                    f"(missing {TRUSTED_MARKER} under {cwd}). "
                    "Do not point it at untrusted repos; a low-privilege worker is "
                    f"not implemented yet. Set {ALLOW_UNTRUSTED_ENV}=1 only for "
                    "explicit local overrides."
                ),
                int((time.perf_counter() - started) * 1000),
            )
        command = list(node.command or ["python", "-m", "pytest", "-q"])
        timeout = float(node.timeout_seconds or self.default_timeout_seconds)
        harness_env = build_harness_env()
        logger.info(
            "repository_test_harness: trusted fixture only; not a security boundary "
            "(cwd=%s, command=%s)",
            cwd,
            command,
        )

        async def _run() -> tuple[int, str, str]:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=harness_env,
                start_new_session=True,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
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
        score, stages, furthest = parse_progress(stdout)
        payload = RepositoryHarnessResultArtifact(
            passed=exit_code == 0,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_summary=stdout[-4000:],
            stderr_summary=stderr[-4000:],
            changed_files=list(change.changed_files),
            score=score,
            stages=stages,
            furthest_stage=furthest,
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
