"""Sandbox-only rule (user decision 2026-10-11): nothing that starts an agent or runs workspace code may run unconfined.

Three layers, each fail-closed:

1. ``require_sandbox(role)`` before any agent start: the configuration says
   enforced, Landlock ABI >= 4 is available (filesystem and TCP rules), and the
   network guard library builds.
2. ``assert_confined_launch(cmd, env)`` on the exact command about to start: it
   begins with the Landlock launcher, its policy restricts the network (and the
   filesystem, except for sealed held-out scoring), and ``LD_PRELOAD`` carries
   the network guard.
3. The launcher itself (``orchestra.sandbox.exec``) verifies after applying the
   rules and before ``exec`` that a forbidden path really reads as EACCES and
   that the guard is preloaded, and exits 126 otherwise.

``run_confined`` is the one way driver-side code and scripts run agent-written
code (gates, authored suites, held-out scoring, baseline checks).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from orchestra.sandbox import policy as SB

MIN_ABI = 4
_READY: dict[str, Any] = {}


SandboxRequiredError = SB.SandboxRequiredError


def _config_enforced() -> bool:
    return bool((SB.load_config().get("sandbox") or {}).get("enforced"))


def require_sandbox(role: str) -> None:
    """Refuse to start ``role`` unless the sandbox can be in force. Cached per process once it passes."""
    if _READY.get("ok"):
        return
    if not _config_enforced():
        raise SandboxRequiredError(f"{role}: configs/sandbox.yaml must say sandbox.enforced: true; refusing to start")
    from orchestra.sandbox.landlock import abi_version

    try:
        abi = int(abi_version())
    except Exception as exc:  # noqa: BLE001
        raise SandboxRequiredError(f"{role}: Landlock unavailable ({exc}); refusing to start") from exc
    if abi < MIN_ABI:
        raise SandboxRequiredError(f"{role}: Landlock ABI {abi} < {MIN_ABI} (no TCP rules); refusing to start")
    try:
        lib = SB.ensure_netguard()
    except Exception as exc:  # noqa: BLE001
        raise SandboxRequiredError(f"{role}: network guard unavailable ({exc}); refusing to start") from exc
    if not Path(lib).is_file():
        raise SandboxRequiredError(f"{role}: network guard {lib} missing; refusing to start")
    _READY.update(ok=True, abi=abi, netguard=str(lib))


def _policy_of(cmd: list[str]) -> dict[str, Any] | None:
    prefix = SB.launcher(Path("X"))
    if len(cmd) < len(prefix) or cmd[: len(prefix) - 2] != prefix[:-2] or cmd[len(prefix) - 1] != "--":
        return None
    try:
        return json.loads(Path(cmd[len(prefix) - 2]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def assert_confined_launch(cmd: list[str], env: dict[str, str] | None, *, role: str, allow_open_fs: bool = False) -> dict[str, Any]:
    """The command starts with the Landlock launcher under a restrictive policy and the guard is preloaded."""
    require_sandbox(role)
    pol = _policy_of(list(cmd))
    if pol is None:
        raise SandboxRequiredError(f"{role}: command does not start with the sandbox launcher: {' '.join(map(str, cmd[:4]))} ...")
    if not pol.get("restrict_net", True):
        raise SandboxRequiredError(f"{role}: policy leaves the network open")
    if not pol.get("restrict_fs", True) and not allow_open_fs:
        raise SandboxRequiredError(f"{role}: policy leaves the filesystem open (only sealed held-out scoring may)")
    pre = (env or {}).get("LD_PRELOAD", "")
    if str(SB.ensure_netguard()) not in pre.split(":"):
        raise SandboxRequiredError(f"{role}: LD_PRELOAD does not carry the network guard")
    return pol


def run_confined(cmd: list[str], *, cwd: str | Path, env: dict[str, str] | None = None, mode: str = "workspace",
                 role: str = "workspace_code", **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` of agent-written code, always confined.

    ``workspace``: file and network rules (the gate's policy: system trees, the
    interpreter's venv, paths named on the command line, ``cwd`` writable).
    ``scoring``: network rules only, for sealed scripts that must read held-out
    or reference files next to the code they run.
    """
    base = dict(os.environ if env is None else env)
    if not SB.enforced():                  # only inside a pytest test (policy._test_unconfined); never in a real run
        return subprocess.run(cmd, cwd=str(cwd), env=base, **kwargs)
    if mode == "workspace":
        full, run_env, private = SB.confine_command(list(cmd), cwd=cwd, env=base)
        assert_confined_launch(full, run_env, role=role)
    elif mode == "scoring":
        full, run_env, private = SB.confine_scoring(list(cmd), base)
        assert_confined_launch(full, run_env, role=role, allow_open_fs=True)
    else:
        raise ValueError(f"mode must be workspace or scoring, not {mode!r}")
    try:
        return subprocess.run(full, cwd=str(cwd), env=run_env, **kwargs)
    finally:
        if private is not None:
            SB.clean_private(private)


def sandboxed_codex_config(workspace: str | Path, private: str | Path, *, base_config: Any, role: str,
                           venv: str | Path | None = None, run_dir: str | Path | None = None,
                           extra_read: list[str] | None = None, testkit: str = "", manifest: dict[str, Any] | None = None) -> Any:
    """The CodexConfig for one confined Codex app-server: every command it starts inherits the Landlock
    rules and the network guard. The single way to start Codex (AdaMAS backend and the baselines).

    ``base_config``: a CodexConfig carrying the caller's overrides (model endpoint, trust); its launch
    is replaced by the launcher. Raises ``SandboxRequiredError`` when the sandbox cannot be in force.
    """
    require_sandbox(role)
    from openai_codex import CodexConfig
    from openai_codex.client import _resolve_codex_bin

    workspace, private = str(workspace), Path(private)
    codex_bin = str(_resolve_codex_bin(base_config))
    reads = list(extra_read or []) + ([testkit] if testkit else [])
    pol = SB.agent_policy(workspace=workspace, private=private, venv=venv or None, codex_bin=codex_bin, extra_read=reads or None)
    pol_path = SB.write_policy(pol, private / "policy.json")
    args = [codex_bin]
    for kv in (*base_config.config_overrides, 'shell_environment_policy.inherit="all"'):
        args += ["--config", kv]
    args += ["app-server", "--listen", "stdio://"]
    env = SB.launcher_env(SB.private_env(private))
    if venv:
        # `python` / `pytest` resolve to the task venv, not to a system Python whose packages are hidden
        env["PATH"] = f"{venv}/bin" + os.pathsep + env.get("PATH", "")
    if testkit:
        # the author's own test runs import adamas_testkit; the launcher restores this PYTHONPATH after confining
        env["ADAMAS_SANDBOX_ORIG_PYTHONPATH"] = os.pathsep.join(x for x in (testkit, env.get("ADAMAS_SANDBOX_ORIG_PYTHONPATH", "")) if x)
    # pytest searches every ancestor for its config and starts collecting at the rootdir it finds;
    # the workspace lives under the repository, whose tree the agent may not list
    env["PYTEST_ADDOPTS"] = " ".join(x for x in (env.get("PYTEST_ADDOPTS", ""), f"--rootdir={workspace}",
                                                f"--confcutdir={workspace}") if x)
    if run_dir is not None:
        SB.write_manifest(Path(run_dir), {"kind": "agent", "role": role, **(manifest or {}),
                                          "policy": pol.to_dict() | {"connect_ports": "see network"},
                                          "private_dir": str(private), "workspace": workspace})
    launch = (*SB.launcher(pol_path), *args)
    assert_confined_launch(list(launch), env, role=role)
    return CodexConfig(launch_args_override=launch, env=env, cwd=workspace)


__all__ = ["MIN_ABI", "SandboxRequiredError", "assert_confined_launch", "require_sandbox", "run_confined", "sandboxed_codex_config"]
