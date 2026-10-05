#!/usr/bin/env python3
"""How much of what the held-out tests call does the documents-only public-symbol list
contain? (user's decision 2026-10-04, check on the derivation rule.) Training tasks only.

For every milestone: the public names (no leading underscore) the attributed held-out
cases reach (attribution ``case_symbols``) against the derived list -- strict (own focus
paths) and lenient (own + earlier milestones + unattributed). Output: ratios and symbol
names only.

    uv run python scripts/sealed/symbol_recall.py [--out docs/reports/symbol_recall.md]
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

from _guard import DATASET_ROOT, assert_no_source_overlap  # noqa: E402
from orchestra.codeprojecteval.public_symbols import derive_public_symbols, load_docs  # noqa: E402

EVO = Path(os.environ.get("ADAMAS_EVOLUTION_ROOT") or "outputs/evolution")
TRAIN = ["bplustree", "cookiecutter", "csvs-to-sqlite", "deprecated", "djangorestframework-simplejwt", "flask",
         "imapclient", "python-hl7", "rsa", "tinydb", "voluptuous", "zxcvbn"]


def _hit(sym: str, names: set[str]) -> bool:
    return sym in names or sym.split(".")[-1] in names


def recall_task(task: str) -> list[dict]:
    attr = json.loads((EVO / "sealed" / "attribution" / f"{task}.json").read_text(encoding="utf-8"))
    plan = json.loads((ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{task}.plan.json").read_text(encoding="utf-8"))
    inv = derive_public_symbols(load_docs(DATASET_ROOT / task / "docs"))
    order = [m["milestone_id"] for m in plan["milestones"]]
    focus = {m["milestone_id"]: m.get("focus_paths") or [] for m in plan["milestones"]}
    rows = []
    for i, mid in enumerate(order):
        cases = attr["attribution"].get(mid) or []
        syms = sorted({s for c in cases for s in (attr["case_symbols"].get(c) or []) if not any(p.startswith("_") for p in s.split("."))})
        if not syms:
            continue
        strict = inv.owned_by(focus[mid])
        lenient = inv.lenient(focus[mid], [p for m in order[:i] for p in focus[m]])
        anywhere = inv.names()
        s_hit = [s for s in syms if _hit(s, strict)]
        l_hit = [s for s in syms if _hit(s, lenient)]
        a_hit = [s for s in syms if _hit(s, anywhere)]
        rows.append({"task": task, "milestone": mid, "heldout_symbols": len(syms),
                     "recall_strict": round(len(s_hit) / len(syms), 3), "recall_lenient": round(len(l_hit) / len(syms), 3),
                     "recall_anywhere": round(len(a_hit) / len(syms), 3),
                     "missed_everywhere": [s for s in syms if s not in a_hit][:12]})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    rows = [r for t in TRAIN for r in recall_task(t)]
    for t in TRAIN:
        tr = [r for r in rows if r["task"] == t]
        if tr:
            text = json.dumps(tr)
            assert_no_source_overlap(text, t)

    def mean(k, rs):
        return sum(r[k] for r in rs) / len(rs) if rs else 0.0
    lines = ["# Public-symbol derivation: recall against what held-out tests call", "",
             "Training tasks; per milestone, the public names the attributed held-out cases reach, found in the "
             "documents-only list. Strict = the milestone's own focus paths (used by coverage and metrics); "
             "lenient = own + earlier milestones + unattributed (used by the audit); anywhere = the whole list.", "",
             "| task | milestones | strict | lenient | anywhere |", "|---|---|---|---|---|"]
    for t in TRAIN:
        tr = [r for r in rows if r["task"] == t]
        if tr:
            lines.append(f"| {t} | {len(tr)} | {mean('recall_strict', tr):.2f} | {mean('recall_lenient', tr):.2f} | {mean('recall_anywhere', tr):.2f} |")
    lines.append(f"| **all** | {len(rows)} | {mean('recall_strict', rows):.2f} | {mean('recall_lenient', rows):.2f} | {mean('recall_anywhere', rows):.2f} |")
    lines += ["", "Names held-out reaches that the documents never list (first 12 per milestone):", ""]
    for r in rows:
        if r["missed_everywhere"]:
            lines.append(f"- {r['task']} / {r['milestone']}: {', '.join('`' + s + '`' for s in r['missed_everywhere'])}")
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
