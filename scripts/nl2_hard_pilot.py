#!/usr/bin/env python3
"""NL2Repo hard-level comparison, stage 2 pilot (user decisions 2026-10-16). Spends quota, capped by this script.

Order: dbutils single session (smoke test: luna Codex end to end, the luna points-per-token rate, logs and scoring);
cerberus single + full; autopep8 single + full. Model gpt-5.6-luna for every arm.

Per run: proxy probe; the run (full arm: run_codeprojecteval_decomp with the frozen plan and the formal config;
single arm: scripts/nl2_single_session.py); a watcher on the luna quota window kills the run at the per-run cap
(15 points, revised after the smoke test) or the pilot cap (40 points); held-out scoring (eval_codeprojecteval,
confined, the converted dataset's held-out suite with its blind conftest fix, identical for all arms); the sealed
audit (similarity to the reference, conftest hashes); a manifest with model, Codex versions, limits, tokens, wall
clock, points. The planner's spend is booked separately (it is no arm's cost).

    uv run python scripts/nl2_hard_pilot.py run [--only dbutils:single] [--per-run-cap 15] [--total-cap 40]
    uv run python scripts/nl2_hard_pilot.py status
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
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

MODEL = "gpt-5.6-luna"
OUT = ROOT / "outputs" / "nl2repo_hard"
STATE = OUT / "pilot_state.json"
DATASET = "/root/codex-benchmarks/nl2repo/cpe_format"
ENVS = "/root/codex-benchmarks/nl2repo/envs"
CONFIG = "configs/experiments/nl2repo_hard_full.yaml"
PLANS = ROOT / "configs" / "datasets" / "nl2repo_hard_plans"
EMPTY_TABLE = ROOT / "configs/playbook_v2/experiments/firstpass_from_repair_20261008/first_pass.empty.yaml"
SINGLE_TIMEOUT = 21600          # 6 h: the single session's only clock limit besides the shared token cap
JOBS = [("nl2_dbutils", "single"), ("nl2_cerberus", "single"), ("nl2_cerberus", "full"),
        ("nl2_autopep8", "single"), ("nl2_autopep8", "full")]


def _state() -> dict:
    return json.loads(STATE.read_text()) if STATE.is_file() else {"runs": {}, "spend": {}}


def _save(st: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, default=str))


def window_reader(model: str = MODEL):
    import joint_experiment as JE

    def read() -> float | None:
        try:
            out = subprocess.run([JE.QUOTA], capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        block = out.split(model, 1)[-1] if model in out else ""
        m = re.search(r"window\s*:\s*(\d+(?:\.\d+)?)% used", block)
        return float(m.group(1)) if m else None

    return read


def proxy_ready() -> str:
    import urllib.error
    import urllib.request

    from orchestra.settings import load_env_file

    load_env_file(ROOT / ".env")
    base = (os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    body = json.dumps({"model": MODEL, "input": "Reply with ok.", "max_output_tokens": 200})
    req = urllib.request.Request(base + "/responses", data=body.encode(), headers={
        "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=180).read()
        return ""
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}: {exc.read()[:200]!r}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _env(arm: str) -> dict[str, str]:
    env = {**os.environ, "CODEX_MODEL": MODEL, "DIAGNOSIS_MODEL": MODEL, "CPE_DATASET_ROOT": DATASET, "CPE_ENV_ROOT": ENVS}
    env.pop("ADAMAS_AGENT_TIMEOUT_CEILING", None)
    if arm == "full":
        env.update({"ADAMAS_MEMORY_ENABLED": "1", "ADAMAS_FIRST_PASS_RECALL": "0", "ADAMAS_FIRST_PASS_TABLE": str(EMPTY_TABLE),
                    "ADAMAS_EVOLUTION_ROOT": str(ROOT / "outputs" / "evolution_nl2hard")})
    else:
        env.update({"ADAMAS_MEMORY_ENABLED": "0", "ADAMAS_AGENT_TIMEOUT_CEILING": str(SINGLE_TIMEOUT)})
    return env


def _versions() -> dict:
    out = {}
    try:
        import importlib.metadata as md
        out["openai_codex"] = md.version("openai-codex")
        out["openai_codex_cli_bin"] = md.version("openai-codex-cli-bin")
    except Exception:  # noqa: BLE001
        pass
    return out


def _tokens(task_dir: Path) -> dict:
    f = task_dir / "logs" / "03_post_run" / "backend_usage_records.json"
    if not f.is_file():
        return {}
    recs = json.loads(f.read_text())
    recs = recs if isinstance(recs, list) else (recs.get("records") or list(recs.values()))
    pt = ct = ca = 0
    for r in recs:
        u = (r.get("usage") if isinstance(r, dict) else None) or r
        pt += int(u.get("prompt_tokens") or 0)
        ct += int(u.get("completion_tokens") or 0)
        ca += int(u.get("cached_tokens") or u.get("cached_prompt_tokens") or 0)
    return {"prompt_tokens": pt, "completion_tokens": ct, "cached_tokens": ca, "records": len(recs)}


def run_job(task: str, arm: str, spend, per_run_cap: float, total_cap: float, total_before: float) -> dict:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{arm}-{stamp}-{task}"
    batch = OUT / run_id
    rec: dict = {"task": task, "arm": arm, "run_id": run_id, "batch": str(batch), "model": MODEL, "started": stamp,
                 "points_before": spend.used(), "versions": _versions(),
                 "limits": {"token_cap_prompt": 60_000_000, "per_run_points_cap": per_run_cap,
                            "codex_steps": "not limited (the Codex backend has no step limit)",
                            "wall_clock": (f"{SINGLE_TIMEOUT} s for the one session" if arm == "single"
                                           else "per node: the frozen plan's timeout_seconds, clamped to <= 2400 s")}}
    py = str(ROOT / ".venv" / "bin" / "python")
    if arm == "single":
        cmd = [py, "scripts/nl2_single_session.py", "--task", task, "--run-id", run_id, "--output-root", str(OUT),
               "--timeout", str(SINGLE_TIMEOUT), "--model", MODEL]
    else:
        cmd = [py, "-m", "orchestra.cli.run_codeprojecteval_decomp", "--config", CONFIG, "--task-id", task, "--arm", "nl2_hard_full",
               "--run-id", run_id, "--output-root", str(OUT), "--dataset-root", DATASET, "--plan-file", str(PLANS / f"{task}.plan.json")]
    log = OUT / f"{run_id}.log"
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=_env(arm), stdout=open(log, "w"), stderr=subprocess.STDOUT, start_new_session=True)
    stop = ""
    while proc.poll() is None:
        time.sleep(60)
        used = spend.poll()
        if used - rec["points_before"] >= per_run_cap:
            stop = f"per-run cap {per_run_cap} points reached"
        elif total_before + used >= total_cap:
            stop = f"pilot cap {total_cap} points reached"
        if stop:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            break
    rec["stop"] = stop
    rec["returncode"] = proc.returncode
    rec["wall_seconds"] = round(time.time() - t0, 1)
    rec["points_run"] = round(spend.poll() - rec["points_before"], 2)
    task_dir = batch / task
    rec["tokens"] = _tokens(task_dir)
    s = json.loads((task_dir / "summary.json").read_text()) if (task_dir / "summary.json").is_file() else {}
    rec["sandbox_breach"] = s.get("sandbox_breach")
    rec["sandbox_void"] = bool(s.get("sandbox_void"))
    rec["single_status"] = s.get("status") if arm == "single" else None
    if arm == "full":
        te = next(iter(task_dir.glob("tasks/rb_*/task_execution.json")), None)
        if te:
            subs = json.loads(te.read_text()).get("subtasks") or {}
            rec["milestones"] = {k: v.get("status") for k, v in subs.items()}
    # held-out scoring, confined, identical for every arm
    ev = subprocess.run([py, "scripts/eval_codeprojecteval.py", str(batch), "--dataset-root", DATASET, "--env-root", ENVS],
                        cwd=str(ROOT), capture_output=True, text=True, timeout=8 * 3600)
    hv = batch / "hidden_eval.json"
    if hv.is_file():
        h = json.loads(hv.read_text())
        r = (h.get("results") or [h])[0] if isinstance(h.get("results"), list) else h
        rec["heldout"] = {k: r.get(k) for k in ("pass_rate", "passed", "failed", "error", "tests_total", "status", "pass_rate_observed")}
        rec["pass_at_1"] = bool(r.get("tests_total") and r.get("passed") == r.get("tests_total"))
    rec["heldout_rc"] = ev.returncode
    au = subprocess.run([py, "scripts/sealed/nl2_hard_audit.py", str(task_dir)], cwd=str(ROOT), capture_output=True, text=True, timeout=1800)
    try:
        rec["audit"] = json.loads(au.stdout)[0]
    except (ValueError, IndexError):
        rec["audit"] = {"error": (au.stderr or "")[-300:]}
    rec["finished"] = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (task_dir / "pilot_manifest.json").write_text(json.dumps(rec, indent=1, default=str)) if task_dir.is_dir() else None
    return rec


def run(per_run_cap: float, total_cap: float, only: str = "") -> dict:
    from quota_spend import Spend
    import joint_experiment as JE
    from orchestra.sandbox.guard import require_sandbox

    require_sandbox("nl2repo hard pilot")
    JE.read_window = window_reader(MODEL)
    st = _state()
    why = proxy_ready()                     # also makes quota.sh show the luna window after a proxy restart
    if why:
        st["stop"] = f"model channel not ready: {why}"
        _save(st)
        return st
    spend = Spend()
    total_before = float(st.get("spend", {}).get("used") or 0.0)
    for task, arm in JOBS:
        key = f"{task}:{arm}"
        if only and key != only:
            continue
        if key in st["runs"] and not st["runs"][key].get("stop"):
            continue
        if total_before + spend.poll() >= total_cap:
            st["stop"] = f"pilot cap {total_cap} reached before {key}"
            break
        why = proxy_ready()
        if why:
            st["stop"] = f"model channel not ready before {key}: {why}"
            break
        rec = run_job(task, arm, spend, per_run_cap, total_cap, total_before)
        st["runs"][key] = rec
        st["spend"] = {"used": round(total_before + spend.used(), 2), "resets": spend.resets, "pilot_cap": total_cap}
        _save(st)
        print(json.dumps({key: {k: rec.get(k) for k in ("stop", "points_run", "tokens", "heldout", "pass_at_1", "wall_seconds")}},
                         default=str), flush=True)
        if rec.get("stop") or only:
            break
    _save(st)
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["run", "status"])
    ap.add_argument("--per-run-cap", type=float, default=15.0)
    ap.add_argument("--total-cap", type=float, default=40.0)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    st = run(a.per_run_cap, a.total_cap, a.only) if a.what == "run" else _state()
    print(json.dumps(st, indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
