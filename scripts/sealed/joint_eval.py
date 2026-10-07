#!/usr/bin/env python3
"""Evaluate the joint experiment's runs (sealed: reads held-out results; writes numbers and our own
suites' case ids only). For every finished job:

first-run job -- the first-run repository is graded on the inherited gate suite (hard cases, one-pass =
all pass), on the milestone's v10.1 verification suite (cases the reference passes), and on held-out:
the milestone's attributed subset where it has one, and the whole suite ("*"), whose paired difference
is the milestone's effect because later milestones' cases fail in both arms alike. The original run's
first run is graded the same way as a third reference value.

repair job -- the seeded incumbent, R0 and the row are graded the same way; nets are against the
incumbent; the pair is "normal" when the incumbent has gate failures, "fallback" when it has none.

Also: the content hash of the gate suite each run used (F0 and F7 must match), the run's prompt tokens,
and a scan of the agent traces for any access to verification suites, held-out tests or the dataset.

    uv run python scripts/sealed/joint_eval.py --round 1
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts"))

import heldout_results as hr  # noqa: E402
import joint_experiment as J  # noqa: E402
from _guard import assert_no_source_overlap  # noqa: E402
from author_eval import ENV_ROOT, run_suite  # noqa: E402
from heldout_matrix import per_case, reference_failures  # noqa: E402
from orchestra.control.evolution.bank import suite_version_of  # noqa: E402

TRACE_PATTERNS = re.compile(r"author_probe|author_eval|/unit_tests|joint_20261007/(?:verif|inventory)|codex-benchmarks/projectgen")


def verifier_suite(task: str, mid: str) -> Path | None:
    for label in ("V", "A1"):
        man = ROOT / "outputs" / "author_eval" / label / "suites.s0.json"
        if man.is_file():
            for e in json.loads(man.read_text()):
                if e["task"] == task and e["milestone"] == mid and e.get("suite_dir"):
                    return Path(e["suite_dir"])
    return None


def grade(task: str, mid: str, ws: Path, gate_suite: Path, verifier: Path | None) -> dict:
    py = ENV_ROOT / task / "bin" / "python"
    gate = run_suite(ws, gate_suite, py)
    out = {"gate_total": len(gate), "gate_failed": sorted(k for k, v in gate.items() if v == "fail")}
    out["one_pass"] = bool(gate) and not out["gate_failed"]
    if verifier is not None:
        invalid = set(reference_failures(task, verifier))
        v = {k: x for k, x in run_suite(ws, verifier, py).items() if k not in invalid}
        out["verifier_total"] = len(v)
        out["verifier_failed"] = sorted(k for k, x in v.items() if x == "fail")
    for scope in (mid, "*"):
        if not hr.attributed_cases(task, scope):
            continue
        rec = hr.results_for(task, scope, str(ws))
        cases = per_case(rec)
        key = "heldout_attr" if scope == mid else "heldout_all"
        out[f"{key}_total"] = len(cases)
        out[f"{key}_passed"] = sum(1 for x in cases.values() if x == "pass")
        out[f"_{key}_cases"] = cases            # kept in memory for nets, never written
    return out


def net(a: dict, b: dict, key: str) -> dict | None:
    """Fixed minus broken from ``a`` (incumbent) to ``b`` on a case-level result set."""
    if f"_{key}_cases" not in a or f"_{key}_cases" not in b:
        return None
    x, y = a[f"_{key}_cases"], b[f"_{key}_cases"]
    fixed = sum(1 for k, v in x.items() if v == "fail" and y.get(k) == "pass")
    broken = sum(1 for k, v in x.items() if v == "pass" and y.get(k) == "fail")
    return {"fixed": fixed, "broken": broken, "net": fixed - broken}


def list_net(a: list, b: list, total_keys: set) -> dict:
    fa, fb = set(a), set(b)
    return {"fixed": len(fa - fb), "broken": len(fb - fa), "net": len(fa - fb) - len(fb - fa)}


def tokens(run_dir: Path, mid: str) -> int | None:
    s = run_dir / "summary.json"
    if not s.is_file():
        return None
    for o in json.loads(s.read_text()).get("milestone_objectives") or []:
        if o.get("milestone_id") == mid:
            return int(o.get("prompt_tokens") or 0)
    return None


def trace_hits(run_dir: Path) -> int:
    hits = 0
    for f in run_dir.glob("tasks/*/backend_traces/**/*.json"):
        hits += len(TRACE_PATTERNS.findall(f.read_text(encoding="utf-8", errors="replace")))
    return hits


def public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=1)
    args = ap.parse_args()
    jobs = json.loads((J.OUT / f"round{args.round}" / "jobs.json").read_text())
    out_path = J.OUT / f"round{args.round}" / "eval.json"
    results = json.loads(out_path.read_text()) if out_path.is_file() else {}
    refs: dict = {}
    for j in jobs:
        if j["status"] not in ("done", "failed", "stopped_by_budget") or not j.get("run_dir") or j["job_id"] in results:
            continue
        run_dir = Path(j["run_dir"])
        task, mid = j["task"], j["milestone"]
        orig_suite = ROOT / J.RUNS[task] / "harness" / f"{mid}.spec_tests"
        gate_suite = run_dir.parent.parent / j["job_id"] / task / "harness" / f"{mid}.spec_tests"
        if not gate_suite.is_dir():
            gate_suite = run_dir / "harness" / f"{mid}.spec_tests"
        ver = verifier_suite(task, mid)
        rec = {"job_id": j["job_id"], "part": j["part"], "task": task, "milestone": mid, "variant": j["variant"], "rep": j["rep"],
               "status": j["status"], "suite_hash": suite_version_of(gate_suite) if gate_suite.is_dir() else "",
               "suite_hash_original": suite_version_of(orig_suite), "tokens": tokens(run_dir, mid), "trace_hits": trace_hits(run_dir),
               "verifier": str(ver) if ver else ""}
        if j["part"] == "first_run":
            wss = sorted(run_dir.glob(f"tasks/rb_*/workspaces/{mid}/repo"))
            if not wss or not gate_suite.is_dir():
                rec["error"] = "no first-run workspace or gate suite"
            else:
                g = grade(task, mid, wss[0], gate_suite, ver)
                rec.update(public(g))
            key = (task, mid)
            if key not in refs:
                refs[key] = public(grade(task, mid, Path(J.first_run_workspace(task, mid)), orig_suite, ver))
            rec["historical_first_run"] = refs[key]
        else:
            seed = Path(j["env"]["ADAMAS_SEED_FIRST_RUN"])
            inc = grade(task, mid, seed, orig_suite, ver)
            rec["incumbent"] = public(inc)
            rec["pair_type"] = "normal" if inc["gate_failed"] else "fallback"
            cands = {}
            for cand in sorted(run_dir.glob(f"tasks/rb_*/subtasks/{mid}/candidates/*/repo")):
                name = cand.parent.name
                kind = "R0" if name == "cand_R0" else ("row" if name.startswith(("cand_v2", "cand_pb_")) else name)
                g = grade(task, mid, cand, orig_suite, ver)
                c = public(g)
                c["kind"] = kind
                c["gate_net"] = list_net(inc["gate_failed"], g["gate_failed"], set())
                if "verifier_failed" in g:
                    c["verifier_net"] = list_net(inc["verifier_failed"], g["verifier_failed"], set())
                for key in ("heldout_attr", "heldout_all"):
                    n = net(inc, g, key)
                    if n:
                        c[f"{key}_net"] = n
                cands[name] = c
            rec["candidates"] = cands
        text = json.dumps(rec)
        assert_no_source_overlap(text, task)
        results[j["job_id"]] = rec
        out_path.write_text(json.dumps(results, indent=1))
        print(f"evaluated {j['job_id']}")
    print(f"{len(results)} job(s) evaluated -> {out_path}")


if __name__ == "__main__":
    main()
