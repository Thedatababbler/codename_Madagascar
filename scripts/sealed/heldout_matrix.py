#!/usr/bin/env python3
"""The §6 metric table (author-evolution spec §5 item 5). Training tasks only; numbers and
case ids out, nothing else.

``census``: no evaluation suite needed. For every training milestone, on the final
repository of the main evolution run(s): attributed held-out cases, failures, how many of
them are documented / undocumented / unknown (``doc_label.py``), and whether the
milestone can carry a discrimination figure (retained candidates with different held-out
pass counts). This decides where cycle 0 spends author calls.

``metrics``: joins evaluation records (written by the replay of stage B4: an evaluation
suite's per-case results on retained workspaces, its tiers and symbols, inventory coverage,
cost, audit violations) with the sealed data -- held-out results cache, doc labels,
inventory misses, reference failures -- and prints the §6 metrics per milestone and
aggregated by split, acceptance with and without the tasks in ``report_without``.

    uv run python scripts/sealed/heldout_matrix.py census [--out docs/reports/heldout_census.md]
    uv run python scripts/sealed/heldout_matrix.py metrics <eval_records.json> [...] [--out <json>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import assert_no_source_overlap  # noqa: E402
from doc_label import doc_paragraphs  # noqa: E402
from heldout_results import attributed_cases, cache_path  # noqa: E402
from inventory_gap import inventory_misses  # noqa: E402
from orchestra.control.author.metrics import HeldoutCase, MilestoneInputs, SuiteCase, aggregate, milestone_metrics  # noqa: E402

EVO = Path(os.environ.get("ADAMAS_EVOLUTION_ROOT") or "outputs/evolution")
SPLIT = yaml.safe_load((ROOT / "configs" / "datasets" / "author_evolution_split.yaml").read_text(encoding="utf-8"))
PLANS = ROOT / "configs" / "datasets" / "cpe_feature_plans"


def labels_of(task: str) -> dict[str, dict]:
    p = EVO / "sealed" / "doc_labels" / f"{task}.json"
    return json.loads(p.read_text(encoding="utf-8"))["labels"] if p.is_file() else {}


def cached(task: str, milestone: str, workspace: str) -> dict | None:
    for key in (f"{workspace}#{milestone}", workspace):
        p = cache_path(task, key)
        if p.is_file():
            rec = json.loads(p.read_text(encoding="utf-8"))
            if rec.get("milestone") == milestone:
                return rec
    return None


def _base_id(node: str) -> str:
    """``unit_tests/test_x.py::Cls::test_y[#ab]`` -> ``test_x.py::test_y`` (the attribution key)."""
    parts = node.split("::")
    return f"{parts[0].split('/', 1)[-1]}::{parts[-1].split('[', 1)[0]}" if len(parts) >= 2 else node


def per_case(rec: dict | None) -> dict[str, str]:
    """Attribution-keyed pass/fail: a case fails when any of its parametrisations fails."""
    out: dict[str, str] = {}
    for node, v in (rec or {}).get("cases", {}).items():
        b = _base_id(node)
        out[b] = "fail" if v == "fail" or out.get(b) == "fail" else "pass"
    return out


def main_runs() -> dict[str, list[str]]:
    from orchestra.control.evolution.ledger import ledger_root_default, read_jsonl
    runs: dict[str, set[str]] = {}
    for f in ledger_root_default().glob("*/candidates.jsonl"):
        for r in read_jsonl(f):
            rd = str(r.get("run_dir") or "").rstrip("/")
            if "/cpe_evolution/" in rd:
                runs.setdefault(str(r["task_id"]).removeprefix("rb_"), set()).add(rd)
    return {t: sorted(v) for t, v in runs.items()}


def census(args) -> None:
    from orchestra.control.evolution.ledger import ledger_root_default, read_jsonl
    cands: dict[tuple[str, str], set[str]] = {}
    for f in ledger_root_default().glob("*/candidates.jsonl"):
        for r in read_jsonl(f):
            if r.get("workspace_ref"):
                cands.setdefault((str(r["task_id"]).removeprefix("rb_"), r["milestone_id"]), set()).add(r["workspace_ref"].rstrip("/"))
    runs = main_runs()
    rows = []
    for split in ("evolution", "acceptance"):
        for task in SPLIT[split]:
            labels = labels_of(task)
            plan = json.loads((PLANS / f"{task}.plan.json").read_text(encoding="utf-8"))
            for m in plan["milestones"]:
                mid = m["milestone_id"]
                attributed = attributed_cases(task, mid)
                best = None
                for rd in runs.get(task, []):
                    for fin in sorted(Path(rd).glob("tasks/*/canonical/repo")):
                        rec = cached(task, mid, str(fin))
                        if rec is None:
                            continue
                        res = per_case(rec)
                        fails = [c for c, v in res.items() if v == "fail"]
                        lab = Counter(labels.get(c, {}).get("label", "unknown") for c in fails)
                        row = {"run": Path(rd).parent.name, "fail": len(fails), "doc": lab["documented"], "undoc": lab["undocumented"], "unknown": lab["unknown"]}
                        best = row if best is None or row["doc"] > best["doc"] else best
                passes = set()
                for ws in cands.get((task, mid), set()):
                    rec = cached(task, mid, ws)
                    if rec is not None and rec.get("attributed"):
                        passes.add(sum(1 for v in per_case(rec).values() if v == "pass"))
                rows.append({"split": split, "task": task, "milestone": mid, "attributed": len(attributed),
                             "candidates": len(cands.get((task, mid), set())), "distinct_heldout_pass": len(passes),
                             "discrimination_computable": len(passes) >= 2, **(best or {"run": "", "fail": None, "doc": None, "undoc": None, "unknown": None})})
    for task in {r["task"] for r in rows}:
        assert_no_source_overlap(json.dumps([r for r in rows if r["task"] == task]), task)
    lines = ["# Held-out census of the training milestones (final repositories, no model calls)", "",
             "Per milestone, on the final repository of the main evolution run (for tinydb the run with more "
             "documented failures): attributed held-out cases, failures, and their documentation labels; "
             "discrimination is computable when retained candidates differ in held-out passes.", "",
             "| split | task | milestone | attributed | fail | documented | undocumented | unknown | candidates | distinct held-out | discrimination |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        f = lambda v: "-" if v is None else str(v)  # noqa: E731
        lines.append(f"| {r['split']} | {r['task']} | {r['milestone']} | {r['attributed']} | {f(r['fail'])} | {f(r['doc'])} | {f(r['undoc'])} | "
                     f"{f(r['unknown'])} | {r['candidates']} | {r['distinct_heldout_pass']} | {'yes' if r['discrimination_computable'] else 'no'} |")
    lines.append("")
    for split in ("evolution", "acceptance"):
        rs = [r for r in rows if r["split"] == split]
        for excl in ([], SPLIT.get("report_without") or []) if split == "acceptance" else ([],):
            sub = [r for r in rs if r["task"] not in excl]
            tag = f"{split}" + (f" without {', '.join(excl)}" if excl else "")
            lines.append(f"- {tag}: milestones {len(sub)}, with documented failures {sum(1 for r in sub if (r['doc'] or 0) > 0)}, "
                         f"failures but none documented {sum(1 for r in sub if (r['fail'] or 0) > 0 and not r['doc'])}, "
                         f"no failure {sum(1 for r in sub if r['fail'] == 0)}, unscored {sum(1 for r in sub if r['fail'] is None)}, "
                         f"discrimination computable {sum(1 for r in sub if r['discrimination_computable'])}")
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        args.out.with_suffix(".json").write_text(json.dumps(rows, indent=1), encoding="utf-8")


def reference_failures(task: str, suite: Path) -> list[str]:
    """Cached by suite content: a frozen suite never changes, and the reference run is the slow part."""
    import hashlib
    from reference_check import suite_on_reference
    h = hashlib.sha1()
    for p in sorted(suite.rglob("*.py")):
        if "__pycache__" not in p.parts:
            h.update(p.relative_to(suite).as_posix().encode())
            h.update(p.read_bytes())
    cache = EVO / "sealed" / "reference_results" / task / f"{h.hexdigest()[:16]}.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))["failed"]
    ref = suite_on_reference(task, suite)
    failed = sorted(k for k, v in ref.items() if v == "fail")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"suite": str(suite), "total": len(ref), "failed": failed}), encoding="utf-8")
    return failed


def metrics(args) -> None:
    rows = []
    for f in args.records:
        for rec in json.loads(Path(f).read_text(encoding="utf-8")):
            task, mid = rec["task"], rec["milestone"]
            labels = labels_of(task)
            attributed = attributed_cases(task, mid)
            sym = json.loads((EVO / "sealed" / "attribution" / f"{task}.json").read_text(encoding="utf-8")).get("case_symbols") or {}
            inv_miss: dict[str, bool] = {}
            if rec.get("inventory"):
                items = json.loads(Path(rec["inventory"]).read_text(encoding="utf-8")).get("items") or []
                inv_miss = inventory_misses(labels, doc_paragraphs(task), items, mid)
            held_cases = [HeldoutCase(c, labels.get(c, {}).get("label", "unknown"), frozenset(sym.get(c) or []), inv_miss.get(c))
                          for c in attributed]
            held_results = {}
            for ws in rec["suite_results"]:
                r = cached(task, mid, ws)
                if r is not None:
                    held_results[ws] = per_case(r)
            ref_failed = frozenset(rec.get("reference_failed") or [])
            if rec.get("reference_check", True) and "reference_failed" not in rec and rec.get("suite_dir"):
                ref_failed = frozenset(reference_failures(task, Path(rec["suite_dir"])))
            m = MilestoneInputs(
                task=task, milestone=mid, final=rec["final"],
                suite=[SuiteCase(c["case_id"], c.get("tier", "hard"), frozenset(c.get("symbols") or [])) for c in rec["suite_cases"]],
                reference_failed=ref_failed, suite_results=rec["suite_results"], heldout_cases=held_cases,
                heldout_results=held_results, inventory_hard_total=rec.get("inventory_hard_total"),
                inventory_hard_covered=rec.get("inventory_hard_covered"), cost=rec.get("cost") or {},
                audit_violations=int(rec.get("audit_violations") or 0))
            row = milestone_metrics(m)
            row["label"] = rec.get("label", "")
            row["sample"] = rec.get("sample", 0)
            row["split"] = "evolution" if task in SPLIT["evolution"] else ("acceptance" if task in SPLIT["acceptance"] else "other")
            assert_no_source_overlap(json.dumps(row), task)
            rows.append(row)
    summary = {}
    for split in ("evolution", "acceptance"):
        rs = [r for r in rows if r["split"] == split]
        summary[split] = aggregate(rs)
        if split == "acceptance":
            summary["acceptance_without"] = aggregate(rs, exclude_tasks=SPLIT.get("report_without") or [])
    out = {"rows": rows, "summary": summary}
    print(json.dumps(summary, indent=1))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out, indent=1), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("census")
    c.add_argument("--out", type=Path, default=None)
    c.set_defaults(fn=census)
    m = sub.add_parser("metrics")
    m.add_argument("records", nargs="+")
    m.add_argument("--out", type=Path, default=None)
    m.set_defaults(fn=metrics)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
