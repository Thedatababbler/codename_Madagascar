#!/usr/bin/env python3
"""Mark the sandbox baseline's repair candidates whose repairer got invalid evidence (user decision 2026-10-15).

Two causes, both read off the stored runs (docs/reports/repair_evidence_mismatch_20261015.md):
  fake_regressions        -- an R0 or row in a milestone whose diagnosis ran the predecessor suites in the task's
                             initial workspace (a predecessor suite with 0 passing and every committed case
                             "regressed"), so its persistent list was cases that pass on the incumbent;
  evidence_not_delivered  -- a table row (``cand_v2:*``) whose evidence run collected nothing (the colon split
                             PYTHONPATH: "found no collectors" / "ERROR collecting").
Marks go to outputs/sandbox/evidence_invalid.jsonl; the ledger reader and the transfer channel drop them.

    uv run python scripts/mark_evidence_invalid.py [--dry-run]
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BATCH = ROOT / "outputs" / "cpe_sandbox_baseline"
REPORT = "docs/reports/repair_evidence_mismatch_20261015.md"


def fake_regressions(routing: dict) -> bool:
    rep = routing.get("incumbent_prior_suites") or {}
    return any(e.get("ran") and not e.get("passed_count") and e.get("regressions") for e in rep.values())


def evidence_collected_nothing(meta: dict) -> bool:
    ev = meta.get("repair_evidence")
    f = Path(ev) / "failures.md" if ev else None
    if not f or not f.is_file():
        return True
    text = f.read_text(errors="replace")
    return "found no collectors" in text or "ERROR collecting" in text or "no tests ran" in text


def find() -> list[dict]:
    out = []
    for te_p in sorted(glob.glob(str(BATCH / "cpe-*" / "*" / "tasks" / "rb_*" / "task_execution.json"))):
        te = json.loads(Path(te_p).read_text())
        run_dir = str(Path(te_p).parents[2])
        for mid, fl in (te.get("fast_loop_states") or {}).items():
            fake = fake_regressions(fl.get("routing") or {})
            for c in fl.get("candidates") or []:
                cid, meta = c["candidate_id"], c.get("metadata") or {}
                kind = meta.get("candidate_kind") or ("row" if cid.startswith("cand_v2") else "")
                if kind not in ("R0", "row") and not cid.startswith("cand_v2"):
                    continue
                reasons = []
                if fake and meta.get("persistence_phase") == 2:
                    reasons.append("fake_regressions")
                if cid.startswith("cand_v2") and evidence_collected_nothing(meta):
                    reasons.append("evidence_not_delivered")
                if reasons:
                    out.append({"run_dir": run_dir, "milestone_id": mid, "candidate_id": cid, "kind": kind or "row",
                                "status": c.get("status"), "reason": "+".join(reasons),
                                "marked_at": datetime.now(UTC).isoformat(timespec="seconds"), "report": REPORT})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    from orchestra.control.evolution.ledger import mark_evidence_invalid

    entries = find()
    for e in entries:
        print(f"{Path(e['run_dir']).name:32s} {e['milestone_id']:36s} {e['candidate_id']:16s} {e['kind']:4s} {e['status']:10s} {e['reason']}")
    by = {}
    for e in entries:
        for r in e["reason"].split("+"):
            by.setdefault((r, e["kind"]), 0)
            by[(r, e["kind"])] += 1
    print(json.dumps({f"{r}/{k}": n for (r, k), n in sorted(by.items())}), "total", len(entries))
    if not a.dry_run:
        print("added", mark_evidence_invalid(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
