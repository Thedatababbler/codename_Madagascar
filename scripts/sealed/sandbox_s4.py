#!/usr/bin/env python3
"""Sandbox test S4: scoring is unchanged under the isolation (sandbox spec A §4).

For stored first-run repositories of four tasks, the gate suite and the held-out
cases are re-run with ``ADAMAS_SANDBOX_ENFORCED=1``:
- the gate runs fully confined;
- held-out scoring is confined to the network rules only.

Both are compared case by case with the stored results:
- gate: the experiment state, or round-1 eval failure lists;
- held-out: the sealed cache.

Output: counts of equal / different cases only, never case content.

    uv run python scripts/sealed/sandbox_s4.py [--out outputs/sandbox/s4.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_results as hr  # noqa: E402
from _guard import task_python  # noqa: E402
from author_eval import run_suite  # noqa: E402

CASES = [
    ("fp", "R11|cookiecutter|template_source_resolution|0"),
    ("fp", "F0|imapclient|mailbox_message_and_extension_operations|0"),
    ("joint", "r1-fr-voluptuous-errors_markers_contracts-F0-0"),
    ("joint", "r1-fr-python-hl7-hierarchical_containers-F0-0"),
]


def locate(kind: str, key: str):
    if kind == "fp":
        st = json.loads((ROOT / "outputs" / "fp_from_repair" / "state.json").read_text())
        rec = st["runs"][key]
        _, task, mid, _ = key.split("|")
        run = ROOT / "outputs" / "fp_from_repair" / "runs" / rec["job_id"] / task
        stored = {k: v for k, v in (rec.get("gate") or {}).items()}
    else:
        rec = json.loads((ROOT / "outputs" / "joint_20261007" / "round1" / "eval.json").read_text())[key]
        task, mid = rec["task"], rec["milestone"]
        run = ROOT / "outputs" / "joint_20261007" / "runs" / key / task
        stored = {"__failed__": sorted(rec.get("gate_failed") if isinstance(rec.get("gate_failed"), list) else eval(rec["gate_failed"])),
                  "__total__": int(rec["gate_total"])}
    ws = next(run.glob(f"tasks/*/workspaces/{mid}/repo"))
    suite = run / "harness" / f"{mid}.spec_tests"
    return task, mid, ws, suite, stored


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "outputs" / "sandbox" / "s4.json"))
    a = ap.parse_args()
    os.environ["ADAMAS_SANDBOX_ENFORCED"] = "1"
    out = {}
    for kind, key in CASES:
        task, mid, ws, suite, stored = locate(kind, key)
        py = task_python(task)
        gate = run_suite(ws, suite, py)
        if "__failed__" in stored:
            failed = sorted(k for k, v in gate.items() if v == "fail")
            g = {"total_stored": stored["__total__"], "total_now": len(gate), "failed_equal": failed == stored["__failed__"],
                 "n_failed_stored": len(stored["__failed__"]), "n_failed_now": len(failed)}
            g["equal"] = g["failed_equal"] and g["total_stored"] == g["total_now"]
        else:
            diff = sorted(k for k in set(stored) | set(gate) if stored.get(k) != gate.get(k))
            g = {"cases": len(stored), "different": len(diff), "equal": not diff}
        held = []
        for f in sorted((hr.CACHE_ROOT / task).glob("*.json")):
            rec = json.loads(f.read_text())
            if Path(rec.get("workspace_ref") or "").resolve() != ws.resolve():
                continue
            cases = hr.attributed_cases(task, rec["milestone"])
            now = hr.run_heldout(task, ws, cases)
            cached = rec.get("cases") or {}
            diff = [k for k in set(cached) | set(now) if cached.get(k) != now.get(k)]
            held.append({"scope": rec["milestone"], "cases": len(cached), "different": len(diff), "equal": not diff})
        out[key] = {"task": task, "gate": g, "heldout": held,
                    "equal": g["equal"] and all(h["equal"] for h in held) and bool(held)}
        print(f"{key}: gate equal={g['equal']} heldout={[(h['scope'], h['cases'], h['different']) for h in held]}")
    out["pass"] = all(v["equal"] for k, v in out.items() if k != "pass")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print("pass", out["pass"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
