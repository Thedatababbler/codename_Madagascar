#!/usr/bin/env python3
"""Repair statistics of the sandboxed baseline batch against the 10-01/02 evolution runs (memory replay, 2026-10-14).

Reads stored runs only (task_execution.json, the frozen gate suites' sidecars, the two evolution ledgers); starts
no agent. Per milestone: whether the first run passed the gate, why a search ran, the probes, default repair (R0),
rows and node resamples that ran, why each uncommitted candidate lost, the source of the committed result, and how
many gate cases the training-task reference filter took out. Output: outputs/clean_baseline_cmp/repair_stats.json.

    uv run python scripts/clean_baseline_repair_stats.py
"""

from __future__ import annotations

import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

NEW = {"deprecated": "cpe-20261012T050337Z-deprecated", "csvs-to-sqlite": "cpe-20261012T053045Z-csvs-to-sqlite",
       "rsa": "cpe-20261012T064502Z-rsa", "djangorestframework-simplejwt": "cpe-20261012T081623Z-djangorestframework-simplejwt",
       "voluptuous": "cpe-20261012T101048Z-voluptuous", "zxcvbn": "cpe-20261012T140933Z-zxcvbn",
       "python-hl7": "cpe-20261012T165502Z-python-hl7"}
OLD = {"deprecated": "cpe-20261001T053508Z-deprecated", "csvs-to-sqlite": "cpe-20261002T060628Z-csvs-to-sqlite",
       "rsa": "cpe-20261002T030933Z-rsa", "djangorestframework-simplejwt": "cpe-20261001T061241Z-djangorestframework-simplejwt",
       "voluptuous": "cpe-20261002T071738Z-voluptuous", "zxcvbn": "cpe-20261002T041405Z-zxcvbn",
       "python-hl7": "cpe-20261001T092429Z-python-hl7"}
ROOTS = {"new": (ROOT / "outputs" / "cpe_sandbox_baseline", ROOT / "outputs" / "evolution_sandbox" / "ledger" / "v1"),
         "old": (ROOT / "outputs" / "cpe_evolution", ROOT / "outputs" / "evolution" / "ledger" / "v1")}


def kind_of(cid: str) -> str:
    if cid == "incumbent_first_pass":
        return "first_run"
    if cid.startswith("cand_feedback"):
        return "probe"
    if cid == "cand_R0":
        return "R0"
    if cid.startswith(("cand_v2:", "cand_pb_")):
        return "row"
    if cid.startswith(("cand_node_resample", "cand_reauthor", "node_resample")):
        return "resample"
    if cid.startswith("infra_retry"):
        return "infra_retry"
    return "other"


def ledger(path: Path, run_dir: str) -> tuple[dict, dict]:
    """(candidate records by (milestone, candidate id), milestone records by milestone) of one run."""
    cands, mss = {}, {}
    for f, into in (("candidates.jsonl", cands), ("milestones.jsonl", mss)):
        p = path / f
        if not p.is_file():
            continue
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not str(r.get("run_dir", "")).startswith(run_dir):
                continue
            if f == "candidates.jsonl":
                cid = str(r["record_id"]).split(":", 2)[-1]
                into[(r["milestone_id"], cid)] = r
            else:
                into[r["milestone_id"]] = r
    return cands, mss


def gate_size(harness: Path, mid: str) -> tuple[int | None, int]:
    """(cases in the frozen gate suite, cases the reference filter removed)."""
    size = None
    sj = harness / f"{mid}.spec_tests.size.json"
    if sj.is_file():
        try:
            d = json.loads(sj.read_text())
            size = int(d.get("pinned") or d.get("cases") or d.get("collected") or d.get("total") or 0) or None
        except (ValueError, TypeError):
            size = None
    if size is None and (harness / f"{mid}.spec_tests").is_dir():
        size = sum(len(re.findall(r"^\s*(?:async\s+)?def (test\w*)", p.read_text(errors="replace"), re.M))
                   for p in (harness / f"{mid}.spec_tests").rglob("test_*.py"))
    sus = harness / f"{mid}.spec_tests.suite_suspect.json"
    removed = 0
    if sus.is_file():
        try:
            d = json.loads(sus.read_text())
            removed = len(d.get("cases") or d.get("suspects") or d) if isinstance(d, (dict, list)) else 0
        except ValueError:
            removed = 0
    return size, removed


def loss_reason(c: dict, rec: dict | None) -> str:
    st = c.get("status")
    if st in ("harness_failed", "backend_failed", "failed", "infra_failed") or (c.get("failure_reason") and st != "valid"):
        if st != "discarded":
            return "run_failed"
    if rec is None:
        return "no_ledger_record"
    fixed, regressed = rec.get("fixed") or [], rec.get("regressed") or []
    if rec.get("filtered_reason"):
        return "rejected_by_acceptance"
    if not fixed:
        return "fixed_no_persistent"
    if regressed:
        return "fixed_but_regressed"
    return "fixed_but_not_better_than_incumbent"


def run_stats(task: str, which: str) -> dict:
    out_root, led = ROOTS[which]
    batch = (NEW if which == "new" else OLD)[task]
    run = out_root / batch / task
    te = json.loads(next(run.glob("tasks/rb_*/task_execution.json")).read_text())
    cands_led, ms_led = ledger(led, str(run.resolve()))
    order = list((te.get("subtasks") or {}).keys())
    res = []
    for mid in order:
        sub = te["subtasks"][mid]
        fl = (te.get("fast_loop_states") or {}).get(mid) or {}
        cands = fl.get("candidates") or []
        msr = ms_led.get(mid) or {}
        inc = next((c for c in cands if c["candidate_id"] == "incumbent_first_pass"), None)
        first_attempt = (sub.get("attempts") or [{}])[0]
        stages = (first_attempt.get("metadata") or {}).get("harness_stages") or []
        if inc is not None:
            first_pass = False if msr.get("first_run_gate") == "discarded" or inc.get("status") == "discarded" else True
            first_fail = len(inc.get("behaviour_failures") or [])
        else:
            first_pass = sub.get("status") == "committed" and not cands
            spec = next((s for s in stages if s.get("stage") == "spec_tests"), None)
            first_fail = (spec["total_units"] - spec["passed_units"]) if spec else None
        searched = bool(cands)
        notes = " | ".join(map(str, fl.get("notes") or []))
        trigger = None
        if searched:
            trigger = "gate_failure" if not first_pass else "quality_below_0.9"
        kinds = Counter(kind_of(c["candidate_id"]) for c in cands if c["candidate_id"] != "incumbent_first_pass")
        committed = next((c for c in cands if c.get("status") == "committed"), None)
        source = kind_of(committed["candidate_id"]) if committed else ("first_run" if sub.get("status") == "committed" else "none")
        losses = {}
        for c in cands:
            if c is committed or c["candidate_id"] == "incumbent_first_pass":
                continue
            losses[c["candidate_id"]] = loss_reason(c, cands_led.get((mid, c["candidate_id"])))
        r0 = cands_led.get((mid, "cand_R0"))
        m = re.search(r"reference filter: (\d+) gate case", notes)
        size, removed = gate_size(run / "harness", mid)
        res.append({
            "milestone": mid, "final_status": sub.get("status"), "first_run_passed_gate": first_pass,
            "first_run_gate_failures": first_fail, "searched": searched, "trigger": trigger,
            "probes": kinds.get("probe", 0), "probe_results": [(c["candidate_id"], c.get("status")) for c in cands if kind_of(c["candidate_id"]) == "probe"],
            "R0_runs": kinds.get("R0", 0), "R0_committed": bool(committed and kind_of(committed["candidate_id"]) == "R0"),
            "R0_fixed": list((r0 or {}).get("fixed") or []), "R0_regressed": list((r0 or {}).get("regressed") or []),
            "rows": kinds.get("row", 0), "row_ids": [c["candidate_id"] for c in cands if kind_of(c["candidate_id"]) == "row"],
            "resamples": kinds.get("resample", 0), "committed_source": source,
            "committed_candidate": committed["candidate_id"] if committed else None,
            "committed_fixed": list((cands_led.get((mid, committed["candidate_id"])) or {}).get("fixed") or []) if committed else [],
            "loss_reasons": losses, "gate_cases": size, "reference_filter_removed": int(m.group(1)) if m else removed,
            "notes": fl.get("notes") or [],
        })
    return {"task": task, "which": which, "batch": batch, "milestones": res}


def main() -> int:
    out = {"new": {}, "old": {}}
    for t in NEW:
        for w in ("new", "old"):
            out[w][t] = run_stats(t, w)
    p = ROOT / "outputs" / "clean_baseline_cmp" / "repair_stats.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1, default=str))
    for w in ("new", "old"):
        ms = [m for t in out[w].values() for m in t["milestones"]]
        print(w, "milestones", len(ms), "first-pass", sum(m["first_run_passed_gate"] for m in ms),
              "searched", sum(m["searched"] for m in ms), "R0 runs", sum(m["R0_runs"] for m in ms),
              "R0 committed", sum(m["R0_committed"] for m in ms), "sources", dict(Counter(m["committed_source"] for m in ms)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
