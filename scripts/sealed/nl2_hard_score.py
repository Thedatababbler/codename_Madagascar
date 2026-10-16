#!/usr/bin/env python3
"""Hang-robust held-out scoring for the NL2Repo hard-level comparison (sealed; 2026-10-16).

The whole-suite scorer hung for an hour on the dbutils smoke run: a deadlock in the agent's pool code blocked a thread
on a lock, which pytest-timeout's signal method cannot interrupt, and the scorer's own limit is 6 h. Here every
held-out test file runs in its own confined pytest process with a wall limit; results that finished count, every
collected case of a file that did not finish counts as failed (a hang is a failure, as at the gate). The denominator
is the pinned suite size (configs/codeprojecteval_suite_sizes.json), the official NL2Repo count of pytest cases.
Identical for every arm. Numbers and counts only.

    uv run python scripts/sealed/nl2_hard_score.py <run_dir> [--file-timeout 600]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard as G  # noqa: E402

_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")


def score(run_dir: Path, file_timeout: int) -> dict:
    from orchestra.sandbox.guard import run_confined

    task = run_dir.name
    sub = G.heldout_subdir_name(task)
    py = G.task_python(task)
    pinned = json.loads((ROOT / "configs" / "codeprojecteval_suite_sizes.json").read_text())["tasks"].get(task) or {}
    total = sum(v for k, v in pinned.items() if not k.startswith("Test:"))
    repo_src = next(iter(run_dir.glob("tasks/*/canonical/repo")))
    out = {"task": task, "tests_total": total, "passed": 0, "failed": 0, "error": 0, "skipped": 0, "files": {}}
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task
        shutil.copytree(repo_src, repo, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", sub))
        shutil.copytree(G.heldout_dir(task), repo / sub)
        env = {"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
               "PATH": f"{py.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
        for mod, n in sorted(pinned.items()):
            if mod.startswith("Test:") or not n:
                continue
            rel = mod.split(f"{sub}/", 1)[-1]
            hung = False
            try:
                p = run_confined([str(py), "-m", "pytest", f"{sub}/{rel}", "-q", "-rA", "--no-header", "-p", "no:cacheprovider",
                                  "-o", "addopts=", "--continue-on-collection-errors", "--timeout=30", "--timeout-method=signal"],
                                 cwd=repo, mode="scoring", role="nl2_hard_score", capture_output=True, text=True,
                                 timeout=file_timeout, check=False, env=env)
                stdout = p.stdout
            except subprocess.TimeoutExpired as exc:
                hung = True
                stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            c = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
            for line in stdout.splitlines():
                m = _LINE.match(line)
                if m and "::" in m.group(2):
                    k = {"PASSED": "passed", "XPASS": "passed", "FAILED": "failed", "ERROR": "error"}.get(m.group(1), "skipped")
                    c[k] += 1
            seen = sum(c.values())
            if seen < n:
                c["failed"] += n - seen            # not run: a module that did not import, or a hang
            for k in c:
                out[k] += c[k]
            out["files"][rel] = {**c, "pinned": n, "hung": hung}
    out["pass_rate"] = round(out["passed"] / total, 4) if total else None
    out["pass_at_1"] = bool(total and out["passed"] == total)
    out["hung_files"] = sum(1 for f in out["files"].values() if f["hung"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--file-timeout", type=int, default=600)
    a = ap.parse_args()
    for d in a.run_dirs:
        r = score(Path(d), a.file_timeout)
        text = json.dumps(r)
        G.assert_no_source_overlap(text, r["task"])
        (Path(d) / "heldout_score.json").write_text(json.dumps(r, indent=1))
        print(json.dumps({k: r[k] for k in ("task", "pass_rate", "passed", "failed", "error", "skipped", "tests_total", "pass_at_1", "hung_files")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
