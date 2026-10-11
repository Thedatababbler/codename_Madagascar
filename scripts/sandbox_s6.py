#!/usr/bin/env python3
"""Sandbox test S6 (sandbox spec A §4): real runs under the isolation. Spends quota, capped by this script.

Two jobs with ``ADAMAS_SANDBOX_ENFORCED=1``, both on cookiecutter
``template_source_resolution`` (earlier milestones from the stored 10-01 snapshot):
1. first run only;
2. repair: the stored first run is replayed, then the default repair R0 and the
   row the selection picks (``ADAMAS_PAIR_ONLY``).

Checks on the stored records:
- the agents finished their work;
- ``sandbox_manifest.json`` is complete;
- the turn items show no access outside the allowed set (refusals listed);
- no memory canary appears anywhere in the runs or workspaces.

Results: outputs/sandbox/s6/result.json.

    uv run python scripts/sandbox_s6.py run | check
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

OUT = ROOT / "outputs" / "sandbox" / "s6"
CONFIG = "configs/experiments/codeprojecteval_milestones_evolution.yaml"
DATASET = "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset"
CHECKPOINT = ROOT / "outputs/cpe_evolution/cpe-20261001T021833Z-cookiecutter/cookiecutter"
TASK, MID = "cookiecutter", "template_source_resolution"
EMPTY_TABLE = ROOT / "configs/playbook_v2/experiments/firstpass_from_repair_20261008/first_pass.empty.yaml"
JOBS = [("s6-fr", "first_run"), ("s6-rp", "repair")]
R0_ONLY = False
SEED = ""   # a stored first-stage workspace to replay (default: the checkpoint's workspaces/<milestone>/repo)


def predecessor() -> tuple[str, str]:
    canon = next(CHECKPOINT.glob("tasks/rb_*/canonical/repo"))
    order = [m["milestone_id"] for m in json.loads((CHECKPOINT / "milestone_plan_draft.json").read_text())["milestones"]]
    log = subprocess.run(["git", "-C", str(canon), "log", "--format=%H %s"], capture_output=True, text=True, check=True).stdout
    commits = {}
    for line in log.splitlines():
        sha, _, subj = line.partition(" ")
        if subj.startswith("canonical commit "):
            commits[subj.removeprefix("canonical commit ").strip()] = sha
        elif subj.strip() == "dataset inputs only":
            commits["__base__"] = sha
    idx = order.index(MID)
    # the plan's first milestone starts from the dataset-inputs commit
    return str(canon), commits[order[idx - 1]] if idx > 0 else commits["__base__"]


def job(jid: str, part: str) -> tuple[list[str], dict[str, str]]:
    repo, rev = predecessor()
    cmd = [str(ROOT / ".venv" / "bin" / "python"), "-m", "orchestra.cli.run_codeprojecteval_decomp", "--config", CONFIG,
           "--task-id", TASK, "--arm", "sandbox_s6", "--run-id", jid, "--output-root", str(OUT / "runs"), "--dataset-root", DATASET,
           "--only-milestone", MID, "--base-snapshot", f"{repo}@{rev}", "--inherit-harness", str(CHECKPOINT / "harness"),
           "--plan-file", str(ROOT / "configs/datasets/cpe_feature_plans" / f"{TASK}.plan.json")]
    env = {"ADAMAS_SANDBOX_ENFORCED": "1", "ADAMAS_FIRST_PASS_TABLE": str(EMPTY_TABLE), "ADAMAS_EVOLUTION_ROOT": str(OUT / "evolution")}
    if part == "first_run":
        env["ADAMAS_FIRST_RUN_ONLY"] = "1"
    else:
        env.update({"ADAMAS_SEED_FIRST_RUN": SEED or str(next(CHECKPOINT.glob(f"tasks/rb_*/workspaces/{MID}/repo"))),
                    "ADAMAS_SEED_MILESTONE": MID, "ADAMAS_PAIR_ONLY": "1"})
        if R0_ONLY:
            # a forced row id no table holds: the selection is empty ("not applicable"), so only R0 runs
            env["ADAMAS_FORCE_ROW"] = "__s6_r0_only__"
    return cmd, env


def run(cap: float) -> dict:
    from quota_spend import Spend

    OUT.mkdir(parents=True, exist_ok=True)
    spend = Spend()
    w0 = spend.w0
    procs = {}
    for jid, part in JOBS:
        cmd, env = job(jid, part)
        procs[jid] = subprocess.Popen(cmd, cwd=str(ROOT), env={**os.environ, **env}, stdout=open(OUT / f"{jid}.log", "w"),
                                      stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(20)
    stop = ""
    while any(p.poll() is None for p in procs.values()):
        time.sleep(60)
        if spend.poll() >= cap:
            stop = f"budget cap {cap} reached ({w0} -> {spend.last}, resets {spend.resets})"
            for p in procs.values():
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGTERM)
            break
    for p in procs.values():
        try:
            p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
    spend.poll()
    rec = {"window_start": w0, "window_end": spend.last, "resets": spend.resets, "points_used": spend.used(), "cap": cap, "stop": stop,
           "returncodes": {j: p.returncode for j, p in procs.items()}}
    (OUT / "spend.json").write_text(json.dumps(rec, indent=1))
    return rec


def check() -> dict:
    from orchestra.memory.runtime import canary_hits

    res: dict = {"jobs": {}}
    for jid, part in JOBS:
        batch = OUT / "runs" / jid
        rd = batch / TASK
        r: dict = {"part": part}
        man = rd / "sandbox_manifest.json"
        m = json.loads(man.read_text()) if man.is_file() else {}
        kinds = [e.get("kind") for e in m.get("entries") or []]
        r["manifest"] = {"present": man.is_file(), "scheme": m.get("scheme"), "landlock_abi": m.get("landlock_abi"),
                         "network": m.get("network"), "run_entry": "run" in kinds, "agent_entries": kinds.count("agent"),
                         "self_check": next((e.get("self_check") for e in m.get("entries") or [] if e.get("kind") == "run"), None)}
        te = next(rd.glob("tasks/*/task_execution.json"), None)
        st = json.loads(te.read_text()) if te else {}
        sub = (st.get("subtasks") or {}).get(MID) or {}
        fl = (st.get("fast_loop_states") or {}).get(MID) or {}
        r["milestone_status"] = sub.get("status")
        r["candidates"] = [(c.get("candidate_id"), c.get("status"), len(c.get("behaviour_failures") or []),
                            len(c.get("behaviour_passed") or [])) for c in fl.get("candidates") or []]
        finals = sorted(rd.glob("tasks/**/backend_traces/*/*.final.txt"))
        r["agent_calls_with_reply"] = len(finals)
        r["agent_calls_captured"] = len(sorted(rd.glob("tasks/**/backend_traces/*/*.prompt.txt")))
        from orchestra.sandbox.audit import audit_run as _audit

        refusals = []
        for f in [*sorted(rd.glob("tasks/*/backend_traces/*/*.items.json")),
                  *sorted(rd.glob("tasks/*/backend_traces/*/.sandbox/*/codex_home/sessions/**/*.jsonl"))]:
            refusals += re.findall(r"(adamas-netguard: [a-z]+ to a non-loopback address refused|Permission denied[^\"\\\\]{0,80})",
                                   f.read_text(errors="replace"))
        r["codex_sessions"] = len(sorted(rd.glob("tasks/*/backend_traces/*/.sandbox/*/codex_home/sessions/**/*.jsonl")))
        au = _audit(rd)
        n_cmd, findings = au["commands"], au["findings"]
        r["commands"] = n_cmd
        r["out_of_bounds_attempts"] = findings
        r["refusals_seen"] = sorted(set(refusals))[:20]
        r["canary_hits"] = canary_hits(batch)
        res["jobs"][jid] = r
    sp = OUT / "spend.json"
    res["spend"] = json.loads(sp.read_text()) if sp.is_file() else None
    (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["run", "check"])
    ap.add_argument("--cap", type=float, default=None, help="points for this attempt (default: sandbox.s6_budget_cap)")
    ap.add_argument("--task", default=None)
    ap.add_argument("--milestone", default=None)
    ap.add_argument("--checkpoint", default=None, help="<evolution run>/<task> directory holding the stored first run")
    ap.add_argument("--repair-only", action="store_true", help="run the repair job only, R0 alone (2026-10-11 retest)")
    ap.add_argument("--seed", default="", help="workspace to replay as the first stage; workspaces/<milestone>/repo holds the "
                    "committed code after the search, so a failing first stage needs a stored probe or candidate workspace")
    a = ap.parse_args()
    global TASK, MID, CHECKPOINT, JOBS, R0_ONLY, SEED
    SEED = str(Path(a.seed).resolve()) if a.seed else ""
    if a.task:
        TASK, MID, CHECKPOINT = a.task, a.milestone, Path(a.checkpoint).resolve()
    if a.repair_only:
        JOBS, R0_ONLY = [("s6-rp", "repair")], True
    from orchestra.sandbox.policy import load_config
    from orchestra.settings import load_env_file

    load_env_file(ROOT / ".env")
    if a.what == "run":
        cap = a.cap if a.cap is not None else float(load_config()["sandbox"].get("s6_budget_cap", 2))
        print(json.dumps(run(cap), indent=1))
    print(json.dumps(check(), indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
