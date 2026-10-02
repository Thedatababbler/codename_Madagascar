#!/usr/bin/env python3
"""Memory replay for the test author (slow loop, self-evolution spec §8.4 / §9.5).

Scores an authored suite against the held-out cases attributed to the same
milestone on every retained candidate workspace of a run, without any
agent call. For each workspace the suite verdict (all pass / fails) and the
held-out subset verdict (all pass / fails) make one cell of the 2x2 table
the slow loop reads:

    suite pass & held-out fail   = miss      (the author did not cover it)
    suite fail & held-out pass   = false alarm (the suite asks for something the task does not)

Two suites scored on the same workspaces are comparable suite to suite:
that is how a new author prompt is evaluated before it earns a single
implementer run. Training tasks only; the held-out outcomes are read as
names and pass/fail, never as source.

    uv run python scripts/author_replay.py score --run outputs/cpe_evolution/<batch>/<task> \\
        --milestone <mid> [--suite <dir>] --attribution outputs/evolution/sealed/attribution/<task>.json --out <json>
    uv run python scripts/author_replay.py summary <json> [...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from orchestra.codeprojecteval.dataset import load_task
from orchestra.control.evolution.ledger import split_of
from orchestra.control.fast_loop.persistence import failure_key
from orchestra.control.fast_loop.prior_suites import PriorSuite, gate_command_of, rewrite_command
from orchestra.harness.progress import behaviour_failures, behaviour_passed, parse_progress
from orchestra.ir.graph import OrchestraGraph

DATASET_ROOT = Path(os.environ.get("CPE_DATASET_ROOT") or "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
ENV_ROOT = Path(os.environ.get("CPE_ENV_ROOT") or "/root/codex-benchmarks/cpe_envs")
_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")
SKIP_CANDIDATES = {"node_resample_blame", "persistent_blame"}


def _arg(command: list[str], flag: str) -> str | None:
    if flag in command:
        i = command.index(flag)
        if i + 1 < len(command):
            return command[i + 1]
    return None


def gate_for(run_dir: Path, milestone: str) -> tuple[list[str], float]:
    graph_path = run_dir / "logs" / "01_subgraphs" / milestone / "graph.json"
    graph = OrchestraGraph.model_validate(json.loads(graph_path.read_text(encoding="utf-8")))
    found = gate_command_of(graph)
    if found is None:
        raise SystemExit(f"no gate command in {graph_path}")
    return found


def workspaces_of(run_dir: Path, milestone: str) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for sub in run_dir.glob("tasks/rb_*/"):
        first = sub / "workspaces" / milestone / "repo"
        if first.is_dir():
            out["first_pass"] = first
        for cand in sorted((sub / "subtasks" / milestone / "candidates").glob("*/repo")):
            name = cand.parent.name
            if name not in SKIP_CANDIDATES:
                out[name] = cand
    return out


def suite_cases(workspace: Path, command: list[str], suite_dir: Path, timeout: float) -> dict[str, str]:
    """The gate script on a workspace with ``suite_dir`` as its frozen suite (analysis, not custody)."""
    contracts = _arg(command, "--contracts") or ""
    level = _arg(command, "--level") or "implementation"
    prior = PriorSuite(milestone_id="", spec_dir=str(Path(suite_dir).resolve()), contracts=contracts, level=level,
                       committed_passed=frozenset(), committed_failed=frozenset())
    cmd = rewrite_command(command, prior)
    proc = subprocess.run(cmd, cwd=str(workspace), capture_output=True, text=True, timeout=timeout, check=False,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    stdout = proc.stdout
    _score, stages, _furthest = parse_progress(stdout)
    if not stages:
        print(f"    (suite run gave no progress line; rc={proc.returncode}; stderr: {proc.stderr.strip()[-200:]})")
    out: dict[str, str] = {}
    for n in behaviour_passed(stages):
        out[failure_key(n)] = "pass"
    for n in behaviour_failures(stages):
        out[failure_key(n)] = "fail"
    return out


def heldout_cases(task_id: str, workspace: Path, cases: list[str], *, per_test_timeout: int = 30) -> dict[str, str]:
    task = load_task(task_id, dataset_root=DATASET_ROOT)
    python = ENV_ROOT / task_id / "bin" / "python"
    if not cases or not python.is_file():
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task_id
        shutil.copytree(workspace, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "spec_tests",
                                                                        "repair_evidence", "unit_tests", "check_tests"))
        shutil.copytree(task.repo_root / task.unit_tests, repo / task.unit_tests)
        node_ids = [f"{task.unit_tests}/{c}" for c in cases]
        proc = subprocess.run(
            [str(python), "-m", "pytest", *node_ids, "-q", "-rA", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
             "--continue-on-collection-errors", f"--timeout={per_test_timeout}", "--timeout-method=signal"],
            cwd=repo, capture_output=True, text=True, timeout=3600, check=False,
            env={"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
                 "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"},
        )
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        m = _LINE.match(line)
        if m:
            out[m.group(2)] = "pass" if m.group(1) in ("PASSED", "XPASS") else "fail"
    for c in cases:  # cases pytest never reported (collection error) count as failed
        key = f"{task.unit_tests}/{c}"
        if not any(k == key or k.startswith(key + "[") for k in out):
            out[key] = "fail"
    return out


def cell(suite: dict[str, str], heldout: dict[str, str]) -> str:
    s_fail = any(v == "fail" for v in suite.values())
    h_fail = any(v == "fail" for v in heldout.values())
    if not suite:
        return "suite_unscored"
    if not heldout:
        return "heldout_unscored"
    return {(False, False): "agree_pass", (True, True): "agree_fail", (False, True): "miss", (True, False): "false_alarm"}[(s_fail, h_fail)]


def score(args) -> None:
    run_dir = Path(args.run)
    task_id = run_dir.name
    if split_of(task_id) != "train":
        raise SystemExit(f"{task_id} is not a training task")
    command, timeout = gate_for(run_dir, args.milestone)
    suite_dir = Path(args.suite) if args.suite else Path(_arg(command, "--spec-tests") or "")
    if not suite_dir.is_dir():
        raise SystemExit(f"suite dir missing: {suite_dir}")
    attribution = json.loads(Path(args.attribution).read_text(encoding="utf-8")).get("attribution") or {}
    cases = list(attribution.get(args.milestone) or [])
    rows = []
    for name, ws in workspaces_of(run_dir, args.milestone).items():
        suite = suite_cases(ws, command, suite_dir, timeout)
        held = heldout_cases(task_id, ws, cases)
        rows.append({
            "workspace": name, "path": str(ws), "cell": cell(suite, held),
            "suite_failed": sorted(k for k, v in suite.items() if v == "fail"), "suite_total": len(suite),
            "heldout_failed": sorted(k for k, v in held.items() if v == "fail"), "heldout_total": len(held),
        })
        print(f"{task_id} {args.milestone[:30]} {name:22s} suite {len(rows[-1]['suite_failed'])}/{len(suite)} failed | held-out {len(rows[-1]['heldout_failed'])}/{len(held)} failed -> {rows[-1]['cell']}")
    out = {"task": task_id, "milestone": args.milestone, "run": str(run_dir), "suite": str(suite_dir), "suite_label": args.label,
           "attributed_cases": len(cases), "workspaces": rows}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")


def summary(args) -> None:
    by_label: dict[str, Counter] = defaultdict(Counter)
    uncovered: dict[str, Counter] = defaultdict(Counter)
    for f in args.files:
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        label = d.get("suite_label") or "suite"
        for w in d["workspaces"]:
            by_label[label][w["cell"]] += 1
            if w["cell"] == "miss":
                for c in w["heldout_failed"]:
                    uncovered[label][f"{d['task']}:{d['milestone']}:{c.split('::', 1)[-1][:60]}"] += 1
    for label, c in by_label.items():
        scored = sum(v for k, v in c.items() if k in ("agree_pass", "agree_fail", "miss", "false_alarm"))
        h_fail = c["agree_fail"] + c["miss"]
        s_fail = c["agree_fail"] + c["false_alarm"]
        print(f"\n[{label}] workspaces scored {scored}: agree_pass {c['agree_pass']}, agree_fail {c['agree_fail']}, miss {c['miss']}, false_alarm {c['false_alarm']}"
              f" (unscored: suite {c['suite_unscored']}, held-out {c['heldout_unscored']})")
        if h_fail:
            print(f"  miss rate (suite all-pass | held-out fails):       {c['miss'] / h_fail:.2f}  ({c['miss']}/{h_fail})")
        if s_fail:
            print(f"  false-alarm rate (held-out all-pass | suite fails): {c['false_alarm'] / s_fail:.2f}  ({c['false_alarm']}/{s_fail})")
        if uncovered[label]:
            print("  held-out cases failing where the suite passes everything (top 12):")
            for k, n in uncovered[label].most_common(12):
                print(f"    {n:2d}x {k}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--run", required=True)
    s.add_argument("--milestone", required=True)
    s.add_argument("--suite", default=None, help="suite directory (default: the run's frozen suite)")
    s.add_argument("--label", default="current")
    s.add_argument("--attribution", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=score)
    m = sub.add_parser("summary")
    m.add_argument("files", nargs="+")
    m.set_defaults(fn=summary)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
