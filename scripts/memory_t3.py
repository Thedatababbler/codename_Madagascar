#!/usr/bin/env python3
"""Functional test T3 (memory spec §9): agents really read the memory. Spends quota, capped by the script.

Three runs, at most ``functional_test.t3_budget_cap`` points of the gpt-5.5 window in total:
- first run, python-hl7 ``mllp_transport`` (earlier milestones from the stored snapshot);
- first run, imapclient ``mailbox_message_and_extension_operations``;
  each target milestone must recall something (ADAMAS_MEMORY_REQUIRE_RECALL), and
  at least one of the two recalls a pitfall written from the other task;
- repair, imapclient ``mailbox_message_and_extension_operations``: the stored
  first run is replayed, then R0 plus the rows selection picks (ADAMAS_PAIR_ONLY).

Then items 1-6 are checked on the stored records: memory_delivery.json, the
captured prompts, MEMORY_ACK against recall, the required roles, canaries
anywhere in the runs and workspaces, and the sealed 20-character check of every
agent reply against the held-out sources. Results: outputs/memory/t3/result.json.

    uv run python scripts/memory_t3.py run     # launch (quota)
    uv run python scripts/memory_t3.py check   # checks only
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

OUT = ROOT / "outputs" / "memory" / "t3"
CONFIG = "configs/experiments/codeprojecteval_milestones_evolution.yaml"
DATASET = "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset"
EMPTY_TABLE = ROOT / "configs" / "playbook_v2" / "experiments" / "firstpass_from_repair_20261008" / "first_pass.empty.yaml"
CHECKPOINTS = {
    "python-hl7": "outputs/cpe_evolution/cpe-20261001T092429Z-python-hl7/python-hl7",
    "imapclient": "outputs/cpe_evolution/cpe-20261001T055006Z-imapclient/imapclient",
}
JOBS = [
    ("t3-fr-hl7", "first_run", "python-hl7", "mllp_transport"),
    ("t3-fr-imap", "first_run", "imapclient", "mailbox_message_and_extension_operations"),
    ("t3-rp-imap", "repair", "imapclient", "mailbox_message_and_extension_operations"),
]


def predecessor(task: str, mid: str) -> tuple[str, str]:
    run = ROOT / CHECKPOINTS[task]
    canon = next(run.glob("tasks/rb_*/canonical/repo"))
    order = [m["milestone_id"] for m in json.loads((run / "milestone_plan_draft.json").read_text())["milestones"]]
    log = subprocess.run(["git", "-C", str(canon), "log", "--format=%H %s"], capture_output=True, text=True, check=True).stdout
    commits, base = {}, ""
    for line in log.splitlines():
        sha, _, subj = line.partition(" ")
        if subj.startswith("canonical commit "):
            commits[subj.removeprefix("canonical commit ").strip()] = sha
        elif "dataset inputs" in subj:
            base = sha
    i = order.index(mid)
    return str(canon), (base if i == 0 else commits[order[i - 1]])


def job_cmd(jid: str, part: str, task: str, mid: str) -> tuple[list[str], dict[str, str]]:
    run = ROOT / CHECKPOINTS[task]
    repo, rev = predecessor(task, mid)
    cmd = [str(ROOT / ".venv" / "bin" / "python"), "-m", "orchestra.cli.run_codeprojecteval_decomp",
           "--config", CONFIG, "--task-id", task, "--arm", "memory_t3", "--run-id", jid,
           "--output-root", str(OUT / "runs"), "--dataset-root", DATASET, "--only-milestone", mid,
           "--base-snapshot", f"{repo}@{rev}", "--inherit-harness", str(run / "harness"),
           "--plan-file", str(ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{task}.plan.json")]
    env = {"ADAMAS_MEMORY_ENABLED": "1", "ADAMAS_FIRST_PASS_TABLE": str(EMPTY_TABLE),
           "ADAMAS_EVOLUTION_ROOT": str(OUT / "evolution")}
    if part == "first_run":
        env.update({"ADAMAS_FIRST_RUN_ONLY": "1", "ADAMAS_MEMORY_REQUIRE_RECALL": mid})
    else:
        env.update({"ADAMAS_SEED_FIRST_RUN": str(next(run.glob(f"tasks/rb_*/workspaces/{mid}/repo"))),
                    "ADAMAS_SEED_MILESTONE": mid, "ADAMAS_PAIR_ONLY": "1"})
    return cmd, env


def run(cap: float) -> dict:
    from joint_experiment import read_window

    OUT.mkdir(parents=True, exist_ok=True)
    w0 = read_window()
    if w0 is None:
        raise SystemExit("cannot read the quota window; refusing to spend")
    log = open(OUT / "driver.log", "a")
    procs = {}
    for jid, part, task, mid in JOBS:
        cmd, env = job_cmd(jid, part, task, mid)
        lf = open(OUT / f"{jid}.log", "w")
        procs[jid] = subprocess.Popen(cmd, cwd=str(ROOT), env={**os.environ, **env}, stdout=lf, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        print(f"[{datetime.now(UTC):%H:%M:%S}] started {jid} pid {procs[jid].pid}", file=log, flush=True)
        time.sleep(20)
    stop = ""
    last = w0
    while any(p.poll() is None for p in procs.values()):
        time.sleep(60)
        w = read_window()
        if w is not None:
            last = w
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
    print(json.dumps(rec), file=log, flush=True)
    (OUT / "spend.json").write_text(json.dumps(rec, indent=1))
    return rec


def check() -> dict:
    from orchestra.memory import runtime as R
    from orchestra.memory.assemble import parse_ack, parse_tags
    from orchestra.memory.validate import substring_check

    res: dict = {"jobs": {}}
    replies: dict[str, str] = {}
    for jid, part, task, mid in JOBS:
        batch = OUT / "runs" / jid
        rd = batch / task
        r: dict = {"part": part, "task": task, "milestone": mid, "run_dir": str(rd)}
        dp = rd / R.DELIVERY_FILE
        if not dp.is_file():
            r["error"] = "no memory_delivery.json"
            res["jobs"][jid] = r
            continue
        if not (dp.parent / "summary.json").is_file() or "audit" not in json.loads(dp.read_text()):
            R.audit_run(rd)
        d = json.loads(dp.read_text())
        fp = (d.get("first_pass") or {}).get(mid) or {}
        r["memory_version"] = (d.get("run") or {}).get("memory_version")
        r["judge"] = {"selected": fp.get("selected"), "domain_tags": fp.get("domain_tags"), "skill_version": fp.get("skill_version")}
        r["recalled_first_pass"] = fp.get("recalled")
        r["cross_task_entries"] = fp.get("cross_task_entries")
        nodes = [n for n in d.get("nodes") or []]
        r["violations"] = d.get("audit", {}).get("violations")
        # 1. delivery record complete, capture non-empty
        sent = [n for n in nodes if not n.get("seeded")]
        caps = {n["request_id"]: Path(n["captured_prompt"]) for n in sent if n.get("captured_prompt")}
        r["1_delivery_complete"] = {
            "has": {k: k in d for k in ("run", "first_pass", "nodes", "audit")}, "agent_calls": len(sent),
            "captures_nonempty": sum(1 for p in caps.values() if p.is_file() and p.stat().st_size > 0),
        }
        # 2. every recalled tag in the captured prompt; 3. MEMORY_ACK == recall
        tagged = []
        for n in sent:
            p = caps.get(n["request_id"])
            text = p.read_text() if p and p.is_file() else ""
            tags = parse_tags(text)
            if not tags:
                continue
            final = Path(str(p).replace(".prompt.txt", ".final.txt"))
            reply = final.read_text() if final.is_file() else ""
            replies[f"{jid}:{n['node_id']}:{n['request_id'][:8]}"] = reply
            expected = (n.get("tags_in_prompt") or [])
            tagged.append({"task_id": n["task_id"], "node": n["node_id"], "bank": n.get("bank"), "tags_in_captured_prompt": tags,
                           "memory_ack": parse_ack(reply), "ack_equals_recall": set(parse_ack(reply) or []) == set(expected),
                           "recalled_all_in_prompt": set(expected) <= set(tags)})
        for n in sent:
            p = caps.get(n["request_id"])
            final = Path(str(p).replace(".prompt.txt", ".final.txt")) if p else None
            if final and final.is_file():
                replies.setdefault(f"{jid}:{n['node_id']}:{n['request_id'][:8]}", final.read_text())
        r["2_3_tagged_calls"] = tagged
        r["repair_recall"] = d.get("repair")
        # 4. required roles
        r["4_roles_added"] = fp.get("roles_added") or []
        # 5. canaries in everything of the batch (runs + workspaces)
        r["5_canary_hits"] = R.canary_hits(batch)
        res["jobs"][jid] = r
    # 6. sealed: every agent reply vs held-out sources (20-char windows)
    sealed = substring_check({k: v for k, v in replies.items() if v.strip()}, tasks=["python-hl7", "imapclient"])
    res["6_sealed_reply_check"] = {"replies": len(sealed), "fail": [k for k, v in sealed.items() if v["heldout"] == "FAIL"]}
    spend = OUT / "spend.json"
    res["spend"] = json.loads(spend.read_text()) if spend.is_file() else None
    (OUT / "result.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["run", "check"])
    a = ap.parse_args()
    from orchestra.memory.store import load_config
    from orchestra.settings import load_env_file

    load_env_file(ROOT / ".env")
    if a.what == "run":
        cap = float((load_config().get("functional_test") or {}).get("t3_budget_cap", 3))
        print(json.dumps(run(cap), indent=1))
    res = check()
    print(json.dumps({j: {k: v for k, v in r.items() if k in ("recalled_first_pass", "cross_task_entries", "violations", "5_canary_hits")}
                      for j, r in res["jobs"].items()}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
