"""Shared authoritative harness command runner (trusted fixtures only)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from orchestra.harness.env_redaction import build_harness_env
from orchestra.harness.repository_test import TRUSTED_MARKER, workspace_is_trusted

logger = logging.getLogger(__name__)


async def run_authoritative_harness_command(
    *,
    cwd: str | Path,
    command: list[str],
    timeout_seconds: float = 60.0,
) -> tuple[bool, int, str, str]:
    """Run harness command with secret redaction + process-group kill.

    Returns (passed, exit_code, stdout, stderr).
    """
    path = Path(cwd)
    if not path.exists():
        return False, 127, "", f"workspace missing: {path}"
    if not workspace_is_trusted(path):
        return (
            False,
            126,
            "",
            (
                f"trusted fixture only; missing {TRUSTED_MARKER} under {path} "
                "(not a security boundary)"
            ),
        )
    env = build_harness_env()
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
        return False, 124, "", f"harness timed out after {timeout_seconds}s"
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    code = int(proc.returncode or 0)
    return code == 0, code, stdout[-4000:], stderr[-4000:]
