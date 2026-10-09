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


def predecessor() -> tuple[str, str]:
    canon = next(CHECKPOINT.glob("tasks/rb_*/canonical/repo"))
    order = [m["milestone_id"] for m in json.loads((CHECKPOINT / "milestone_plan_draft.json").read_text())["milestones"]]
    log = subprocess.run(["git", "-C", str(canon), "log", "--format=%H %s"], capture_output=True, text=True, check=True).stdout
    commits = {}
    for line in log.splitlines():
        sha, _, subj = line.partition(" ")
        if subj.startswith("canonical commit "):
            commits[subj.removeprefix("canonical commit ").strip()] = sha
    return str(canon), commits[order[order.index(MID) - 1]]


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
        env.update({"ADAMAS_SEED_FIRST_RUN": str(next(CHECKPOINT.glob(f"tasks/rb_*/workspaces/{MID}/repo"))),
                    "ADAMAS_SEED_MILESTONE": MID, "ADAMAS_PAIR_ONLY": "1"})
    return cmd, env


def run(cap: float) -> dict:
    from joint_experiment import read_window

    OUT.mkdir(parents=True, exist_ok=True)
    w0 = read_window()
    if w0 is None:
        raise SystemExit("cannot read the quota window; refusing to spend")
    procs = {}
    for jid, part in JOBS:
        cmd, env = job(jid, part)
        procs[jid] = subprocess.Popen(cmd, cwd=str(ROOT), env={**os.environ, **env}, stdout=open(OUT / f"{jid}.log", "w"),
                                      stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(20)
    stop, last = "", w0
    while any(p.poll() is None for p in procs.values()):
        time.sleep(60)
        w = read_window()
        last = w if w is not None else last
        if last - w0 >= cap:
            stop = f"budget cap {cap} reached ({w0} -> {last})"
            for p in procs.values():
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGTERM)
            break
    for p in procs.values():
        try:
            p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
    w1 = read_window() or last
    rec = {"window_start": w0, "window_end": w1, "points_used": round(w1 - w0, 2), "cap": cap, "stop": stop,
           "returncodes": {j: p.returncode for j, p in procs.items()}}
    (OUT / "spend.json").write_text(json.dumps(rec, indent=1))
    return rec


def check() -> dict:
    from orchestra.memory.runtime import canary_hits
    from sandbox_tests import _FORBIDDEN, _EXTERNAL, _INSTALL, _commands

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
        findings, refusals, n_cmd = [], [], 0
        sessions = sorted(rd.glob("tasks/*/backend_traces/*/.sandbox/*/codex_home/sessions/**/*.jsonl"))
        r["codex_sessions"] = len(sessions)
        for f in [*sorted(rd.glob("tasks/*/backend_traces/*/*.items.json")), *sessions]:
            text = f.read_text(errors="replace")
            refusals += re.findall(r"(adamas-netguard: [a-z]+ to a non-loopback address refused|Permission denied[^\"\\\\]{0,80})", text)
            if f.suffix == ".jsonl":
                cmds = []
                for line in text.splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    pl = e.get("payload") or {}
                    if pl.get("type") == "function_call":
                        try:
                            args = json.loads(pl.get("arguments") or "{}")
                        except ValueError:
                            args = {"cmd": pl.get("arguments")}
                        c = args.get("cmd") or args.get("command") or args.get("input") or ""
                        cmds.append(" ".join(map(str, c)) if isinstance(c, list) else str(c))
            else:
                cmds = _commands(json.loads(text))
            for c in cmds:
                n_cmd += 1
                bad = [k for k, p in _FORBIDDEN.items() if p in c]
                if _EXTERNAL.search(c):
                    bad.append("external_network")
                if _INSTALL.search(c):
                    bad.append("package_install")
                outs = [x for x in re.findall(re.escape(str(ROOT / "outputs")) + r"/[^\s'\":]+", c) if not x.startswith(str(batch))]
                if outs:
                    bad.append("other_run_outputs")
                if bad:
                    findings.append({"kinds": bad, "command": c[:240]})
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
    a = ap.parse_args()
    from orchestra.sandbox.policy import load_config
    from orchestra.settings import load_env_file

    load_env_file(ROOT / ".env")
    if a.what == "run":
        print(json.dumps(run(float(load_config()["sandbox"].get("s6_budget_cap", 2))), indent=1))
    print(json.dumps(check(), indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
