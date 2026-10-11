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
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestra.sandbox import policy as SB  # noqa: E402
from orchestra.sandbox.policy import BLOCKED_DIR_NAMES  # noqa: E402

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
        # third-party packages of the system and other Pythons (a task's own project can sit there: PyJWT)
        "system_jwt_source": "/usr/local/lib/python3.12/dist-packages/jwt/__init__.py",
        "usr_lib_dist_packages_file": next((str(p) for p in Path("/usr/lib/python3/dist-packages").rglob("*.py")), "/usr/lib/python3/dist-packages/x.py"),
        "uv_base_site_packages_file": next((str(p) for p in Path("/.uv/python_install/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/site-packages").rglob("*.py")),
                                           "/.uv/python_install/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/site-packages/x.py"),
        "other_venv_jwt": "/venv/mas/lib/python3.11/site-packages/jwt/__init__.py",
    }
    # the explicit deny list (2026-10-11): every listed directory, and a file inside it when there is one
    for d in SB.explicit_deny_paths():
        key = "deny:" + d
        forbidden[key] = d
        f = next((str(x) for x in Path(d).rglob("*.py")), None) if Path(d).is_dir() else None
        if f:
            forbidden[key + ":file"] = f
    ws, priv, td = fresh()
    allowed = {"workspace_file": str(ws / "hello.py"), "task_venv": str(VENV / "bin"), "usr_bin": "/usr/bin",
               "task_venv_site_packages": str(next((VENV / "lib").glob("python3*/site-packages"))),
               "stdlib": "/usr/lib/python3.12/os.py"}
    probe = priv / "probe.py"
    probe.write_text(PROBE)
    r = confined(["/usr/bin/python3", str(probe), json.dumps({**forbidden, **allowed})], workspace=ws, private=priv)
    res = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {"error": r.stderr[-500:]}
    # shell tools: cat / ls / find with the same launcher
    sh = confined([f"cat {forbidden['memory_file']} 2>&1 | head -1; ls {ROOT} 2>&1 | head -1; "
                   f"git -C {ws} status --short | head -1; echo GIT_RC=$?; "
                   f"{VENV}/bin/python -c 'import pytest; print(\"pytest_ok\")'; "
                   "/usr/bin/python3 -c 'import jwt' 2>&1 | tail -1; "
                   "PATH=" + str(VENV / "bin") + ":$PATH python -c 'import sys; print(\"python_is\", sys.prefix)'"],
                  workspace=ws, private=priv, shell=True)
    find = confined(["find / -xdev \\( -name pitfalls.yaml -o -name VERSION -o -name 'unit_tests' -o -name '*.spec_tests' "
                     "-o -name sandbox.yaml -o -name 'logs_2.sqlite' \\) 2>/dev/null | head -50"], workspace=ws, private=priv,
                    shell=True, timeout=600)
    found = [ln for ln in find.stdout.splitlines() if ln.strip()]
    leaky = [ln for ln in found if any(k in ln for k in ("/memory/", "python-subset", "cpe_format", "/outputs/", "/configs/",
                                                          ".codex", "spec_tests"))]
    td.cleanup()
    # a denied package directory under an interpreter's stdlib tree stays listable (names only): Landlock grants
    # readdir on the stdlib directory for the whole hierarchy below it, and Python cannot import without it.
    # Accepted only when a file inside it is refused.
    names_only = sorted(k for k in forbidden if k.startswith("deny:") and not k.endswith(":file") and res.get(k) == "READ"
                        and any(b in k for b in BLOCKED_DIR_NAMES) and res.get(k + ":file") in ("EACCES", "EPERM"))
    ok = all(res.get(k) in ("EACCES", "ENOENT", "EPERM") or k in names_only for k in forbidden) \
        and all(res.get(k) == "READ" for k in allowed) \
        and not leaky and "pytest_ok" in sh.stdout and ("No module named 'jwt'" in sh.stdout or "PermissionError" in sh.stdout)
    return {"reads": res, "names_only_dirs": names_only, "shell": sh.stdout.strip().splitlines(), "find_hits": found,
            "find_forbidden_hits": leaky, "pass": ok,
            "note": "system / uv dist-packages and site-packages directories list their names (readdir only, so Python's import scan works); "
                    "no file in them can be read"}


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
        "no_ldpreload_tcp_1.1.1.1:53": "env -u LD_PRELOAD /usr/bin/python3 -c \"import socket;s=socket.socket();s.settimeout(4);"
                                       "print(s.connect_ex(('1.1.1.1',53)))\"",
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

    # every task of both dataset roots (2026-10-11: the 14 transfer tasks alone missed portalocker,
    # nl2_stamina and nl2_pytorch-grad-cam, whose own venvs imported their original project)
    all_tasks = sorted(p.name for ds in (CPE, NL2) for p in Path(ds).iterdir() if (p / "config.json").is_file())
    rows = {}
    for t in all_tasks:
        nl2 = t.startswith("nl2_")
        ds = NL2 if nl2 else CPE
        env = Path("/root/codex-benchmarks/nl2repo/envs" if nl2 else "/root/codex-benchmarks/cpe_envs") / t / "bin" / "python"
        if not env.exists():
            continue
        pk = _top_level_packages(expected_modules(load_task(t, dataset_root=ds))) + [t.removeprefix("nl2_")]
        rows[t] = check(env, pk)
    # copies installed elsewhere (system Pythons, other venvs): found unconfined, then read inside the sandbox
    import subprocess as sp
    pkgs = {t: [p for p in r["packages"] if "-" not in p] for t, r in rows.items()}
    names = sorted({p for ps in pkgs.values() for p in ps})
    found = sp.run(["find", "/", "-xdev", "(", "-path", "/proc", "-o", "-path", "/sys", "-o", "-path", str(ROOT), ")", "-prune", "-o",
                    "-type", "d", "(", *sum([["-name", n, "-o"] for n in names], [])[:-1], ")", "-path", "*-packages/*", "-print"],
                   capture_output=True, text=True, timeout=900).stdout.split()
    elsewhere = {}
    for t, ps in pkgs.items():
        own = str(Path("/root/codex-benchmarks/nl2repo/envs" if t.startswith("nl2_") else "/root/codex-benchmarks/cpe_envs") / t)
        # a real copy is an importable package directly in a *-packages dir (not a stub, not a submodule of another package)
        elsewhere[t] = [f for f in found if Path(f).name in ps and not f.startswith(own + "/")
                        and Path(f).parent.name in ("site-packages", "dist-packages") and (Path(f) / "__init__.py").is_file()]
    ws, priv, td = fresh()
    probe = priv / "probe.py"
    probe.write_text(PROBE)
    targets = {f"{t}:{i}": str(Path(f) / "__init__.py") for t, fs in elsewhere.items() for i, f in enumerate(fs)}
    # every original-project copy the machine-wide sweep found that is still on disk (scripts/sealed/copy_sweep.py)
    sweep = ROOT / "outputs" / "sandbox" / "copy_sweep" / "summary.json"
    swept: list[str] = []
    if sweep.is_file():
        sw = json.loads(sweep.read_text())
        cands = [p["path"] for p in sw["package_dirs"] if p["similarity"] >= 0.65] + \
                [a["path"] for a in sw["archives"] if a["similarity"] >= 0.65]
        for c in sorted(set(cands)):
            if c.startswith(str(ROOT)) or not Path(c).exists():
                continue
            f = next((x for x in sorted(Path(c).rglob("*.py"))), None) if Path(c).is_dir() else Path(c)
            if f is not None:
                swept.append(str(f))
        targets.update({f"sweep:{i}": f for i, f in enumerate(swept)})
    r = confined(["/usr/bin/python3", str(probe), json.dumps(targets)], workspace=ws, private=priv)
    reads = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {"error": r.stderr[-300:]}
    td.cleanup()
    copies = {t: [{"path": f, "inside_sandbox": reads.get(f"{t}:{i}")} for i, f in enumerate(fs)] for t, fs in elsewhere.items() if fs}
    swept_reads = {f: reads.get(f"sweep:{i}") for i, f in enumerate(swept)}
    # the probe's own venv is readable by design; a dependency there exposes only another task's project, never the
    # venv task's own (checked above for every task): counted apart, not a failure
    probe_venv = str(Path(VENV).resolve()) + "/"
    own_venv_deps = {f: v for f, v in swept_reads.items() if f.startswith(probe_venv)}
    swept_reads = {f: v for f, v in swept_reads.items() if f not in own_venv_deps}
    readable = [c for cs in copies.values() for c in cs if c["inside_sandbox"] == "READ" and not c["path"].startswith(probe_venv)] + \
               [{"path": f, "inside_sandbox": v} for f, v in swept_reads.items() if v == "READ"]
    return {"tasks": rows, "copies_elsewhere": copies, "sweep_copies_inside_sandbox": swept_reads,
            "probe_venv_dependencies": own_venv_deps, "readable_copies_inside_sandbox": readable,
            "pass": all(r["clean"] for r in rows.values()) and not readable}


# --------------------------------------------------------------------------- S5 part 1: command audit

def s5a() -> dict:
    from orchestra.sandbox.audit import audit_run

    runs = sorted({p.split("/tasks/")[0] for p in map(str, ROOT.glob("outputs/**/backend_traces/*/*.items.json"))}
                  | {p.split("/tasks/")[0] for p in map(str, ROOT.glob("outputs/**/backend_traces/*/.sandbox/*/codex_home/sessions/**/*.jsonl"))})
    total, findings = 0, []
    for rd in runs:
        au = audit_run(Path(rd))
        total += au["commands"]
        findings += au["findings"]
    return {"runs": len(runs), "commands": total, "findings": findings,
            "note": "only runs after the 2026-10-09 capture fix have turn items or private Codex session logs"}


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
