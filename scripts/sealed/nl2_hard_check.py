#!/usr/bin/env python3
"""NL2Repo hard-level candidate checks (experiment "NL2Repo 困难级题目上的框架对照", stage 1; 2026-10-15). Sealed.

For each converted task: reference size (files, non-blank lines -- numbers only); whether the task environment holds
the original project (an installed distribution or importable package of that name, an editable install, a .pth
pointing at the upstream clone); the held-out pass rate on the reference and on an empty repository (the dataset
inputs only), both confined under the scoring sandbox (network blocked); document length in tokens (chars / 4).

    uv run python scripts/sealed/nl2_hard_check.py nl2_deepdiff nl2_cerberus ...
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard as G  # noqa: E402
from nl2_ceiling import run_heldout_cases  # noqa: E402

OUT = ROOT / "outputs" / "nl2repo_hard" / "checks.json"


def ref_size(task: str) -> dict:
    cfg = G.task_config(task)
    src = G.reference_root(task) / str(cfg.get("source_code"))
    files = [src] if src.is_file() else [p for p in src.rglob("*.py") if "__pycache__" not in p.parts]
    lines = sum(len([ln for ln in p.read_text(errors="replace").splitlines() if ln.strip()]) for p in files)
    return {"reference_py_files": len(files), "reference_nonblank_lines": lines}


def env_check(task: str) -> dict:
    cfg = G.task_config(task)
    name = Path(str(cfg.get("source_code"))).name.removesuffix(".py")
    py = G.task_python(task)
    probe = (
        "import importlib.util, json, sys, pathlib, importlib.metadata as md\n"
        f"name={name!r}\n"
        "spec=importlib.util.find_spec(name)\n"
        "dists=[d.metadata['Name'] for d in md.distributions()]\n"
        "pths=[str(p) for sp in sys.path if sp and pathlib.Path(sp).is_dir() for p in pathlib.Path(sp).glob('*.pth')]\n"
        "pth_hits=[p for p in pths if 'adamas_sealed' in open(p, errors='replace').read() or 'upstream' in open(p, errors='replace').read()]\n"
        "print(json.dumps({'importable_outside_workspace': bool(spec), 'origin': (spec.origin if spec else None),"
        " 'dist_match': [d for d in dists if d and d.lower().replace('_','-') in (name.lower().replace('_','-'), name.lower())],"
        " 'pth_to_upstream': pth_hits}))\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run([str(py), "-I", "-c", probe], cwd=tmp, capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": (r.stderr or "")[-200:]}
    out["clean"] = not out["importable_outside_workspace"] and not out["dist_match"] and not out["pth_to_upstream"]
    out["origin"] = "set" if out.get("origin") else None
    return out


def empty_repo(task: str, dest: Path) -> Path:
    """The repository an agent starts from: documents, requirements, check_tests -- no source, no held-out."""
    root = G.dataset_root_for(task) / task
    dest.mkdir(parents=True)
    for name in ("docs", "requirements.txt", "check_tests"):
        p = root / name
        if p.is_dir():
            subprocess.run(["cp", "-r", str(p), str(dest / name)], check=True)
        elif p.is_file():
            subprocess.run(["cp", str(p), str(dest / name)], check=True)
    return dest


def main() -> int:
    tasks = sys.argv[1:]
    data = json.loads(OUT.read_text()) if OUT.is_file() else {}
    for task in tasks:
        rec: dict = {"task": task}
        rec.update(ref_size(task))
        rec["env"] = env_check(task)
        docs = G.dataset_root_for(task) / task / "docs"
        rec["docs_tokens_est"] = sum(len(p.read_text(errors="replace")) for p in docs.glob("*") if p.is_file()) // 4
        ref = run_heldout_cases(task, G.reference_root(task))
        rec["reference"] = {"case_pass_rate": ref["case_pass_rate"], "case_counts": ref["case_counts"], "node_counts": ref["node_counts"]}
        with tempfile.TemporaryDirectory() as tmp:
            emp = run_heldout_cases(task, empty_repo(task, Path(tmp) / task))
        rec["empty"] = {"case_pass_rate": emp["case_pass_rate"], "case_counts": emp["case_counts"], "node_counts": emp["node_counts"]}
        text = json.dumps(rec)
        G.assert_no_source_overlap(text, task)
        data[task] = rec
        print(text, flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1))
    return 0


if __name__ == "__main__" and not (len(sys.argv) > 1 and sys.argv[1] in ("--probe", "--kinds")):
    raise SystemExit(main())


def total_lines(task: str) -> int:
    cfg = G.task_config(task)
    src = G.reference_root(task) / str(cfg.get("source_code"))
    files = [src] if src.is_file() else [p for p in src.rglob("*.py") if "__pycache__" not in p.parts]
    return sum(len(p.read_text(errors="replace").splitlines()) for p in files)


def collection_errors(task: str) -> list[tuple[str, str]]:
    """(held-out module, exception class) for every module that fails to collect on the reference. Confined."""
    import os
    import re
    import shutil

    from orchestra.sandbox.guard import run_confined

    py = G.task_python(task)
    sub = G.heldout_subdir_name(task)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task
        shutil.copytree(G.reference_root(task), repo, ignore=shutil.ignore_patterns("__pycache__", sub, "check_tests"))
        shutil.copytree(G.heldout_dir(task), repo / sub)
        env = {"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
               "PATH": f"{py.parent}:/usr/bin:/bin", "HOME": str(repo)}
        r = run_confined([str(py), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors", sub],
                         cwd=repo, mode="scoring", role="nl2_hard_check", capture_output=True, text=True, timeout=900, check=False, env=env)
    out = []
    for m in re.finditer(r"_+ ERROR collecting (\S+) _+\n(.*?)(?=\n_+ ERROR collecting|\n=+ short test summary|\Z)", r.stdout, re.S):
        exc = re.findall(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception))", m.group(2), re.M)
        missing = re.findall(r"No module named '([\w.]+)'", m.group(2)) or re.findall(r"cannot import name '\w+' from '([\w.]+)'", m.group(2))
        out.append((m.group(1).split(sub + "/", 1)[-1], exc[-1] if exc else "?", missing[-1].split(".")[0] if missing else ""))
    return out


def probe_repo(task: str, repo_src: Path) -> dict:
    """Collection of the held-out suite on any repository: counts and missing module names only. Confined."""
    import os
    import re
    import shutil

    from orchestra.sandbox.guard import run_confined

    py = G.task_python(task)
    sub = G.heldout_subdir_name(task)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task
        shutil.copytree(repo_src, repo, ignore=shutil.ignore_patterns("__pycache__", sub, ".git"))
        shutil.copytree(G.heldout_dir(task), repo / sub)
        env = {"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
               "PATH": f"{py.parent}:/usr/bin:/bin", "HOME": str(repo)}
        r = run_confined([str(py), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors", sub],
                         cwd=repo, mode="scoring", role="nl2_hard_check", capture_output=True, text=True, timeout=900, check=False, env=env)
    return {"collected": re.findall(r"(\d+) tests? collected", r.stdout),
            "missing_modules": sorted(set(re.findall(r"No module named '([\w.]+)'", r.stdout))),
            "cannot_import_from": sorted(set(re.findall(r"cannot import name '\w+' from '([\w.]+)'", r.stdout))),
            "collection_errors": len(re.findall("ERROR collecting", r.stdout)),
            "conftest_error": bool(re.search(r"ImportError while loading conftest|Error while loading conftest", r.stdout + r.stderr)),
            "exception_types": sorted(set(re.findall(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception))", r.stdout + r.stderr, re.M))),
            "modules_named": sorted(set(re.findall(r"(?:No module named|from) '([\w.]+)'", r.stdout + r.stderr))),
            "exit_code": r.returncode,
            "error_lines": sorted(set(re.sub(r"'[^']*'", lambda m: m.group(0) if re.fullmatch(r"'[\w.]+'", m.group(0)) else "'…'", ln.strip())[:160]
                                      for ln in (r.stdout + r.stderr).splitlines()
                                      if re.match(r"^\s*E?\s*\w*(Error|Exception)\b", ln) and "Traceback" not in ln))[:8]}


def probe_task(task: str, run_repo: str | None = None) -> dict:
    """probe_repo on the reference, on an empty repository and (optionally) on a run's repository."""
    out = {"reference": probe_repo(task, G.reference_root(task))}
    with tempfile.TemporaryDirectory() as tmp:
        out["empty"] = probe_repo(task, empty_repo(task, Path(tmp) / task))
    if run_repo:
        out["run"] = probe_repo(task, Path(run_repo))
    return out


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--probe":
    print(json.dumps(probe_task(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)))


def failure_kinds(task: str, repo_src: Path, file_timeout: int = 600) -> dict:
    """Held-out outcome by failure category on a run's repository, one confined pytest per test file (junit types).
    Categories: import / attr_name / type / assertion / timeout / other, plus collection failures of whole files
    (counted at their pinned size) and hung files. Counts only."""
    import os
    import re
    import shutil
    import subprocess
    import xml.etree.ElementTree as ET

    from orchestra.sandbox.guard import run_confined

    def cat(t: str, msg: str) -> str:
        t = t or ""
        if t.endswith(("ImportError", "ModuleNotFoundError")):
            return "import"
        if t.endswith(("AttributeError", "NameError")):
            return "attr_name"
        if t.endswith("TypeError"):
            return "type"
        if t.endswith("AssertionError") or msg.lstrip().startswith("assert") or "DID NOT RAISE" in msg:
            return "assertion"
        if "Timeout" in t or "Timeout" in msg:
            return "timeout"
        return "other"

    py = G.task_python(task)
    sub = G.heldout_subdir_name(task)
    pinned = json.loads((ROOT / "configs" / "codeprojecteval_suite_sizes.json").read_text())["tasks"].get(task) or {}
    out = {"passed": 0, "failed_by_kind": {}, "collection_failed_cases": 0, "collection_failed_files": 0,
           "collection_kinds": {}, "hung_files": 0, "hung_cases": 0, "files": 0}
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task
        shutil.copytree(repo_src, repo, ignore=shutil.ignore_patterns("__pycache__", sub, ".git"))
        shutil.copytree(G.heldout_dir(task), repo / sub)
        env = {"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
               "PATH": f"{py.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
        for mod, n in sorted(pinned.items()):
            if mod.startswith("Test:") or not n:
                continue
            out["files"] += 1
            rel = mod.split(f"{sub}/", 1)[-1]
            xml = Path(tmp) / "r.xml"
            xml.unlink(missing_ok=True)
            hung = False
            try:
                r = run_confined([str(py), "-m", "pytest", f"{sub}/{rel}", "-q", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
                                  "--continue-on-collection-errors", "--timeout=30", "--timeout-method=signal",
                                  "-o", "junit_family=xunit1", f"--junitxml={xml}"],
                                 cwd=repo, mode="scoring", role="nl2_hard_check", capture_output=True, text=True,
                                 timeout=file_timeout, check=False, env=env)
                text = r.stdout + r.stderr
            except subprocess.TimeoutExpired:
                hung = True
                text = ""
            seen = 0
            if xml.is_file():
                for tc in ET.parse(xml).getroot().iter("testcase"):
                    bad = tc.find("failure")
                    if bad is None:
                        bad = tc.find("error")
                    if tc.find("skipped") is not None:
                        seen += 1
                        continue
                    if not tc.get("classname") and not tc.get("file") and bad is not None:
                        continue                                   # a collection error entry, counted below
                    seen += 1
                    if bad is None:
                        out["passed"] += 1
                    else:
                        k = cat(bad.get("type") or "", bad.get("message") or "")
                        out["failed_by_kind"][k] = out["failed_by_kind"].get(k, 0) + 1
            if hung:
                out["hung_files"] += 1
                out["hung_cases"] += max(0, n - seen)
            elif seen < n:
                out["collection_failed_cases"] += n - seen
                out["collection_failed_files"] += 1
                m = re.findall(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception))", text, re.M)
                k = cat(m[-1] if m else "", "")
                out["collection_kinds"][k] = out["collection_kinds"].get(k, 0) + 1
    out["total_pinned"] = sum(v for k, v in pinned.items() if not k.startswith("Test:"))
    return out


if __name__ == "__main__" and len(sys.argv) > 2 and sys.argv[1] == "--kinds":
    print(json.dumps(failure_kinds(sys.argv[2], Path(sys.argv[3]))))
