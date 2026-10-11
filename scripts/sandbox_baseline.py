#!/usr/bin/env python3
"""Sandboxed baseline batch (EXP-20261011-03, approved 2026-10-11): eight training tasks, one full run each.

Per task, in order (cheapest first), sequentially:
1. the standard procedure under ``configs/experiments/cpe_sandbox_baseline.yaml``: sandbox enforced, frozen plan,
   author layer on, first pass empty (empty first-pass table, ``ADAMAS_FIRST_PASS_RECALL=0``), repair and author
   memory banks on, new evolution root ``outputs/evolution_sandbox``;
2. held-out scoring (``scripts/eval_codeprojecteval.py``, confined);
3. the transfer channel on this batch's runs only (memory-train tasks feed it; test tasks are skipped by it);
4. the run's breach summary.

Spend: capped (``--cap`` points, reset-aware). After the first three tasks the total is re-estimated from what they
cost against their estimates; if the projection exceeds the cap the batch pauses for the user.

    uv run python scripts/sandbox_baseline.py run [--cap 60]
    uv run python scripts/sandbox_baseline.py status
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

CONFIG = "configs/experiments/cpe_sandbox_baseline.yaml"
DATASET = "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset"
OUT = ROOT / "outputs" / "cpe_sandbox_baseline"
EMPTY_TABLE = ROOT / "configs/playbook_v2/experiments/firstpass_from_repair_20261008/first_pass.empty.yaml"
EVOLUTION_ROOT = ROOT / "outputs" / "evolution_sandbox"
# (task, estimated points) -- docs/reports/sandbox_only_prep_20261011.md
TASKS = [("deprecated", 2.5), ("bplustree", 5), ("csvs-to-sqlite", 5), ("rsa", 5.5),
         ("djangorestframework-simplejwt", 6), ("voluptuous", 6), ("zxcvbn", 10), ("python-hl7", 13)]
STATE = OUT / "state.json"


def _state() -> dict:
    return json.loads(STATE.read_text()) if STATE.is_file() else {"tasks": {}, "spend": {}}


def _save(st: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, default=str))


def _env() -> dict[str, str]:
    return {**os.environ, "ADAMAS_MEMORY_ENABLED": "1", "ADAMAS_FIRST_PASS_RECALL": "0",
            "ADAMAS_FIRST_PASS_TABLE": str(EMPTY_TABLE), "ADAMAS_EVOLUTION_ROOT": str(EVOLUTION_ROOT)}


def _watch(proc: subprocess.Popen, spend, cap: float) -> str:
    while proc.poll() is None:
        time.sleep(60)
        if spend.poll() >= cap:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            return f"budget cap {cap} reached (used {spend.used()})"
    return ""


def run_task(task: str, spend, cap: float) -> dict:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    batch = OUT / f"cpe-{stamp}-{task}"
    rec: dict = {"task": task, "batch": str(batch), "started": stamp, "points_before": spend.used()}
    cmd = [str(ROOT / ".venv" / "bin" / "python"), "-m", "orchestra.cli.run_codeprojecteval_decomp", "--config", CONFIG,
           "--task-id", task, "--arm", "sandbox_baseline", "--run-id", batch.name, "--output-root", str(OUT),
           "--dataset-root", DATASET, "--plan-file", str(ROOT / "configs/datasets/cpe_feature_plans" / f"{task}.plan.json")]
    OUT.mkdir(parents=True, exist_ok=True)
    log = OUT / f"{batch.name}.log"
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=_env(), stdout=open(log, "w"), stderr=subprocess.STDOUT,
                            start_new_session=True)
    rec["stop"] = _watch(proc, spend, cap)
    rec["returncode"] = proc.returncode
    rec["points_run"] = round(spend.poll() - rec["points_before"], 2)
    if rec["stop"]:
        return rec
    # held-out scoring (confined, sealed reads); no model call
    ev = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "eval_codeprojecteval.py"), str(batch)],
                        cwd=str(ROOT), capture_output=True, text=True, timeout=8 * 3600)
    hv = batch / "hidden_eval.json"
    rec["heldout"] = (json.loads(hv.read_text()).get("mean_pass_rate") if hv.is_file() else None)
    rec["heldout_rc"] = ev.returncode
    # breach summary written by the run itself
    sj = batch / task / "summary.json"
    s = json.loads(sj.read_text()) if sj.is_file() else {}
    rec["sandbox_breach"] = s.get("sandbox_breach")
    rec["sandbox_void"] = bool(s.get("sandbox_void"))
    # transfer channel on this batch only (it skips the memory-test tasks itself)
    tr = subprocess.run([str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "scripts" / "memory_transfer.py"),
                         "--runs-glob", f"outputs/cpe_sandbox_baseline/cpe-*-{{task}}/{{task}}"],
                        cwd=str(ROOT), env=_env(), capture_output=True, text=True, timeout=3 * 3600)
    try:
        rec["transfer"] = json.loads(tr.stdout[tr.stdout.index("{"):])
    except ValueError:
        rec["transfer"] = {"rc": tr.returncode, "tail": (tr.stdout + tr.stderr)[-800:]}
    rec["points_total"] = round(spend.poll() - rec["points_before"], 2)
    rec["finished"] = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return rec


def run(cap: float) -> dict:
    from quota_spend import Spend
    from orchestra.sandbox.guard import require_sandbox

    require_sandbox("sandbox baseline batch")
    st = _state()
    spend = Spend(float(st.get("spend", {}).get("used") or 0.0))
    done_est = done_used = 0.0
    for i, (task, est) in enumerate(TASKS):
        if task in st["tasks"] and not st["tasks"][task].get("stop"):
            done_est += est
            done_used += st["tasks"][task].get("points_total") or 0
            continue
        if spend.poll() >= cap:
            st["stop"] = f"budget cap {cap} reached before {task}"
            break
        rec = run_task(task, spend, cap)
        st["tasks"][task] = rec
        st["spend"] = {"used": spend.used(), "resets": spend.resets, "cap": cap}
        _save(st)
        if rec.get("stop"):
            st["stop"] = rec["stop"]
            break
        done_est += est
        done_used += rec.get("points_total") or 0
        if i == 2:
            ratio = done_used / done_est if done_est else 1.0
            projected = round(done_used + ratio * sum(e for _, e in TASKS[3:]), 1)
            st["reestimate"] = {"after": [t for t, _ in TASKS[:3]], "used": done_used, "estimated": done_est,
                                "ratio": round(ratio, 2), "projected_total": projected}
            _save(st)
            if projected > cap:
                st["stop"] = f"paused: projected {projected} points exceeds the cap {cap}"
                break
    _save(st)
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["run", "status"])
    ap.add_argument("--cap", type=float, default=60.0)
    a = ap.parse_args()
    st = run(a.cap) if a.what == "run" else _state()
    print(json.dumps(st, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
