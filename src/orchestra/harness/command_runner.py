"""Shared authoritative harness command runner (trusted fixtures only)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestra.harness import results_file

from orchestra.harness.env_redaction import build_harness_env
from orchestra.harness.repository_test import TRUSTED_MARKER, workspace_is_trusted

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HarnessRun:
    """One gate run. ``payload`` is the check script's structured result (its results file), None when it wrote none;
    ``stdout``/``stderr`` are tails for messages only and are never parsed for a result."""

    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    payload: dict[str, Any] | None


async def run_harness(*, cwd: str | Path, command: list[str], timeout_seconds: float = 60.0) -> HarnessRun:
    """Run a harness command (secret redaction, process-group kill, sandbox) and read its structured result."""
    path = Path(cwd)
    if not path.exists():
        return HarnessRun(False, 127, "", f"workspace missing: {path}", None)
    if not workspace_is_trusted(path):
        return HarnessRun(
            False,
            126,
            "",
            (
                f"trusted fixture only; missing {TRUSTED_MARKER} under {path} "
                "(not a security boundary)"
            ),
            None,
        )
    from orchestra.sandbox import gate_tmp

    async with gate_tmp.session_for_command(list(command)) as tmp_grant:
        return await _run_harness(path, list(command), timeout_seconds, tmp_grant)


async def _run_harness(path: Path, command: list[str], timeout_seconds: float, tmp_grant: list[str]) -> HarnessRun:
    env = build_harness_env()
    command, holder = results_file.attach(list(command))
    command, env, private = _confine(path, list(command), env,
                                     extra_write=[*([str(holder)] if holder else []), *tmp_grant] or None)
    logger.info(
        "authoritative harness: trusted fixture only; not a security boundary "
        "(cwd=%s command=%s)",
        path,
        command,
    )
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.communicate()
        results_file.read(holder)
        return HarnessRun(False, 124, "", f"harness timed out after {timeout_seconds}s", None)
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    code = int(proc.returncode or 0)
    if private is not None:
        import shutil

        shutil.rmtree(private, ignore_errors=True)
    return HarnessRun(code == 0, code, stdout[-4000:], stderr[-4000:], results_file.read(holder))


async def run_authoritative_harness_command(
    *,
    cwd: str | Path,
    command: list[str],
    timeout_seconds: float = 60.0,
) -> tuple[bool, int, str, str]:
    """Run harness command with secret redaction + process-group kill.

    Returns (passed, exit_code, stdout tail, stderr tail). For the gate's result use ``run_harness`` and its
    ``payload``: the tails are for messages and are cut at 4000 characters.
    """
    run = await run_harness(cwd=cwd, command=command, timeout_seconds=timeout_seconds)
    return run.passed, run.exit_code, run.stdout, run.stderr


def _confine(cwd: Path, command: list[str], env: dict[str, str],
             extra_write: list[str] | None = None) -> tuple[list[str], dict[str, str], Path | None]:
    """With ``ADAMAS_SANDBOX_ENFORCED`` on, the gate runs confined (sandbox spec A): it reads the
    workspace, the harness directory named on its command line (gate suites, testkit) and the venv."""
    from orchestra.sandbox.policy import confine_command

    return confine_command(command, cwd=cwd, env=env, extra_write=extra_write)
