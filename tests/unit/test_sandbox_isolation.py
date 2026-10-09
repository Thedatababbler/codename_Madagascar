"""Sandbox spec A: Landlock policy, launcher, off-switch identity."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orchestra.sandbox import landlock as L
from orchestra.sandbox import policy as SB

pytestmark = pytest.mark.skipif(L.abi_version() < 4, reason="needs Landlock ABI >= 4")


@pytest.fixture(autouse=True)
def _env():
    old = os.environ.get(SB.ENFORCED_ENV)
    yield
    if old is None:
        os.environ.pop(SB.ENFORCED_ENV, None)
    else:
        os.environ[SB.ENFORCED_ENV] = old


def _run(policy: L.Policy, private: Path, cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    pp = SB.write_policy(policy, private / "policy.json")
    return subprocess.run([*SB.launcher(pp), *cmd], cwd=cwd, env=SB.launcher_env(SB.private_env(private)),
                          capture_output=True, text=True, timeout=60)


def _dirs():
    # outside /tmp: a confined pytest/rootdir walk must not need /tmp itself
    base = Path(__file__).resolve().parents[2] / "outputs" / "sandbox" / f"unit-{os.getpid()}"
    ws, priv, secret = base / "ws", base / "priv", base / "secret"
    for d in (ws, priv, secret):
        d.mkdir(parents=True, exist_ok=True)
    (secret / "s.txt").write_text("secret")
    (ws / "a.txt").write_text("ok")
    return base, ws, priv, secret


def test_reads_outside_policy_are_refused_before_reading():
    base, ws, priv, secret = _dirs()
    pol = SB.agent_policy(workspace=ws, private=priv, venv=None)
    r = _run(pol, priv, ["bash", "-c", f"cat {ws}/a.txt; cat {secret}/s.txt; ls {base}"], ws)
    assert "ok" in r.stdout and "secret" not in r.stdout
    assert r.stderr.count("Permission denied") >= 2


def test_network_external_refused_loopback_allowed():
    base, ws, priv, _ = _dirs()
    code = ("import socket,threading;srv=socket.socket();srv.bind(('127.0.0.1',0));srv.listen();p=srv.getsockname()[1];"
            "threading.Thread(target=srv.accept,daemon=True).start();c=socket.socket();c.connect(('127.0.0.1',p));print('local-ok');"
            "e=socket.socket();e.settimeout(3);print('ext',e.connect_ex(('151.101.0.223',443)))")
    r = _run(SB.agent_policy(workspace=ws, private=priv, venv=None), priv, ["/usr/bin/python3", "-c", code], ws)
    assert "local-ok" in r.stdout
    assert "ext 13" in r.stdout          # EACCES, refused locally


def test_git_dirs_of_a_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    (main / "f").write_text("x")
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A"], cwd=main, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "c"], cwd=main, check=True)
    subprocess.run(["git", "worktree", "add", "-q", str(tmp_path / "wt")], cwd=main, check=True)
    dirs = SB.git_dirs(tmp_path / "wt")
    assert any(d.endswith("worktrees/wt") for d in dirs) and str((main / ".git").resolve()) in dirs


def test_confine_command_is_identity_when_off(tmp_path):
    os.environ[SB.ENFORCED_ENV] = "0"
    cmd = ["python", "-c", "print(1)"]
    out, env, private = SB.confine_command(cmd, cwd=tmp_path, env={"A": "1"})
    assert out == cmd and env == {"A": "1"} and private is None


def test_confine_command_wraps_when_on(tmp_path):
    os.environ[SB.ENFORCED_ENV] = "1"
    out, env, private = SB.confine_command(["/usr/bin/python3", "-c", "print(1)"], cwd=tmp_path, env={})
    assert out[1:4] == ["-m", "orchestra.sandbox.exec", "--policy"] and env["LD_PRELOAD"].endswith(".so") and private is not None


def test_scoring_policy_keeps_files_restricts_network(tmp_path):
    os.environ[SB.ENFORCED_ENV] = "1"
    (tmp_path / "f").write_text("readable")
    cmd, env, _ = SB.confine_scoring(["/usr/bin/python3", "-c",
                                      f"import socket;print(open('{tmp_path}/f').read());s=socket.socket();s.settimeout(3);"
                                      "print(s.connect_ex(('151.101.0.223',443)))"], {"PATH": "/usr/bin:/bin"})
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    assert "readable" in r.stdout and "13" in r.stdout


def test_codex_config_unchanged_when_off():
    from orchestra.backends import codex_sdk as C

    os.environ[SB.ENFORCED_ENV] = "0"
    assert C._sandboxed_config(None, None, "/x") is None
