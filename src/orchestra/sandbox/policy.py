"""Sandbox policies, the network guard and the per-run manifest (sandbox spec A, 2026-10-09).

Mechanism, chosen because the host allows nothing else (no namespaces, no
CAP_SYS_ADMIN / CAP_NET_ADMIN):

1. **Landlock filesystem** (``landlock.apply``). An agent process and every
   command it runs read only:
   - its workspace and that workspace's git directories;
   - a private home / tmp / CODEX_HOME;
   - its task's own virtual environment and the Python installs it links to;
   - the Codex binary;
   - system trees (``/usr``, ``/bin``, ``/lib*``, ``/etc``, ``/proc``,
     ``/sys``, ``/dev``).

   Everything else fails with EACCES before the read. That covers memory/, the
   dataset (held-out and reference), the verification suites, other runs,
   outputs/, configs/, the repository source, ``/tmp`` and ``~/.codex``.
2. **Landlock TCP**: ``connect()`` only to the model proxy's port and the local
   ephemeral range (32768-60999). External services on their ports (80, 443,
   22, 9418, ...) are unreachable for every program, static binaries included.
3. **netguard** (``LD_PRELOAD``): every dynamically linked program (bash,
   python, git, curl, pip) can connect / sendto / sendmsg only to loopback or
   AF_UNIX addresses, on any port.
4. The after-the-fact audit of the captured turn items stays as a second line.

Gate tests run under the same filesystem rules (workspace, the run's harness
directory with the gate suites and the testkit, the task venv) and the same
network rules. Held-out scoring keeps its file access and gets the network
rules only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from orchestra.sandbox.landlock import Policy, abi_version, port_ranges

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "configs" / "sandbox.yaml"
ENFORCED_ENV = "ADAMAS_SANDBOX_ENFORCED"
VENV_ENV = "ADAMAS_SANDBOX_VENV"
NETGUARD_SRC = Path(__file__).resolve().parent / "native" / "netguard.c"
NETGUARD_DIR = Path("/usr/local/lib/adamas")
SYSTEM_READ = ["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/proc", "/sys", "/.uv/python_install"]
DEV_WRITE = ["/dev"]
EPHEMERAL = (32768, 60999)


def load_config(experiment: dict[str, Any] | None = None) -> dict[str, Any]:
    base = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.is_file() else {}
    cfg = {k: dict(v or {}) for k, v in (base or {}).items()}
    for k in ("sandbox", "author"):
        cfg.setdefault(k, {}).update(((experiment or {}).get(k) or {}) if isinstance((experiment or {}).get(k), dict) else {})
    flag = (os.environ.get(ENFORCED_ENV) or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        cfg["sandbox"]["enforced"] = True
    elif flag in ("0", "false", "no", "off"):
        cfg["sandbox"]["enforced"] = False
    return cfg


def enforced() -> bool:
    return (os.environ.get(ENFORCED_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def proxy_port() -> int:
    u = urlparse((os.environ.get("OPENAI_BASE_URL") or "http://127.0.0.1:8317/v1").strip())
    return int(u.port or (443 if u.scheme == "https" else 80))


def ensure_netguard() -> Path:
    """Build libnetguard.so into a system tree every sandboxed process may read."""
    src = NETGUARD_SRC.read_bytes()
    tag = hashlib.sha256(src).hexdigest()[:12]
    out = NETGUARD_DIR / f"libnetguard-{tag}.so"
    if not out.is_file():
        NETGUARD_DIR.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        subprocess.run(["gcc", "-shared", "-fPIC", "-O2", "-o", str(tmp), str(NETGUARD_SRC), "-ldl"], check=True, capture_output=True)
        tmp.rename(out)
    return out


PYTEST_CONFIG_NAMES = ("pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg")


def ancestor_configs(workspace: str | Path) -> list[str]:
    """pytest looks for its config in every ancestor of the test path and fails on one it may not open;
    those files (and only those, no directory listing) are readable."""
    out = []
    for d in Path(workspace).resolve().parents:
        for n in PYTEST_CONFIG_NAMES:
            if (d / n).is_file():
                out.append(str(d / n))
    return out


def git_dirs(workspace: Path) -> list[str]:
    """The workspace's git metadata outside it: a worktree's gitdir and the common dir."""
    g = Path(workspace) / ".git"
    out: list[str] = []
    if g.is_file():
        text = g.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            gd = Path(text.split(":", 1)[1].strip())
            gd = gd if gd.is_absolute() else (Path(workspace) / gd).resolve()
            out.append(str(gd))
            cd = gd / "commondir"
            if cd.is_file():
                common = Path(cd.read_text(encoding="utf-8").strip())
                out.append(str(common if common.is_absolute() else (gd / common).resolve()))
    return out


def venv_reads(venv: str | Path | None) -> list[str]:
    if not venv:
        return []
    v = Path(venv)
    out = [str(v)]
    py = v / "bin" / "python"
    if py.exists():
        out.append(str(Path(os.path.realpath(py)).parent.parent))
    return out


def net_ports() -> list[int]:
    return sorted({proxy_port(), *port_ranges([EPHEMERAL])})


def agent_policy(*, workspace: str | Path, private: str | Path, venv: str | Path | None, codex_bin: str | Path | None = None,
                 extra_read: list[str] | None = None) -> Policy:
    read = SYSTEM_READ + venv_reads(venv) + [str(ensure_netguard().parent)]
    if codex_bin:
        read.append(str(Path(os.path.realpath(codex_bin)).parent))
    read += list(extra_read or []) + ancestor_configs(workspace)
    write = [str(workspace), *git_dirs(Path(workspace)), str(private), *DEV_WRITE]
    return Policy(read=read, write=write, connect_ports=net_ports())


def gate_policy(*, workspace: str | Path, private: str | Path, venv: str | Path | None, harness_dir: str | Path,
                testkit_dir: str | Path | None = None) -> Policy:
    read = SYSTEM_READ + venv_reads(venv) + [str(harness_dir), str(ensure_netguard().parent)] + ancestor_configs(workspace)
    if testkit_dir:
        read.append(str(testkit_dir))
    return Policy(read=read, write=[str(workspace), *git_dirs(Path(workspace)), str(private), *DEV_WRITE], connect_ports=net_ports())


def scoring_policy() -> Policy:
    """Held-out scoring: file access unchanged (no filesystem rules), network as for agents."""
    return Policy(read=[], write=[], connect_ports=net_ports(), restrict_net=True, restrict_fs=False)


def confine_scoring(command: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str], Path | None]:
    """Held-out scoring under the network rules only (sandbox spec A §1); file access as before."""
    if not enforced():
        return command, env, None
    import tempfile

    private = Path(tempfile.mkdtemp(prefix="adamas-score-"))
    pol_path = write_policy(scoring_policy(), private / "policy.json")
    out = launcher_env(dict(env))
    out["LD_PRELOAD"] = str(ensure_netguard())
    return [*launcher(pol_path), *command], out, private


def private_env(private: Path, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """HOME, TMPDIR and CODEX_HOME inside the private directory, plus the network guard."""
    env = dict(base if base is not None else os.environ)
    for sub in ("home", "tmp", "codex_home"):
        (private / sub).mkdir(parents=True, exist_ok=True)
    env.update({"HOME": str(private / "home"), "TMPDIR": str(private / "tmp"), "TMP": str(private / "tmp"),
                "TEMP": str(private / "tmp"), "CODEX_HOME": str(private / "codex_home"),
                "LD_PRELOAD": str(ensure_netguard()), "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "XDG_CACHE_HOME": str(private / "tmp" / "cache"), "PYTHONDONTWRITEBYTECODE": "1"})
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy", "UV_CACHE_DIR"):
        env.pop(k, None)
    return env


def confine_command(command: list[str], *, cwd: str | Path, env: dict[str, str] | None) -> tuple[list[str], dict[str, str], Path | None]:
    """Wrap a driver-side run of workspace code (gate, repair evidence, resample frames) when enforced.

    Reads: system trees, the interpreter's venv, every existing path named on the
    command line or in PYTHONPATH (the harness script, suites, evidence copies);
    writes: ``cwd`` (the workspace) and a private tmp. Network as for agents.
    Returns (command, env, private dir to remove afterwards or None).
    """
    base_env = dict(os.environ if env is None else env)
    if not enforced() or not command:
        return command, base_env, None
    import tempfile

    private = Path(tempfile.mkdtemp(prefix="adamas-run-"))
    py = Path(command[0])
    venv = py.parent.parent if py.parent.name == "bin" else None
    read = SYSTEM_READ + venv_reads(venv) + [str(ensure_netguard().parent)]
    if venv is None and py.is_absolute():
        read.append(str(py.parent))
    cands = [a.split("::", 1)[0] for a in command[1:]] + (base_env.get("PYTHONPATH") or "").split(os.pathsep)
    for a in cands:
        if a and os.path.isabs(a) and os.path.exists(a):
            for x in {a, os.path.realpath(a)}:          # a symlinked suite is checked at its real path
                read.append(x if os.path.isdir(x) else str(Path(x).parent))
    read += ancestor_configs(cwd)
    pol = Policy(read=read, write=[str(cwd), *git_dirs(Path(cwd)), str(private), *DEV_WRITE], connect_ports=net_ports())
    pol_path = write_policy(pol, private / "policy.json")
    return [*launcher(pol_path), *command], launcher_env(private_env(private, base=base_env)), private


def write_policy(policy: Policy, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy.to_dict()), encoding="utf-8")
    return path


def launcher(policy_path: Path) -> list[str]:
    """The prefix that confines and then execs: ``<python> -m orchestra.sandbox.exec --policy <p> --``."""
    return [sys.executable, "-m", "orchestra.sandbox.exec", "--policy", str(policy_path), "--"]


def launcher_env(env: dict[str, str]) -> dict[str, str]:
    """The launcher imports orchestra.sandbox before it confines itself."""
    out = dict(env)
    out["ADAMAS_SANDBOX_ORIG_PYTHONPATH"] = env.get("PYTHONPATH", "")
    out["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), *[p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p]])
    return out


# --------------------------------------------------------------------------- manifest and self-checks


SELF_CHECK = r'''
import os, socket, sys, json
res = {}
def can_read(p):
    try:
        if os.path.isdir(p):
            os.listdir(p)
        else:
            open(p, "rb").read(1)
        return True
    except OSError:
        return False
for name, p in json.loads(sys.argv[1]).items():
    res["read:" + name] = "ALLOWED" if can_read(p) else "denied"
def conn(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((host, port))
        return "connected"
    except OSError as e:
        return type(e).__name__
    finally:
        s.close()
res["net:external_443"] = conn("151.101.0.223", 443)
res["net:proxy"] = conn("127.0.0.1", int(sys.argv[2]))
print(json.dumps(res))
'''


def self_check(policy: Policy, env: dict[str, str], probes: dict[str, str], *, python: str = "/usr/bin/python3") -> dict[str, Any]:
    """Run probes inside the exact launcher an agent gets; returns their outcome."""
    d = Path(env.get("TMPDIR") or "/tmp")
    pol = write_policy(policy, d.parent / "selfcheck_policy.json")
    proc = subprocess.run([*launcher(pol), python, "-c", SELF_CHECK, json.dumps(probes), str(proxy_port())],
                          capture_output=True, text=True, env=launcher_env(env), timeout=60)
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"error": proc.stderr[-500:]}
    ok = all(v == "denied" for k, v in out.items() if k.startswith("read:")) and out.get("net:external_443") != "connected" \
        and out.get("net:proxy") == "connected"
    return {"results": out, "ok": ok}


def default_probes() -> dict[str, str]:
    """Paths an agent must not read: memory/, dataset (held-out + reference), outputs/, configs/, the source, ~/.codex, /tmp."""
    ds = Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
    return {"memory": str(ROOT / "memory" / "VERSION"), "dataset": str(ds), "heldout": str(ds / "tinydb" / "unit_tests"),
            "reference": str(ds / "tinydb" / "tinydb"), "outputs": str(ROOT / "outputs"), "configs": str(ROOT / "configs"),
            "source": str(ROOT / "src"), "codex_home": "/root/.codex", "global_tmp": "/tmp"}


def write_manifest(run_dir: Path, entry: dict[str, Any]) -> None:
    p = Path(run_dir) / "sandbox_manifest.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {
        "scheme": "landlock (fs + tcp connect ports) + LD_PRELOAD netguard (loopback only) + turn-item audit",
        "landlock_abi": abi_version(), "netguard": str(ensure_netguard()), "network": {
            "tcp_connect_ports": [proxy_port(), f"{EPHEMERAL[0]}-{EPHEMERAL[1]}"],
            "dynamic_programs": "connect/sendto/sendmsg only to loopback or AF_UNIX",
            "static_programs": "tcp connect only to the ports above", "udp": "loopback only for dynamic programs; not covered by landlock"},
        "entries": []}
    data["entries"].append(entry)
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")


def clean_private(private: Path) -> None:
    shutil.rmtree(private, ignore_errors=True)


__all__ = [
    "CONFIG_PATH", "ENFORCED_ENV", "VENV_ENV", "agent_policy", "confine_command", "confine_scoring", "default_probes", "enforced", "ensure_netguard", "gate_policy",
    "git_dirs", "launcher", "launcher_env", "load_config", "net_ports", "private_env", "proxy_port", "scoring_policy",
    "self_check", "write_manifest", "write_policy",
]
