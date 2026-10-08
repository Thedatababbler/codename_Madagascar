#!/usr/bin/env python3
"""First-pass design from repair experience -- preparation (user's spec of 2026-10-08, §3-§4). Memory replay only.

    uv run python scripts/fp_from_repair.py prepare --out outputs/fp_from_repair/prep.json

Repair experience = every committed repair candidate (default repair / continuation, playbook rows, node
resample; not probes) in the stored runs of the evolution tasks, read from each run's search state: the
milestone's features, the first run's gate failures (classified), what the repair fixed and broke on the gate,
what kind of repair it was, a diff summary, the cited sentences of the fixed cases and the cost. Clusters are
(position group x size group x error class); usefulness = milestones x mean stable fixes - total regressions.
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CPE_ROOT = Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
NL2_ROOT = Path("/root/codex-benchmarks/nl2repo/cpe_format")
TASKS = {
    "cookiecutter": {"group": "evolution", "plan": "configs/datasets/cpe_feature_plans/cookiecutter.plan.json", "data": CPE_ROOT,
                     "checkpoint_run": "outputs/cpe_evolution/cpe-20261001T021833Z-cookiecutter/cookiecutter"},
    "imapclient": {"group": "evolution", "plan": "configs/datasets/cpe_feature_plans/imapclient.plan.json", "data": CPE_ROOT,
                   "checkpoint_run": "outputs/cpe_evolution/cpe-20261001T055006Z-imapclient/imapclient"},
    "nl2_python-jose": {"group": "acceptance", "plan": "configs/datasets/nl2repo_feature_plans/nl2_python-jose.plan.json", "data": NL2_ROOT,
                        "checkpoint_run": "outputs/cpe_milestones/cpe-20260916T135414Z-nl2_python-jose/nl2_python-jose"},
    "nl2_tablib": {"group": "acceptance", "plan": "configs/datasets/nl2repo_feature_plans/nl2_tablib.plan.json", "data": NL2_ROOT,
                   "checkpoint_run": "outputs/cpe_milestones/cpe-20260916T160528Z-nl2_tablib/nl2_tablib"},
}


def runs_of(task: str) -> list[Path]:
    pats = [f"outputs/*/cpe-*-{task}/{task}", f"outputs/*/cpe-*/{task}", f"outputs/evolution/reruns/*{task}*/{task}"]
    out = set()
    for p in pats:
        for d in glob.glob(str(ROOT / p)):
            if glob.glob(d + "/tasks/*/task_execution.json"):
                out.add(Path(d))
    return sorted(out)


def features(task: str) -> dict[str, dict]:
    from orchestra.control.first_pass.features import milestone_features
    from orchestra.realbench.milestone_planner import parse_plan_payload
    spec = TASKS[task]
    plan = parse_plan_payload(json.loads((ROOT / spec["plan"]).read_text()), max_agents=6, max_milestones=12)
    docs = "\n\n".join(p.read_text(errors="replace") for p in sorted((spec["data"] / task / "docs").glob("*")) if p.is_file())
    out = {}
    n = len(plan.milestones)
    for i, m in enumerate(plan.milestones):
        f = milestone_features(m, index=i, total=n, docs_text=docs)
        f["position"] = i + 1
        f["n_milestones"] = n
        f["position_group"] = "first" if i == 0 else ("last" if i == n - 1 else "middle")
        out[m.milestone_id] = f
    return out


def suite_sources(suite: Path) -> dict[str, tuple[str, list[str]]]:
    from orchestra.codeprojecteval.suite_audit import CITATION_RE
    out = {}
    for p in suite.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        src = p.read_text(errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                start = min([fn.lineno] + [d.lineno for d in fn.decorator_list]) - 1
                j = start - 1
                while j >= 0 and lines[j].strip().startswith("#"):
                    j -= 1
                body = "\n".join(lines[j + 1: fn.end_lineno])
                out[fn.name] = (body, [m.group("quote") for m in CITATION_RE.finditer(body)])
    return out


def case_name(case: str) -> str:
    return case.split("::")[-1].split("[")[0]


def classify(case: str, src: dict) -> str:
    from orchestra.control.fast_loop.error_classes import CaseFacts, classify_case
    body = src.get(case_name(case), ("", []))[0]
    return classify_case(CaseFacts(case_id=case, key=case, name=case_name(case), source=body))


def kind_of(cid: str, meta: dict) -> tuple[str, str] | None:
    if "feedback" in cid or cid == "incumbent_first_pass" or meta.get("probe"):
        return None
    k = meta.get("candidate_kind")
    if cid == "cand_R0" or k == "R0" or "continue_improve" in cid:
        return "default repair (continuation with the failure list)", "R0"
    if "node_resample" in cid or k == "resample":
        return "node resample (the blamed writer re-run with the evidence)", "resample"
    row = meta.get("v2_row") or cid.removeprefix("cand_").removeprefix("v2:").removeprefix("pb_")
    return f"playbook row {row}", "row"


def diff_stats(patch: str) -> dict:
    files = re.findall(r"^diff --git a/(\S+)", patch or "", re.M)
    add = sum(1 for ln in (patch or "").splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    rem = sum(1 for ln in (patch or "").splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return {"files": len(files), "added": add, "removed": rem, "test_files": sum(1 for f in files if "test" in f)}


def experiences(task: str) -> list[dict]:
    feats = features(task)
    out = []
    for run in runs_of(task):
        f = glob.glob(str(run / "tasks/*/task_execution.json"))[0]
        d = json.loads(Path(f).read_text())
        for mid, v in (d.get("fast_loop_states") or {}).items():
            cands = v.get("candidates") or []
            inc = next((c for c in cands if c["candidate_id"] == "incumbent_first_pass"), None)
            if inc is None or mid not in feats:
                continue
            inc_fail = set(inc.get("behaviour_failures") or [])
            suite = run / "harness" / f"{mid}.spec_tests"
            src = suite_sources(suite) if suite.is_dir() else {}
            for c in cands:
                if c.get("status") != "committed":
                    continue
                k = kind_of(c["candidate_id"], c.get("metadata") or {})
                if k is None:
                    continue
                fail = set(c.get("behaviour_failures") or [])
                passed = set(c.get("behaviour_passed") or [])
                fixed = sorted(x for x in inc_fail if x not in fail and (not passed or x in passed or case_name(x) in {case_name(p) for p in passed}))
                inc_passed = set(inc.get("behaviour_passed") or [])
                regressed = sorted(x for x in fail if x not in inc_fail and (x in inc_passed or not inc_passed))
                classes = defaultdict(int)
                for x in fixed:
                    classes[classify(x, src)] += 1
                first_classes = defaultdict(int)
                for x in inc_fail:
                    first_classes[classify(x, src)] += 1
                cites = []
                for x in fixed:
                    for q in src.get(case_name(x), ("", []))[1]:
                        if q not in cites:
                            cites.append(q)
                cost = c.get("cost") or {}
                out.append({"task": task, "milestone": mid, "run": run.parent.name, "features": feats[mid],
                            "repair": k[0], "repair_kind": k[1], "fixed": len(fixed), "regressed": len(regressed),
                            "fixed_classes": dict(classes), "first_run_failures": dict(first_classes),
                            "cited_sentences": cites[:10], "diff": diff_stats(c.get("patch") or ""),
                            "cost": {"prompt_tokens": cost.get("prompt_tokens"), "calls": cost.get("backend_calls")}})
    return out


def clusters(exps: list[dict], *, k: int = 3, min_milestones: int = 2) -> list[dict]:
    sizes = [e["features"]["n_focus_files"] for e in exps]
    med = statistics.median(sizes) if sizes else 0
    groups: dict[tuple, list] = defaultdict(list)
    for e in exps:
        size = "small" if e["features"]["n_focus_files"] <= med else "large"
        for cls, n in e["fixed_classes"].items():
            groups[(e["features"]["position_group"], size, cls)].append((e, n))
    out = []
    for key, items in groups.items():
        ms = {(e["task"], e["milestone"]) for e, _ in items}
        mean_fix = statistics.fmean(n for _, n in items)
        reg = sum(e["regressed"] for e, _ in items)
        out.append({"position_group": key[0], "size_group": key[1], "error_class": key[2], "milestones": len(ms),
                    "experiences": len(items), "mean_stable_fixes": round(mean_fix, 2), "regressions": reg,
                    "usefulness": round(len(ms) * mean_fix - reg, 2), "eligible": len(ms) >= min_milestones,
                    "repairs": dict(sorted(defaultdict(int, {r: sum(1 for e, _ in items if e["repair_kind"] == r) for r in ("R0", "row", "resample")}).items())),
                    "milestone_list": sorted(f"{t}:{m}" for t, m in ms), "size_median": med})
    out.sort(key=lambda c: -c["usefulness"])
    return out


def prepare(args) -> None:
    rel = {}
    for t in TASKS:
        a = json.loads((ROOT / "outputs/evolution/sealed/attribution" / f"{t}.json").read_text())
        rel[t] = {m: len(v) for m, v in (a.get("relevant") or {}).items()}
    exps = [e for t, s in TASKS.items() if s["group"] == "evolution" for e in experiences(t)]
    cl = clusters(exps)
    cover = []
    for t, s in TASKS.items():
        feats = features(t)
        run = ROOT / s["checkpoint_run"]
        for mid, f in feats.items():
            cover.append({"task": t, "group": s["group"], "milestone": mid, "position": f["position"], "position_group": f["position_group"],
                          "frozen_suite": (run / "harness" / f"{mid}.spec_tests").is_dir(), "relevant_heldout": rel[t].get(mid, 0),
                          "n_focus_files": f["n_focus_files"]})
    verif = {}
    for label in ("A1", "V"):
        man = ROOT / "outputs/author_eval" / label / "suites.s0.json"
        if man.is_file():
            for e in json.loads(man.read_text()):
                if e.get("suite_dir"):
                    verif[(e["task"], e["milestone"])] = label
    for c in cover:
        c["verification_suite"] = verif.get((c["task"], c["milestone"]), "")
    out = {"experiences": exps, "clusters": cl, "coverage": cover}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, default=str))
    print(f"experiences {len(exps)}; clusters {len(cl)}, eligible {sum(c['eligible'] for c in cl)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--out", default="outputs/fp_from_repair/prep.json")
    p.set_defaults(fn=prepare)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
