#!/usr/bin/env python3
"""Sandbox tests S1-S3 and S5 part 1 (sandbox spec A §4). No model call.

Every probe runs inside the exact launcher and environment an agent gets
(``agent_policy`` + ``private_env`` + ``orchestra.sandbox.exec``), but executes
scripted commands instead of Codex. Results: outputs/sandbox/<test>.json.

    uv run python scripts/sandbox_tests.py s1|s2|s3|s5a
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestra.sandbox import policy as SB  # noqa: E402

OUT = ROOT / "outputs" / "sandbox"
CPE = Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
NL2 = Path("/root/codex-benchmarks/nl2repo/cpe_format")
TASK = "cookiecutter"
VENV = Path("/root/codex-benchmarks/cpe_envs") / TASK


def confined(cmd: list[str], *, workspace: Path, private: Path, shell: bool = False, extra_env: dict | None = None,
             timeout: int = 120) -> subprocess.CompletedProcess:
    pol = SB.agent_policy(workspace=workspace, private=private, venv=VENV,
                          codex_bin="/root/cli-proxy/codex-0.149.1/bin/codex")
    pp = SB.write_policy(pol, private / "policy.json")
    env = SB.launcher_env(SB.private_env(private))
    env.update(extra_env or {})
    full = [*SB.launcher(pp), *(["bash", "-c", cmd[0]] if shell else cmd)]
    return subprocess.run(full, cwd=str(workspace), env=env, capture_output=True, text=True, timeout=timeout)


def fresh() -> tuple[Path, Path, tempfile.TemporaryDirectory]:
    td = tempfile.TemporaryDirectory(prefix="adamas-sbxtest-")
    base = Path(td.name)
    ws = base / "workspace" / "repo"
    ws.mkdir(parents=True)
    (ws / "hello.py").write_text("print('hello')\n")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init"], cwd=ws, check=True)
    priv = base / "private"
    priv.mkdir()
    return ws, priv, td


PROBE = r'''
import errno, json, os, sys
out = {}
for name, p in json.loads(sys.argv[1]).items():
    try:
        if os.path.isdir(p):
            os.listdir(p)
            out[name] = "READ"
        else:
            with open(p, "rb") as fh:
                fh.read(1)
            out[name] = "READ"
    except OSError as e:
        out[name] = errno.errorcode.get(e.errno, str(e.errno))
print(json.dumps(out))
'''


def s1() -> dict:
    vdirs = json.loads((ROOT / "outputs" / "fp_from_repair" / "verifiers.json").read_text())
    vfile = next(Path(next(iter(vdirs.values()))).rglob("test_*.py"))
    other_ws = next((ROOT / "outputs" / "memory" / "t3" / "runs" / "t3-fr-hl7").rglob("workspaces/*/repo"))
    other_file = next(p for p in other_ws.rglob("*.py") if ".git" not in p.parts)
    forbidden = {
        "memory_file": str(ROOT / "memory" / "first_pass" / "pitfalls.yaml"),
        "memory_dir": str(ROOT / "memory"),
        "heldout_dir": str(CPE / "tinydb" / "unit_tests"),
        "heldout_file": str(CPE / "tinydb" / "unit_tests" / "__init__.py"),
        "nl2_heldout_dir": str(NL2 / "nl2_tablib"),
        "reference_source": str(CPE / "tinydb" / "tinydb" / "__init__.py"),
        "reference_dir": str(CPE / TASK),
        "verification_suite_file": str(vfile),
        "other_run_workspace_file": str(other_file),
        "configs_file": str(ROOT / "configs" / "sandbox.yaml"),
        "repo_source": str(ROOT / "src" / "orchestra" / "memory" / "store.py"),
        "outputs_dir": str(ROOT / "outputs"),
        "codex_home_sessions": "/root/.codex/logs_2.sqlite",
        "global_tmp": "/tmp",
        "root_home": "/root",
    }
    ws, priv, td = fresh()
    allowed = {"workspace_file": str(ws / "hello.py"), "task_venv": str(VENV / "bin"), "usr_bin": "/usr/bin"}
    probe = priv / "probe.py"
    probe.write_text(PROBE)
    r = confined(["/usr/bin/python3", str(probe), json.dumps({**forbidden, **allowed})], workspace=ws, private=priv)
    res = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {"error": r.stderr[-500:]}
    # shell tools: cat / ls / find with the same launcher
    sh = confined([f"cat {forbidden['memory_file']} 2>&1 | head -1; ls {ROOT} 2>&1 | head -1; "
                   f"git -C {ws} status --short | head -1; echo GIT_RC=$?; "
                   f"{VENV}/bin/python -c 'import pytest; print(\"pytest_ok\")'"], workspace=ws, private=priv, shell=True)
    find = confined(["find / -xdev \\( -name pitfalls.yaml -o -name VERSION -o -name 'unit_tests' -o -name '*.spec_tests' "
                     "-o -name sandbox.yaml -o -name 'logs_2.sqlite' \\) 2>/dev/null | head -50"], workspace=ws, private=priv,
                    shell=True, timeout=600)
    found = [ln for ln in find.stdout.splitlines() if ln.strip()]
    leaky = [ln for ln in found if any(k in ln for k in ("/memory/", "python-subset", "cpe_format", "/outputs/", "/configs/",
                                                          ".codex", "spec_tests"))]
    td.cleanup()
    ok = all(res.get(k) in ("EACCES", "ENOENT", "EPERM") for k in forbidden) and all(res.get(k) == "READ" for k in allowed) \
        and not leaky and "pytest_ok" in sh.stdout
    return {"reads": res, "shell": sh.stdout.strip().splitlines(), "find_hits": found, "find_forbidden_hits": leaky, "pass": ok}


NET = r'''
import json, socket, sys, threading, urllib.request, http.server
out = {}
def conn(host, port):
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect((host, port)); return "connected"
    except OSError as e:
        return type(e).__name__ + ":" + str(e.errno)
    finally:
        s.close()
for name, (h, p) in {"pypi_443": ("151.101.0.223", 443), "github_443": ("140.82.112.3", 443),
                     "public_80": ("93.184.215.14", 80), "dns_tcp_53": ("1.1.1.1", 53)}.items():
    out[name] = conn(h, p)
try:
    urllib.request.urlopen("https://pypi.org/simple/", timeout=6); out["urllib_pypi"] = "OK"
except Exception as e:
    out["urllib_pypi"] = type(e).__name__
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/", timeout=5).read(); out["local_http_server"] = "OK"
except Exception as e:
    out["local_http_server"] = type(e).__name__ + str(e)
srv.shutdown()
out["proxy_tcp"] = conn("127.0.0.1", int(sys.argv[1]))
print(json.dumps(out))
'''


def s2() -> dict:
    ws, priv, td = fresh()
    probe = priv / "net.py"
    probe.write_text(NET)
    port = str(SB.proxy_port())
    py = confined(["/usr/bin/python3", str(probe), port], workspace=ws, private=priv, timeout=180)
    res = json.loads(py.stdout.strip().splitlines()[-1]) if py.stdout.strip() else {"error": py.stderr[-400:]}
    tools = {}
    for name, cmd in {
        "curl_pypi": "curl -s -m 8 -o /dev/null -w '%{http_code}' https://pypi.org/simple/; echo \" rc=$?\"",
        "curl_example": "curl -s -m 8 -o /dev/null -w '%{http_code}' http://example.com/; echo \" rc=$?\"",
        "pip_download": f"{VENV}/bin/python -m pip download --no-cache-dir --index-url https://pypi.org/simple -d $TMPDIR/dl six 2>&1 | tail -1; echo \"rc=${{PIPESTATUS[0]}}\"",
        "git_clone_https": "git clone -q https://github.com/psf/requests.git $TMPDIR/req 2>&1 | tail -1; echo \"rc=${PIPESTATUS[0]}\"",
        # the network guard removed: only Landlock's port rules remain
        "no_ldpreload_curl_pypi": "env -u LD_PRELOAD curl -s -m 8 -o /dev/null -w '%{http_code}' https://pypi.org/simple/; echo \" rc=$?\"",
        "no_ldpreload_tcp_1.1.1.1:53": f"env -u LD_PRELOAD /usr/bin/python3 -c \"import socket;s=socket.socket();s.settimeout(4);"
                                       f"print(s.connect_ex(('1.1.1.1',53)))\"",
        "no_ldpreload_tcp_external_ephemeral": "env -u LD_PRELOAD /usr/bin/python3 -c \"import socket;s=socket.socket();s.settimeout(4);"
                                               "print(s.connect_ex(('1.1.1.1',40000)))\"",
    }.items():
        r = confined([cmd], workspace=ws, private=priv, shell=True, timeout=120)
        tools[name] = (r.stdout.strip().splitlines() or [""])[-1][:200]
    td.cleanup()
    blocked = lambda v: "connected" not in v and v != "OK"  # noqa: E731
    ok = (all(blocked(res.get(k, "")) for k in ("pypi_443", "github_443", "public_80", "dns_tcp_53", "urllib_pypi"))
          and res.get("local_http_server") == "OK" and res.get("proxy_tcp") == "connected"
          and "000" in tools["curl_pypi"] and "000" in tools["curl_example"] and "rc=0" not in tools["pip_download"]
          and "rc=0" not in tools["git_clone_https"] and "000" in tools["no_ldpreload_curl_pypi"]
          and tools["no_ldpreload_tcp_1.1.1.1:53"].strip() != "0")
    return {"python": res, "tools": tools, "pass": ok,
            "residual": "with LD_PRELOAD removed, a TCP connect to an external host on an ephemeral port (32768-60999) is not "
                        "refused locally (Landlock filters by port only); result: " + tools["no_ldpreload_tcp_external_ephemeral"]}


def s3() -> dict:
    import orchestra.control  # noqa: F401
    from orchestra.codeprojecteval.dataset import load_task
    from orchestra.codeprojecteval.harness import _top_level_packages, expected_modules
    from orchestra.memory.store import load_config
    from orchestra.sandbox.venv_check import check

    tr = load_config()["transfer"]
    rows = {}
    for t in tr["train_tasks"] + tr["test_tasks"]:
        nl2 = t.startswith("nl2_")
        ds = NL2 if nl2 else CPE
        env = Path("/root/codex-benchmarks/nl2repo/envs" if nl2 else "/root/codex-benchmarks/cpe_envs") / t / "bin" / "python"
        pk = _top_level_packages(expected_modules(load_task(t, dataset_root=ds))) + [t.removeprefix("nl2_")]
        rows[t] = check(env, pk)
    return {"tasks": rows, "pass": all(r["clean"] for r in rows.values())}


# --------------------------------------------------------------------------- S5 part 1: command audit

_EXTERNAL = re.compile(r"https?://(?!127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])[^\s'\"]+")
_INSTALL = re.compile(r"\b(pip3?|uv pip|python3? -m pip)\s+(install|download)\b|\bpoetry add\b|\bconda install\b|\bnpm install\b")
_FORBIDDEN = {
    "memory": str(ROOT / "memory") + "/", "configs": str(ROOT / "configs") + "/", "repo_source": str(ROOT / "src") + "/",
    "dataset_cpe": "/root/codex-benchmarks/projectgen/", "dataset_nl2": "/root/codex-benchmarks/nl2repo/cpe_format",
    "codex_home": "/root/.codex", "verification_suites": str(ROOT / "outputs" / "author_probe") + "/",
}


def _commands(items: list) -> list[str]:
    out = []
    for it in items:
        if isinstance(it, dict):
            for k in ("command", "cmd"):
                v = it.get(k)
                if isinstance(v, list):
                    v = " ".join(map(str, v))
                if isinstance(v, str):
                    out.append(v)
            for v in it.values():
                if isinstance(v, (list, dict)):
                    out += _commands(v if isinstance(v, list) else [v])
    return out


def s5a() -> dict:
    files = sorted(ROOT.glob("outputs/**/backend_traces/*/*.items.json"))
    findings = []
    n_cmd = 0
    for f in files:
        try:
            items = json.loads(f.read_text())
        except ValueError:
            continue
        run_dir = str(f).split("/tasks/")[0]
        batch_dir = str(Path(run_dir).parent)
        for c in _commands(items):
            n_cmd += 1
            kinds = []
            if _EXTERNAL.search(c):
                kinds.append("external_network")
            if _INSTALL.search(c):
                kinds.append("package_install")
            bad = [k for k, p in _FORBIDDEN.items() if p in c]
            outs = [m for m in re.findall(re.escape(str(ROOT / "outputs")) + r"/[^\s'\":]+", c) if not m.startswith(batch_dir)]
            if outs:
                bad.append("other_run_outputs")
            if bad:
                kinds.append("forbidden_path:" + ",".join(sorted(set(bad))))
            if kinds:
                findings.append({"file": str(f.relative_to(ROOT)), "kinds": kinds, "command": c[:300]})
    return {"items_files": len(files), "commands": n_cmd, "findings": findings,
            "note": "only runs after the 2026-10-09 capture fix have turn items"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("test", choices=["s1", "s2", "s3", "s5a"])
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    res = {"s1": s1, "s2": s2, "s3": s3, "s5a": s5a}[a.test]()
    (OUT / f"{a.test}.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str)[:4000])
    return 0 if res.get("pass", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
