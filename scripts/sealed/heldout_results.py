#!/usr/bin/env python3
"""Per-case held-out results of every retained candidate workspace, computed once
and cached (author-evolution spec §5 item 1). Training tasks only.

For a workspace the attributed held-out cases of its milestone are run on a
scratch copy (the dataset's held-out directory copied in, the task's own
interpreter), and ``{case_id: pass|fail}`` is written to
``outputs/evolution/sealed/heldout_results/<task>/<key>.json`` keyed by the
workspace path. Memory replay reads the cache and never runs held-out again.
Output is case ids and pass/fail only.

    uv run python scripts/sealed/heldout_results.py --all                 # every ledger workspace + first pass + final repos
    uv run python scripts/sealed/heldout_results.py --workspace P --task T --milestone M
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import assert_no_source_overlap, heldout_dir, heldout_subdir_name, sealed_case_id, task_python  # noqa: E402

CACHE_ROOT = Path(os.environ.get("ADAMAS_EVOLUTION_ROOT") or "outputs/evolution") / "sealed" / "heldout_results"
ATTRIBUTION_ROOT = Path(os.environ.get("ADAMAS_EVOLUTION_ROOT") or "outputs/evolution") / "sealed" / "attribution"
_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")
_IGNORE = ("__pycache__", ".pytest_cache", ".git", "spec_tests", "spec_tests_soft", "repair_evidence", "unit_tests", "check_tests",
           "bin", "lib", "lib64", "include", "share", "pyvenv.cfg", ".venv", "venv")


def cache_key(workspace: str | Path) -> str:
    return hashlib.sha1(str(Path(workspace).resolve()).rstrip("/").encode()).hexdigest()[:16]


def cache_path(task: str, workspace: str | Path) -> Path:
    return CACHE_ROOT / task / f"{cache_key(workspace)}.json"


def attributed_cases(task: str, milestone: str) -> list[str]:
    p = ATTRIBUTION_ROOT / f"{task}.json"
    if not p.is_file():
        return []
    return list((json.loads(p.read_text(encoding="utf-8")).get("attribution") or {}).get(milestone) or [])


def run_heldout(task: str, workspace: Path, cases: list[str], *, per_test_timeout: int = 30) -> dict[str, str]:
    """``{node_id: pass|fail}`` for the attributed cases on ``workspace``. Sealed: reads the held-out directory."""
    python = task_python(task)
    if not cases or not python.is_file() or not workspace.is_dir():
        return {}
    sub = heldout_subdir_name(task)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task
        shutil.copytree(workspace, repo, symlinks=True, ignore=shutil.ignore_patterns(*_IGNORE))
        shutil.copytree(heldout_dir(task), repo / sub)
        files = sorted({f"{sub}/{c.split('::', 1)[0]}" for c in cases})
        wanted = set(cases)
        proc = subprocess.run(
            [str(python), "-m", "pytest", *files, "-q", "-rA", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
             "--continue-on-collection-errors", f"--timeout={per_test_timeout}", "--timeout-method=signal"],
            cwd=repo, capture_output=True, text=True, timeout=3600, check=False,
            env={"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
                 "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"},
        )
    out: dict[str, str] = {}
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        node = m.group(2)
        parts = node.split("::")
        key = f"{parts[0].split('/', 1)[-1]}::{parts[-1].split('[', 1)[0]}" if len(parts) >= 2 else node
        if key in wanted:
            seen.add(key)
            out[sealed_case_id(node)] = "pass" if m.group(1) in ("PASSED", "XPASS") else "fail"
    for c in wanted - seen:
        out[f"{sub}/{c}"] = "fail"
    return out


def results_for(task: str, milestone: str, workspace: str | Path, *, refresh: bool = False) -> dict:
    """Cached per-case results; computed on a miss. Safe to call from anywhere (it only reads the cache
    when the entry exists; a miss runs the held-out suite, which the guard allows here)."""
    p = cache_path(task, workspace)
    if p.is_file() and not refresh:
        return json.loads(p.read_text(encoding="utf-8"))
    cases = attributed_cases(task, milestone)
    t0 = time.time()
    res = run_heldout(task, Path(workspace), cases)
    rec = {"task": task, "milestone": milestone, "workspace_ref": str(Path(workspace).resolve()), "attributed": len(cases),
           "cases": dict(sorted(res.items())), "passed": sum(1 for v in res.values() if v == "pass"),
           "failed": sum(1 for v in res.values() if v == "fail"), "seconds": round(time.time() - t0, 1)}
    text = json.dumps(rec, indent=1)
    assert_no_source_overlap(json.dumps({k: v for k, v in rec.items() if k != "workspace_ref"}), task)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rec


def _ledger_workspaces(ledger_root: Path | None):
    from orchestra.control.evolution.ledger import ledger_root_default, read_jsonl, split_of
    root = ledger_root or ledger_root_default()
    seen = set()
    for f in sorted(root.glob("*/candidates.jsonl")):
        for r in read_jsonl(f):
            ws = str(r.get("workspace_ref") or "")
            task = str(r.get("task_id") or "").removeprefix("rb_")
            if not ws or not task or split_of(str(r.get("task_id"))) != "train" or ws in seen:
                continue
            seen.add(ws)
            yield task, str(r.get("milestone_id")), ws
    # the committed final repository of every run, for every milestone of its task's plan
    runs: dict[str, str] = {}
    for f in sorted(root.glob("*/candidates.jsonl")):
        for r in read_jsonl(f):
            rd = str(r.get("run_dir") or "")
            if rd and split_of(str(r.get("task_id"))) == "train":
                runs.setdefault(rd, str(r.get("task_id") or "").removeprefix("rb_"))
    plans = ROOT / "configs" / "datasets" / "cpe_feature_plans"
    for rd, task in sorted(runs.items()):
        plan = plans / f"{task}.plan.json"
        if not plan.is_file():
            continue
        mids = [m["milestone_id"] for m in json.loads(plan.read_text(encoding="utf-8"))["milestones"]]
        for fin in sorted(Path(rd).glob("tasks/*/canonical/repo")):
            for mid in mids:
                key = f"{fin}#{mid}"
                if key in seen:
                    continue
                seen.add(key)
                yield task, mid, str(fin)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ledger", type=Path, default=None)
    ap.add_argument("--workspace", type=Path)
    ap.add_argument("--task")
    ap.add_argument("--milestone")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--shard", default="0/1", help="i/n: process every n-th job starting at i")
    args = ap.parse_args()
    jobs = []
    if args.all:
        jobs = list(_ledger_workspaces(args.ledger))
    elif args.workspace and args.task and args.milestone:
        jobs = [(args.task, args.milestone, str(args.workspace))]
    else:
        ap.error("--all or --workspace/--task/--milestone")
    si, sn = (int(x) for x in args.shard.split("/"))
    jobs = jobs[si::sn]
    done = 0
    for task, mid, ws in jobs:
        # the final repository serves every milestone: key the cache by workspace + milestone
        key_ws = ws if not Path(ws).match("tasks/*/canonical/repo") else f"{ws}#{mid}"
        p = cache_path(task, key_ws)
        if p.is_file() and not args.refresh:
            rec = json.loads(p.read_text(encoding="utf-8"))
            print(f"cached  {task:14s} {mid[:34]:34s} {rec['passed']:3d}/{rec['attributed']:3d} pass")
            continue
        cases = attributed_cases(task, mid)
        t0 = time.time()
        res = run_heldout(task, Path(ws), cases)
        rec = {"task": task, "milestone": mid, "workspace_ref": ws, "attributed": len(cases), "cases": dict(sorted(res.items())),
               "passed": sum(1 for v in res.values() if v == "pass"), "failed": sum(1 for v in res.values() if v == "fail"),
               "seconds": round(time.time() - t0, 1)}
        text = json.dumps(rec, indent=1)
        assert_no_source_overlap(json.dumps({k: v for k, v in rec.items() if k != "workspace_ref"}), task)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        done += 1
        print(f"scored  {task:14s} {mid[:34]:34s} {rec['passed']:3d}/{rec['attributed']:3d} pass  ({rec['seconds']}s)  {ws[-60:]}")
    print(f"{done} computed, {len(jobs) - done} from cache")


if __name__ == "__main__":
    main()
