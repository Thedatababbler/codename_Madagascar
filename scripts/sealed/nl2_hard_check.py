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


if __name__ == "__main__":
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
