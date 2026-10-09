#!/usr/bin/env python3
"""Author fake-object fix: tests A1 (audit vs reference, memory replay) and A3 (accommodation check).

A1: every stored gate suite of the training tasks (deduplicated by content) goes through the new
audit rules (fake_io, env_path, call_assert). The sealed script gives each case's reference result
and failure category. Reports recall on reference-failing cases, false flags on reference-passing
ones, the 84 transfer-rejected records, and the unflagged reference failures by category.

    uv run python scripts/author_fix_tests.py a1
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "outputs" / "author_fix"
NEW_RULES = ("fake_io", "env_path", "call_assert", "fake_inject")
SPEC_RULES = ("fake_io", "env_path", "call_assert")


def train_tasks() -> list[str]:
    from orchestra.memory.store import load_config

    return list(load_config()["transfer"]["train_tasks"])


def dataset_root(task: str) -> Path:
    nl2 = Path("/root/codex-benchmarks/nl2repo/cpe_format")
    return nl2 if task.startswith("nl2_") and (nl2 / task).is_dir() else Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")


def packages(task: str) -> list[str]:
    import orchestra.control  # noqa: F401
    from orchestra.codeprojecteval.dataset import load_task
    from orchestra.codeprojecteval.harness import _top_level_packages, expected_modules

    return _top_level_packages(expected_modules(load_task(task, dataset_root=dataset_root(task))))


def unique_suites(task: str) -> list[tuple[str, Path, list[str]]]:
    """(content hash, one suite dir, milestone ids it served) per distinct gate suite of the task."""
    from orchestra.control.evolution.bank import suite_version_of
    from orchestra.memory.transfer import runs_of

    seen: dict[str, tuple[Path, set[str]]] = {}
    for run in runs_of(task):
        for d in sorted((run / "harness").glob("*.spec_tests")):
            if not any(d.rglob("test_*.py")):
                continue
            h = suite_version_of(d)
            seen.setdefault(h, (d, set()))[1].add(d.name.removesuffix(".spec_tests"))
    return [(h, d, sorted(m)) for h, (d, m) in sorted(seen.items())]


def norm_case(cid: str) -> str:
    """'spec_tests/sub/test_x.py::Cls::test_y' / 'test_x.py::test_y' -> 'test_x.py::[Cls::]test_y'."""
    cid = cid.split("spec_tests/", 1)[-1]
    head, _, rest = cid.partition("::")
    return f"{Path(head).name}::{rest}".split("[", 1)[0]


def test_refs(suite: Path) -> dict[str, set[str]]:
    """'file::[Cls::]test' -> names it references (calls, fixtures by parameter, attributes)."""
    out: dict[str, set[str]] = {}
    for p in suite.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except SyntaxError:
            continue
        owner = {id(m): c.name for c in tree.body if isinstance(c, ast.ClassDef) for m in c.body}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                key = f"{p.name}::{owner[id(fn)]}::{fn.name}" if id(fn) in owner else f"{p.name}::{fn.name}"
                names = {a.arg for a in fn.args.args}
                names |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                names |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
                out[key] = names
    return out


def helper_closure(suite: Path) -> dict[str, set[str]]:
    """helper/fixture name -> helper names it uses (one level suffices in practice; closed transitively)."""
    calls: dict[str, set[str]] = {}
    for p in suite.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn.name.startswith("test"):
                calls[fn.name] = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {a.arg for a in fn.args.args}
    return calls


def flagged_cases(suite: Path, violations: list) -> dict[str, list[str]]:
    refs = test_refs(suite)
    helpers = helper_closure(suite)
    by_helper: dict[str, set[str]] = collections.defaultdict(set)
    by_test: dict[str, set[str]] = collections.defaultdict(set)
    for v in violations:
        if v.rule not in NEW_RULES:
            continue
        if v.case.startswith("<"):
            by_helper[v.case.strip("<>")].add(v.rule)
        elif v.case:
            for k in refs:
                if k.split("::")[0] == v.file and k.split("::")[-1] == v.case:
                    by_test[k].add(v.rule)
    # helpers reached through other helpers
    changed = True
    while changed:
        changed = False
        for h, used in helpers.items():
            inherited = set().union(*(by_helper.get(u, set()) for u in used if u != h)) if used else set()
            if inherited - by_helper.get(h, set()):
                by_helper[h] |= inherited
                changed = True
    for k, names in refs.items():
        for h, rules in by_helper.items():
            if h in names:
                by_test[k] |= rules
    return {k: sorted(v) for k, v in by_test.items()}


def reference_cases(task: str, h: str, suite: Path) -> dict:
    out = OUT / "refcases" / task / f"{h}.json"
    if not out.is_file():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "sealed" / "reference_case_kinds.py"), "--task", task,
                        "--suite", str(suite), "--out", str(out)], capture_output=True, text=True, timeout=1800, cwd=str(ROOT))
    return json.loads(out.read_text()) if out.is_file() else {"total": 0, "cases": {}}


def a1() -> dict:
    from orchestra.codeprojecteval.public_symbols import load_docs
    from orchestra.codeprojecteval.suite_audit import audit_suite
    from orchestra.memory.store import read_yaml_list

    rows = []
    for task in train_tasks():
        docs = load_docs(dataset_root(task) / task / "docs")
        pk = packages(task)
        for h, suite, mids in unique_suites(task):
            res = audit_suite(suite, docs, packages=pk, fake_object_audit=True)
            flags = flagged_cases(suite, res.violations)
            ref = reference_cases(task, h, suite)
            for cid, info in (ref.get("cases") or {}).items():
                k = norm_case(cid)
                rows.append({"task": task, "suite": h, "milestones": mids, "case": k, "ref": info["ref"], "kind": info.get("kind"),
                             "exc": info.get("exc"), "flags": flags.get(k, [])})
    fail = [r for r in rows if r["ref"] == "fail"]
    ok = [r for r in rows if r["ref"] == "pass"]
    caught = [r for r in fail if r["flags"]]
    false = [r for r in ok if r["flags"]]
    # the 84 transfer records rejected because the case fails on the reference
    root = ROOT / "memory"
    sources = {s["record_id"]: s for s in read_yaml_list(root / "transfer" / "sources.yaml")}
    rej = [r for r in read_yaml_list(root / "transfer" / "rejected.yaml") if r.get("stage") == "reference"]
    rej_cases = {(sources[r["record_id"]]["task"], sources[r["record_id"]]["milestone"], sources[r["record_id"]]["case"])
                 for r in rej if r.get("record_id") in sources}
    def hit(t, m, c):
        cands = [r for r in rows if r["task"] == t and m in r["milestones"] and r["case"].split("::")[-1] == c]
        return any(r["flags"] for r in cands), bool(cands)
    rej_hits = [hit(*k) for k in rej_cases]
    missed = collections.Counter(r["kind"] for r in fail if not r["flags"])
    res = {
        "suites": len({(r["task"], r["suite"]) for r in rows}), "cases": len(rows), "reference_fail": len(fail), "reference_pass": len(ok),
        "recall": round(len(caught) / len(fail), 3) if fail else None, "caught": len(caught),
        "recall_spec_rules_only": round(sum(1 for r in fail if set(r["flags"]) & set(SPEC_RULES)) / len(fail), 3) if fail else None,
        "false_flags_spec_rules_only": sum(1 for r in ok if set(r["flags"]) & set(SPEC_RULES)),
        "recall_by_kind": {k: f"{sum(1 for r in fail if r['kind'] == k and r['flags'])}/{sum(1 for r in fail if r['kind'] == k)}"
                           for k in sorted({r["kind"] for r in fail})},
        "false_flags": len(false), "false_flag_rate": round(len(false) / len(ok), 3) if ok else None,
        "false_flags_by_rule": dict(collections.Counter(f for r in false for f in r["flags"])),
        "false_flag_examples": [{"task": r["task"], "case": r["case"], "rules": r["flags"]} for r in false[:5]],
        "transfer_rejected_cases": len(rej_cases), "transfer_rejected_found_in_suites": sum(1 for _, found in rej_hits if found),
        "transfer_rejected_flagged": sum(1 for flagged, _ in rej_hits if flagged),
        "missed_by_kind": dict(missed), "by_task": {},
    }
    for t in sorted({r["task"] for r in rows}):
        f = [r for r in fail if r["task"] == t]
        res["by_task"][t] = {"ref_fail": len(f), "caught": sum(1 for r in f if r["flags"]),
                             "false_flags": sum(1 for r in ok if r["task"] == t and r["flags"])}
    (OUT / "a1_rows.json").write_text(json.dumps(rows, indent=1))
    return res


def a3() -> dict:
    """§6.4 on every stored committed repair of the training tasks; reference failure as the yardstick."""
    import glob

    from orchestra.control.evolution.bank import suite_version_of
    from orchestra.control.fast_loop.accommodation import judge
    from orchestra.memory.transfer import _cached_call, _norm_case, _patch_of, failure_section, runs_of
    from orchestra.settings import load_env_file

    load_env_file(ROOT / ".env")
    from orchestra.control.evolution.evolver import default_call

    rows_a1 = json.loads((OUT / "a1_rows.json").read_text())
    truth = {(r["task"], r["suite"], r["case"]): r["ref"] for r in rows_a1}
    call = lambda sys_, prompt: _cached_call(default_call, sys_, prompt, tag="accom")  # noqa: E731
    out_rows, n_rep = [], 0
    for task in train_tasks():
        for run in runs_of(task):
            for te in glob.glob(str(run / "tasks" / "*" / "task_execution.json")):
                d = json.loads(Path(te).read_text())
                for mid, v in (d.get("fast_loop_states") or {}).items():
                    inc = next((c for c in v["candidates"] if c["candidate_id"] == "incumbent_first_pass"), None)
                    if not inc:
                        continue
                    suite = run / "harness" / f"{mid}.spec_tests"
                    h = suite_version_of(suite) if suite.is_dir() else ""
                    for c in v["candidates"]:
                        if c.get("status") != "committed" or c["candidate_id"] == "incumbent_first_pass" or "feedback" in c["candidate_id"]:
                            continue
                        fixed = sorted({_norm_case(x) for x in inc.get("behaviour_failures") or []}
                                       - {_norm_case(x) for x in c.get("behaviour_failures") or []})
                        if not fixed:
                            continue
                        patch = _patch_of(run, mid, c["candidate_id"])
                        if not patch:
                            continue
                        n_rep += 1
                        ev = (c.get("metadata") or {}).get("repair_evidence")
                        fails = {k: failure_section(Path(ev) / "failures.md", k.split("::")[-1]) for k in fixed} if ev else {}
                        j = judge(patch, fixed, failures=fails, call=call)
                        for k in fixed:
                            key = (task, h, norm_case(k))
                            out_rows.append({"task": task, "run": str(run.relative_to(ROOT)), "milestone": mid, "candidate": c["candidate_id"],
                                             "case": k, "predicted": k in j.accommodated, "verdict": j.verdict, "rule": j.rule,
                                             "ref": truth.get(key)})
    known = [r for r in out_rows if r["ref"] in ("pass", "fail")]
    tp = sum(1 for r in known if r["predicted"] and r["ref"] == "fail")
    fp = sum(1 for r in known if r["predicted"] and r["ref"] == "pass")
    fn = sum(1 for r in known if not r["predicted"] and r["ref"] == "fail")
    res = {"repairs": n_rep, "fixed_cases": len(out_rows), "with_reference_result": len(known),
           "precision": round(tp / (tp + fp), 3) if tp + fp else None, "recall": round(tp / (tp + fn), 3) if tp + fn else None,
           "tp": tp, "fp": fp, "fn": fn, "verdicts": dict(collections.Counter(r["verdict"] for r in out_rows)),
           "false_positive_examples": [{k: r[k] for k in ("task", "milestone", "case", "rule")} for r in known if r["predicted"] and r["ref"] == "pass"][:6],
           "false_negative_examples": [{k: r[k] for k in ("task", "milestone", "case", "rule")} for r in known if not r["predicted"] and r["ref"] == "fail"][:6]}
    (OUT / "a3_rows.json").write_text(json.dumps(out_rows, indent=1))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("test", choices=["a1", "a3"])
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    res = {"a1": a1, "a3": a3}[a.test]()
    (OUT / f"{a.test}.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1)[:5000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
