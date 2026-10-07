#!/usr/bin/env python3
"""Joint first-pass / playbook experiment driver (user's spec of 2026-10-07).

    uv run python scripts/joint_experiment.py plan --round 1
    uv run python scripts/joint_experiment.py prepare --round 1        # inventories + verification suites (small spend)
    uv run python scripts/joint_experiment.py run --round 1 --part first_run [--only-first 2]
    uv run python scripts/joint_experiment.py run --round 1 --part repair
    uv run python scripts/sealed/joint_eval.py --round 1               # gate / verification / held-out per run (sealed)
    uv run python scripts/joint_experiment.py dry-run-guard            # the budget guard stops (no model call)

Every run is one CLI invocation of a single milestone from its predecessor snapshot, inheriting the
original run's frozen suites (the author is dropped, so F0 and F7 are graded by the same suite):

* first-run jobs: ``ADAMAS_FIRST_RUN_ONLY=1`` (the milestone ends at its first gate), F0 or F7 forced
  from the experiment's first-pass table;
* repair jobs: the first stage replays the stored first run (``ADAMAS_SEED_FIRST_RUN``), then the
  search runs R0 and the forced row only (``ADAMAS_PAIR_ONLY``, ``ADAMAS_FORCE_ROW``) on that incumbent.

The budget guard reads the account's window before every launch and every 30 seconds while runs are
alive; at the round cap or the total cap it stops launching and terminates the live runs (their
process groups). Finished results stay on disk; unfinished jobs are listed in the job file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXP = "joint_20261007"
NS = ROOT / "configs" / "playbook_v2" / "experiments" / EXP
OUT = ROOT / "outputs" / EXP
CONFIG = "configs/experiments/codeprojecteval_milestones_evolution.yaml"
DATASET_ROOT = "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset"
QUOTA = "/root/cli-proxy/quota.sh"
ROUND_CAP = 10.0
TOTAL_CAP = 30.0

RUNS = {  # the original runs whose snapshots, frozen suites and first runs the checkpoints come from
    "voluptuous": "outputs/cpe_evolution/cpe-20261002T071738Z-voluptuous/voluptuous",
    "python-hl7": "outputs/cpe_evolution/cpe-20261001T092429Z-python-hl7/python-hl7",
    "flask": "outputs/cpe_evolution/cpe-20261002T030913Z-flask/flask",
}
ROUND1 = {
    # first-run pairs, in sequential order (the first two decide whether to go on)
    "first_run": [("voluptuous", "errors_markers_contracts"), ("python-hl7", "hierarchical_containers"),
                  ("flask", "core_contracts_and_context_primitives"), ("voluptuous", "schema_compiler_core")],
    "first_run_entries": ["F0", "F7"],
    "first_run_reps": 2,
    # repair pairs: the first half only in round 1
    "repair": [("voluptuous", "errors_markers_contracts"), ("flask", "cli_public_api_and_full_integration")],
    "repair_row": "E3-S1",
    "repair_reps": 2,
}


@dataclass
class Job:
    job_id: str
    part: str            # first_run | repair
    task: str
    milestone: str
    variant: str         # F0 / F7 / E3-S1
    rep: int
    env: dict = field(default_factory=dict)
    cmd: list = field(default_factory=list)
    status: str = "pending"   # pending | running | done | failed | stopped_by_budget | skipped
    run_dir: str = ""
    started: str = ""
    finished: str = ""
    returncode: int | None = None


# --- checkpoints ---------------------------------------------------------------------


def predecessor(task: str, milestone: str) -> tuple[str, str]:
    """(canonical repo, revision the milestone started from): the previous milestone's canonical commit."""
    run = ROOT / RUNS[task]
    canon = next(run.glob("tasks/rb_*/canonical/repo"))
    plan = json.loads((run / "milestone_plan_draft.json").read_text(encoding="utf-8"))
    order = [m["milestone_id"] for m in plan["milestones"]]
    log = subprocess.run(["git", "-C", str(canon), "log", "--format=%H %s"], capture_output=True, text=True, check=True).stdout
    commits = {}
    base = ""
    for line in log.splitlines():
        sha, _, subj = line.partition(" ")
        if subj.startswith("canonical commit "):
            commits[subj.removeprefix("canonical commit ").strip()] = sha
        elif "dataset inputs" in subj:
            base = sha
    i = order.index(milestone)
    rev = base if i == 0 else commits[order[i - 1]]
    return str(canon), rev


def first_run_workspace(task: str, milestone: str) -> str:
    run = ROOT / RUNS[task]
    return str(next(run.glob(f"tasks/rb_*/workspaces/{milestone}/repo")))


def base_cmd(task: str, milestone: str, run_id: str) -> list[str]:
    run = ROOT / RUNS[task]
    repo, rev = predecessor(task, milestone)
    return [str(ROOT / ".venv" / "bin" / "python"), "-m", "orchestra.cli.run_codeprojecteval_decomp",
            "--config", CONFIG, "--task-id", task, "--arm", "joint", "--run-id", run_id,
            "--output-root", str(OUT / "runs"), "--dataset-root", DATASET_ROOT,
            "--only-milestone", milestone, "--base-snapshot", f"{repo}@{rev}",
            "--inherit-harness", str(run / "harness"),
            # the planner's own plan: a run's saved draft already carries the first-pass design it applied
            # (flask's core milestone has F1 baked in), which would make "F0" something else
            "--plan-file", str(ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{task}.plan.json")]


def common_env() -> dict:
    return {"ADAMAS_EVOLUTION_ROOT": str(OUT / "evolution"),        # an isolated ledger
            "ADAMAS_FIRST_PASS_TABLE": str(NS / "first_pass.yaml"),
            "ADAMAS_FP_INVENTORY_DIR": str(OUT / "inventory")}


def plan(args) -> None:
    rnd = args.round
    jobs: list[Job] = []
    for task, mid in ROUND1["first_run"]:
        for variant in ROUND1["first_run_entries"]:
            for rep in range(ROUND1["first_run_reps"]):
                jid = f"r{rnd}-fr-{task}-{mid}-{variant}-{rep}"
                env = {**common_env(), "ADAMAS_FIRST_RUN_ONLY": "1", "ADAMAS_FORCE_F": f"{mid}={variant}"}
                jobs.append(Job(jid, "first_run", task, mid, variant, rep, env, base_cmd(task, mid, jid)))
    for task, mid in ROUND1["repair"]:
        for rep in range(ROUND1["repair_reps"]):
            jid = f"r{rnd}-rp-{task}-{mid}-{ROUND1['repair_row']}-{rep}"
            env = {**common_env(), "ADAMAS_FORCE_F": f"{mid}=F0", "ADAMAS_SEED_FIRST_RUN": first_run_workspace(task, mid),
                   "ADAMAS_SEED_MILESTONE": mid, "ADAMAS_PAIR_ONLY": "1", "ADAMAS_FORCE_ROW": ROUND1["repair_row"]}
            jobs.append(Job(jid, "repair", task, mid, ROUND1["repair_row"], rep, env, base_cmd(task, mid, jid)))
    path = OUT / f"round{rnd}" / "jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(j) for j in jobs], indent=1), encoding="utf-8")
    print(f"{len(jobs)} jobs -> {path}")
    for j in jobs:
        print(f"  {j.job_id}")


# --- budget guard ------------------------------------------------------------------------


def read_window() -> float | None:
    """Percent used of the gpt-5.5 window that moves with usage (quota.sh's first window line)."""
    try:
        out = subprocess.run([QUOTA], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    block = out.split("gpt-5.5", 1)[-1] if "gpt-5.5" in out else ""
    m = re.search(r"window\s*:\s*(\d+(?:\.\d+)?)% used", block)
    return float(m.group(1)) if m else None


class BudgetGuard:
    """Stops at ``round_cap`` points used since the round started or ``total_cap`` since the experiment did."""

    def __init__(self, reader, *, round_start: float, total_start: float, round_cap: float, total_cap: float):
        self.reader, self.round_start, self.total_start = reader, round_start, total_start
        self.round_cap, self.total_cap = round_cap, total_cap
        self.last: float | None = None

    def exceeded(self) -> str:
        v = self.reader()
        if v is None:
            return ""
        self.last = v
        if v - self.round_start >= self.round_cap:
            return f"round cap: {v - self.round_start:.1f} >= {self.round_cap} points"
        if v - self.total_start >= self.total_cap:
            return f"total cap: {v - self.total_start:.1f} >= {self.total_cap} points"
        return ""


def _budget_state() -> dict:
    p = OUT / "budget.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def _save_budget(d: dict) -> None:
    (OUT / "budget.json").parent.mkdir(parents=True, exist_ok=True)
    (OUT / "budget.json").write_text(json.dumps(d, indent=1))


def execute(jobs: list[Job], *, guard: BudgetGuard, concurrency: int, save, poll: float = 30.0, stagger: float = 60.0) -> str:
    """Run pending jobs ``concurrency`` at a time; on a cap, terminate live ones. Returns the stop reason or ''."""
    live: dict[str, subprocess.Popen] = {}
    by_id = {j.job_id: j for j in jobs}
    reason = ""
    while True:
        for jid, proc in list(live.items()):
            rc = proc.poll()
            if rc is not None:
                j = by_id[jid]
                j.status, j.returncode, j.finished = ("done" if rc == 0 else "failed"), rc, datetime.now(UTC).isoformat()
                del live[jid]
                save()
        reason = reason or guard.exceeded()
        if reason:
            for jid, proc in live.items():
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                by_id[jid].status = "stopped_by_budget"
                by_id[jid].finished = datetime.now(UTC).isoformat()
            for j in jobs:
                if j.status == "pending":
                    j.status = "stopped_by_budget"
            save()
            return reason
        pending = [j for j in jobs if j.status == "pending"]
        if not pending and not live:
            return ""
        if pending and len(live) < concurrency:
            j = pending[0]
            log = OUT / "logs" / f"{j.job_id}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, **j.env}
            fh = open(log, "w")
            live[j.job_id] = subprocess.Popen(j.cmd, env=env, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                                              start_new_session=True)
            j.status, j.started = "running", datetime.now(UTC).isoformat()
            j.run_dir = str(OUT / "runs" / j.job_id / j.task)
            save()
            time.sleep(stagger)   # codex keeps a sqlite state file: simultaneous starts collide
            continue
        time.sleep(poll)


def run(args) -> None:
    path = OUT / f"round{args.round}" / "jobs.json"
    jobs = [Job(**d) for d in json.loads(path.read_text())]
    sel = [j for j in jobs if j.part == args.part and j.status in ("pending", "stopped_by_budget")]
    if args.only_first:
        keep = []
        for j in sel:
            if (j.task, j.milestone) not in keep:
                keep.append((j.task, j.milestone))
        allowed = set(keep[: args.only_first])
        sel = [j for j in sel if (j.task, j.milestone) in allowed]
    for j in sel:
        j.status = "pending"
    b = _budget_state()
    now = read_window()
    if now is None:
        raise SystemExit("cannot read the account window; refusing to spend without the guard")
    b.setdefault("experiment_start", now)
    b.setdefault(f"round{args.round}_start", now)
    _save_budget(b)
    guard = BudgetGuard(read_window, round_start=b[f"round{args.round}_start"], total_start=b["experiment_start"],
                        round_cap=args.round_cap, total_cap=args.total_cap)

    def save():
        path.write_text(json.dumps([asdict(j) for j in jobs], indent=1))

    print(f"{len(sel)} job(s), window now {now}%, round started at {b[f'round{args.round}_start']}%, caps {args.round_cap}/{args.total_cap}", flush=True)
    reason = execute(sel, guard=guard, concurrency=args.jobs, save=save)
    b[f"round{args.round}_last"] = guard.last
    _save_budget(b)
    print("stopped: " + reason if reason else "all jobs finished", flush=True)
    for j in sel:
        print(f"  {j.status:18s} {j.job_id}")


def dry_run_guard(args) -> None:
    """A real execute() with harmless jobs and a reader that climbs: it must stop and say so."""
    readings = iter([0.0, 0.0, 0.4, 0.9, 1.6, 2.5, 3.0, 3.0, 3.0])
    reader = lambda: next(readings, 3.0)  # noqa: E731
    jobs = [Job(f"dry-{i}", "first_run", "t", "m", "F0", i, {}, ["sleep", "30"]) for i in range(4)]
    guard = BudgetGuard(reader, round_start=0.0, total_start=0.0, round_cap=1.0, total_cap=30.0)
    reason = execute(jobs, guard=guard, concurrency=2, save=lambda: None, poll=1.0, stagger=0.5)
    print("stop reason:", reason or "(none)")
    for j in jobs:
        print(f"  {j.job_id}: {j.status}")
    assert reason.startswith("round cap") and all(j.status in ("stopped_by_budget", "done") for j in jobs)
    assert any(j.status == "stopped_by_budget" for j in jobs)


VERIFIER_NEEDED = [("voluptuous", "errors_markers_contracts"), ("voluptuous", "schema_compiler_core"),
                   ("python-hl7", "hierarchical_containers")]
FOUNDATIONS = [("voluptuous", "errors_markers_contracts"), ("voluptuous", "schema_compiler_core"),
               ("python-hl7", "hierarchical_containers"), ("flask", "core_contracts_and_context_primitives")]


def prepare(args) -> None:
    """Behaviour inventories for the F7 checkpoints and v10.1 verification suites where none exists."""
    from orchestra.codeprojecteval.behaviour_inventory import extract_inventory
    from orchestra.codeprojecteval.public_symbols import load_docs

    os.environ.setdefault("ADAMAS_AUTHOR_INVENTORY_CACHE", str(ROOT / "outputs" / "author_eval" / "inventory_cache"))
    for task, mid in FOUNDATIONS:
        plan = json.loads((ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{task}.plan.json").read_text())
        m = next(x for x in plan["milestones"] if x["milestone_id"] == mid)
        inv = extract_inventory(task=task, milestone_id=mid, objective=m.get("objective", ""),
                                criteria=(m.get("acceptance") or {}).get("criteria") or [], focus_paths=m.get("focus_paths") or [],
                                docs=load_docs(Path(DATASET_ROOT) / task / "docs"), out_dir=OUT / "inventory")
        kinds = {k: sum(1 for i in inv.items if i.kind == k) for k in ("main_path", "boundary", "error_path")}
        print(f"inventory {task}:{mid} items {len(inv.items)} (F7 kinds {kinds}) dropped {len(inv.dropped)}")
    sel = OUT / "verifier_select.txt"
    sel.write_text("\n".join(f"{t}:{m}" for t, m in VERIFIER_NEEDED) + "\n")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "author_eval.py"), "generate", "--label", "V", "--group", "A",
                    "--sample", "0", "--select-file", str(sel), "--jobs", "2"], cwd=ROOT, check=False)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--round", type=int, default=1)
    p.set_defaults(fn=plan)
    r = sub.add_parser("run")
    r.add_argument("--round", type=int, default=1)
    r.add_argument("--part", choices=["first_run", "repair"], required=True)
    r.add_argument("--only-first", type=int, default=0, help="only the first N checkpoints of this part")
    r.add_argument("--jobs", type=int, default=2)
    r.add_argument("--round-cap", type=float, default=ROUND_CAP)
    r.add_argument("--total-cap", type=float, default=TOTAL_CAP)
    r.set_defaults(fn=run)
    pr = sub.add_parser("prepare")
    pr.add_argument("--round", type=int, default=1)
    pr.set_defaults(fn=prepare)
    d = sub.add_parser("dry-run-guard")
    d.set_defaults(fn=dry_run_guard)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
