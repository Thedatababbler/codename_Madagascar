#!/usr/bin/env python3
"""Which documented held-out failures did the behaviour inventory miss? (spec §5 item 3)

For a held-out case labelled ``documented`` (``doc_label.py``), the document paragraphs it
rests on are known by index. The inventory *covers* the case when one of its items quotes
a sentence inside one of those paragraphs and names one of the case's symbols (leaf match);
otherwise the case is an ``inventory_miss``: the extraction step, not the author, lost it.
Output: case ids and booleans.

    uv run python scripts/sealed/inventory_gap.py --task tinydb --milestone M --inventory <run>/inventory.json
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

from _guard import assert_no_source_overlap  # noqa: E402
from doc_label import doc_paragraphs  # noqa: E402
from orchestra.codeprojecteval.suite_audit import normalise  # noqa: E402

EVO = Path(os.environ.get("ADAMAS_EVOLUTION_ROOT") or "outputs/evolution")


def inventory_misses(labels: dict[str, dict], paragraphs: list[str], items: list[dict], milestone: str) -> dict[str, bool]:
    """``{case: missed}`` for the documented cases of ``milestone``. Pure."""
    norm_paras = [normalise(p) for p in paragraphs]
    out: dict[str, bool] = {}
    for cid, lab in labels.items():
        if lab.get("milestone") != milestone or lab.get("label") != "documented":
            continue
        leaves = {s.split(".")[-1] for s in lab.get("symbols") or []}
        backing = [norm_paras[i] for i in lab.get("paragraphs") or [] if 0 <= i < len(norm_paras)]
        covered = False
        for it in items:
            q = normalise(str(it.get("quote") or ""))
            sym_leaf = str(it.get("symbol") or "").split(".")[-1]
            if q and any(q in bp for bp in backing) and (not leaves or sym_leaf in leaves):
                covered = True
                break
        out[cid] = not covered
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--milestone", required=True)
    ap.add_argument("--inventory", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    labels = json.loads((EVO / "sealed" / "doc_labels" / f"{args.task}.json").read_text(encoding="utf-8"))["labels"]
    inv = json.loads(args.inventory.read_text(encoding="utf-8"))
    res = inventory_misses(labels, doc_paragraphs(args.task), inv.get("items") or [], args.milestone)
    text = json.dumps({"task": args.task, "milestone": args.milestone, "inventory_miss": res}, indent=1)
    assert_no_source_overlap(text, args.task)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(f"documented cases {len(res)}, inventory missed {sum(res.values())}")


if __name__ == "__main__":
    main()
