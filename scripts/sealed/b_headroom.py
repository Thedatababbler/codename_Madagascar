#!/usr/bin/env python3
"""How much of the held-out B class (documents state the behaviour, not the asserted detail) is done right, and how
much room is left in it (sealed; 2026-10-16). Reads stored runs only; starts no solving agent.

Scope: the NL2Repo Medium runs of the ceiling decomposition (outputs/nl2_ceiling, suspicious and incomplete runs
excluded; voluptuous -- whole-suite collection failure -- listed apart), and the CPE sandbox baseline batch (one clean
run per task). Every held-out case gets the three-level script label (A/B/C/U); a sealed judge (gpt-5.6-luna, capped
by this script) re-labels 40 cases per task stratified by test file and by outcome, and for B cases that fail in every
run of a multi-run task, says whether common Python or domain practice would likely get them right.

Only hashed case ids, labels, pass/fail and numbers leave this script; the judge's prompts and replies go to the
sealed log directory.

    uv run python scripts/sealed/b_headroom.py prepare      # labels + per-run outcomes for every task
    uv run python scripts/sealed/b_headroom.py judge [--cap 2.0]
    uv run python scripts/sealed/b_headroom.py convention [--cap 2.0]
    uv run python scripts/sealed/b_headroom.py summary
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard as G  # noqa: E402
import nl2_ceiling as NC  # noqa: E402

OUT = ROOT / "outputs" / "b_headroom"
SEALED_LOG = Path("/root/.adamas_sealed/b_headroom_judge")
LETTER = {"detail_specified": "A", "behaviour_only": "B", "not_specified": "C", "unknown": "U"}
CPE_RUNS = {"deprecated": "cpe-20261012T050337Z-deprecated", "csvs-to-sqlite": "cpe-20261012T053045Z-csvs-to-sqlite",
            "rsa": "cpe-20261012T064502Z-rsa", "djangorestframework-simplejwt": "cpe-20261012T081623Z-djangorestframework-simplejwt",
            "voluptuous": "cpe-20261012T101048Z-voluptuous", "zxcvbn": "cpe-20261012T140933Z-zxcvbn",
            "python-hl7": "cpe-20261012T165502Z-python-hl7"}
APART = {"nl2_voluptuous": "whole held-out suite fails to collect in both runs"}

CONVENTION_RULES = """For each test below, the design documents describe the behaviour it checks but not the exact detail
it asserts, and every implementation built from the documents so far has failed it.
Answer with exactly one letter per test:
L - likely: an implementer who follows common Python conventions or the usual practice of this domain would very
    probably produce exactly what the test asserts;
N - not likely: the asserted detail is a specific choice the documents do not fix and conventions do not settle;
X - cannot tell.
Reply with one JSON object and nothing else, mapping each test id to its letter, e.g. {"t01": "N", "t02": "L"}."""


def hid(case: str) -> str:
    return "c" + hashlib.sha1(case.encode()).hexdigest()[:12]


def _write(path: Path, data, task: str) -> None:
    text = json.dumps(data, indent=1, sort_keys=True)
    G.assert_no_source_overlap(text, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cpe_labels(task: str) -> dict[str, str]:
    import doc_label
    import heldout_attribution as HA

    root = G.dataset_root_for(task)
    HA.DATASET_ROOT = root
    doc_label.DATASET_ROOT = root
    cfg = G.task_config(task)
    packages = [str(cfg.get("source_code") or task).removeprefix("src/").split("/")[0]]
    syms = HA.case_symbols(task, packages)
    keys = NC.heldout_case_keys(task)
    plan = ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{task}.plan.json"
    attribution = HA.attribute(task, plan) if plan.is_file() else {}
    seen = {c for cs in attribution.values() for c in cs}
    attr = dict(attribution)
    attr["__unattributed__"] = [k for k in keys if k not in seen]
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / f"{task}.json").write_text(json.dumps({"attribution": attr, "case_symbols": syms}))
        doc_label.ATTRIBUTION_ROOT = Path(tmp)
        labels = doc_label.label_task(task)
    return {k: LETTER.get(v["depth"], "U") for k, v in labels.items()}


def cmd_prepare(_a) -> None:
    # NL2Repo Medium: labels and outcomes from the ceiling decomposition
    runs = [r for r in json.loads((ROOT / "outputs" / "nl2_ceiling" / "runs.json").read_text()) if r["included"]]
    for task in sorted({r["task"] for r in runs}):
        lab = json.loads((ROOT / "outputs" / "nl2_ceiling" / "labels" / f"{task}.json").read_text())["labels"]
        keys = NC.heldout_case_keys(task)
        outcomes = [json.loads((ROOT / "outputs" / "nl2_ceiling" / "cases" / f"{r['batch']}.json").read_text())["cases"]
                    for r in runs if r["task"] == task]
        _store(task, "nl2_medium", keys, {k: LETTER.get((lab.get(k) or {}).get("depth", "unknown"), "U") for k in keys}, outcomes)
    # CPE sandbox baseline: labels computed here, one run per task scored here
    for task, batch in CPE_RUNS.items():
        canon = next(iter((ROOT / "outputs" / "cpe_sandbox_baseline" / batch / task).glob("tasks/*/canonical/repo")))
        res = NC.run_heldout_cases(task, canon)
        keys = NC.heldout_case_keys(task)
        labels = _cpe_labels(task)
        _store(task, "cpe", keys, {k: labels.get(k, "U") for k in keys}, [res["cases"]])


def _store(task: str, group: str, keys: list[str], labels: dict[str, str], outcomes: list[dict[str, str]]) -> None:
    rec = {"task": task, "group": group, "runs": len(outcomes), "cases": {}}
    for k in keys:
        res = [o.get(k, "fail") for o in outcomes]
        rec["cases"][hid(k)] = {"script": labels.get(k, "U"), "results": res, "file": hid(k.split("::", 1)[0])}
    _write(OUT / "tasks" / f"{task}.json", rec, task)
    c = Counter(v["script"] for v in rec["cases"].values())
    print(task, group, "runs", len(outcomes), "cases", len(keys), dict(c), flush=True)


def _real_keys(task: str) -> dict[str, str]:
    return {hid(k): k for k in NC.heldout_case_keys(task)}


def sample(task: str, rec: dict, n: int = 40, seed: int = 20261016) -> list[str]:
    """n hashed ids: half from cases passing in some run, half from cases failing in some run, each by test file."""
    rng = random.Random(f"{seed}:{task}")
    cases = rec["cases"]
    passing = sorted(h for h, v in cases.items() if "pass" in v["results"])
    failing = sorted(h for h, v in cases.items() if "fail" in v["results"] and h not in set(passing))
    def by_file(pool: list[str], k: int) -> list[str]:
        if len(pool) <= k:
            return list(pool)
        strata: dict[str, list[str]] = defaultdict(list)
        for h in pool:
            strata[cases[h]["file"]].append(h)
        quota = {f: max(1, round(k * len(v) / len(pool))) for f, v in strata.items()}
        while sum(quota.values()) > k:
            quota[max(quota, key=lambda f: quota[f])] -= 1
        while sum(quota.values()) < k:
            quota[max(strata, key=lambda f: len(strata[f]) - quota[f])] += 1
        out = []
        for f, v in sorted(strata.items()):
            out += rng.sample(sorted(v), min(len(v), quota[f]))
        return out
    half = n // 2
    a = by_file(passing, half)
    b = by_file(failing, n - len(a))
    if len(a) + len(b) < n:
        rest = [h for h in passing if h not in set(a)]
        a += rng.sample(rest, min(len(rest), n - len(a) - len(b)))
    return sorted(set(a + b))


def _judge_loop(a, kind: str) -> None:
    import joint_experiment as JE
    from quota_spend import Spend

    SEALED_LOG.mkdir(parents=True, exist_ok=True)
    JE.read_window = NC._window_reader(a.model)
    spend = Spend()
    res_p = OUT / f"{kind}.json"
    state = json.loads(res_p.read_text()) if res_p.is_file() else {"labels": {}, "calls": [], "window_before": spend.w0}
    tokens_all = sum(int(c.get("tokens") or 0) for p in OUT.glob("*.json") if p.stem in ("judge", "convention")
                     for c in json.loads(p.read_text()).get("calls", []))
    reuse = json.loads((ROOT / "outputs" / "nl2_ceiling" / "judge.json").read_text())["labels"] if kind == "judge" else {}
    for f in sorted((OUT / "tasks").glob("*.json")):
        rec = json.loads(f.read_text())
        task = rec["task"]
        real = _real_keys(task)
        if kind == "judge":
            todo_h = sample(task, rec)
            for h in todo_h:                                   # the ceiling decomposition's judge saw some of them
                if real[h] in reuse.get(task, {}) and h not in state["labels"].get(task, {}):
                    state["labels"].setdefault(task, {})[h] = reuse[task][real[h]]
                    state.setdefault("reused", 0)
                    state["reused"] += 1
        else:
            if rec["runs"] < 2 or task in APART:
                continue
            jl = json.loads((OUT / "judge.json").read_text())["labels"].get(task, {})
            bfail = sorted(h for h, v in rec["cases"].items()
                           if (jl.get(h) or v["script"]) == "B" and all(r == "fail" for r in v["results"]))
            todo_h = random.Random(f"conv:{task}").sample(bfail, min(10, len(bfail)))
        todo = [real[h] for h in todo_h if h not in state["labels"].get(task, {})]
        docs = NC._docs_text(task)
        for i in range(0, len(todo), a.batch):
            if spend.poll() >= a.cap or tokens_all >= int(a.cap * 2_000_000):
                state["stopped"] = f"cap reached: {spend.used()} points read, {tokens_all} tokens (both stages)"
                _write(res_p, state, task)
                print(state["stopped"])
                return
            chunk = todo[i:i + a.batch]
            ids = {f"t{j + 1:02d}": c for j, c in enumerate(chunk)}
            tests = "\n\n".join(f"### test {tid}\n```python\n{NC._test_source(task, c)}\n```" for tid, c in ids.items())
            rules = NC.JUDGE_RULES if kind == "judge" else CONVENTION_RULES
            user = f"{rules}\n\n## Design documents\n\n{docs}\n\n## Tests\n\n{tests}"
            try:
                text, usage = NC._call(a.model, NC.JUDGE_SYSTEM, user)
            except Exception as exc:  # noqa: BLE001 -- stop at the first failure (proxy failure counter)
                detail = exc.read().decode(errors="replace")[:300] if hasattr(exc, "read") else ""
                state["stopped"] = f"call failure: {type(exc).__name__} {detail[:200]}"
                _write(res_p, state, task)
                print(task, state["stopped"])
                return
            tok = int(usage.get("total_tokens") or 0)
            tokens_all += tok
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            (SEALED_LOG / f"{stamp}_{kind}_{task}_{i}.json").write_text(json.dumps({"ids": ids, "prompt": user, "reply": text, "usage": usage}))
            m = re.search(r"\{.*\}", text, re.S)
            try:
                verdicts = json.loads(m.group(0)) if m else {}
            except ValueError:
                verdicts = {}
            allowed = "ABCU" if kind == "judge" else "LNX"
            got = {hid(ids[t]): str(v).strip().upper()[:1] for t, v in verdicts.items() if t in ids and str(v).strip().upper()[:1] in allowed}
            state["labels"].setdefault(task, {}).update(got)
            state["calls"].append({"task": task, "n": len(chunk), "answered": len(got), "tokens": tok})
            state["spend_points_read"] = spend.used()
            _write(res_p, state, task)
            print(kind, task, f"{len(got)}/{len(chunk)}", tok, "points", spend.used(), flush=True)
    state["stopped"] = state.get("stopped") or "done"
    state["window_after"] = spend.last
    _write(res_p, state, "rsa")
    print(kind, "done", len(state["calls"]), "calls", tokens_all, "tokens (both stages)", spend.used(), "points")


def cmd_summary(_a) -> None:
    judge = json.loads((OUT / "judge.json").read_text())["labels"] if (OUT / "judge.json").is_file() else {}
    conv = json.loads((OUT / "convention.json").read_text())["labels"] if (OUT / "convention.json").is_file() else {}
    rows = []
    for f in sorted((OUT / "tasks").glob("*.json")):
        rec = json.loads(f.read_text())
        task, cases = rec["task"], rec["cases"]
        n = len(cases)
        jl = judge.get(task, {})
        jc = Counter(jl.values())
        jn = sum(jc.values())
        pairs = [(cases[h]["script"], v) for h, v in jl.items() if h in cases]

        def rate(hs):
            vals = [sum(r == "pass" for r in cases[h]["results"]) / len(cases[h]["results"]) for h in hs]
            return round(sum(vals) / len(vals), 4) if vals else None

        by_script = {k: [h for h, v in cases.items() if v["script"] == k] for k in "ABCU"}
        by_judge = {k: [h for h, v in jl.items() if v == k and h in cases] for k in "ABCU"}
        row = {"task": task, "group": rec["group"], "runs": rec["runs"], "cases": n, "apart": APART.get(task, ""),
               "script_share": {k: round(len(v) / n, 4) for k, v in by_script.items()},
               "script_pass_rate": {k: rate(v) for k, v in by_script.items()},
               "judged": jn, "judge_share": {k: round(jc[k] / jn, 4) for k in "ABCU"} if jn else {},
               "judge_pass_rate": {k: rate(v) for k, v in by_judge.items()},
               "agree_three_level": round(sum(x == y for x, y in pairs) / len(pairs), 4) if pairs else None,
               "agree_a_vs_not": round(sum((x == "A") == (y == "A") for x, y in pairs) / len(pairs), 4) if pairs else None,
               "overall_pass_rate": rate(list(cases))}
        # the B class of the whole suite: the script's B cases (the judge only saw a sample)
        b = by_script["B"]
        if rec["runs"] >= 2:
            row["b_groups"] = {"always_pass": sum(all(r == "pass" for r in cases[h]["results"]) for h in b),
                               "sometimes": sum(len(set(cases[h]["results"]) - {"skip"}) > 1 for h in b),
                               "always_fail": sum(all(r == "fail" for r in cases[h]["results"]) for h in b)}
            cl = conv.get(task, {})
            cc = Counter(cl.values())
            row["always_fail_b_convention"] = dict(cc)
            share_l = cc["L"] / sum(cc.values()) if cc else None
            row["always_fail_b_likely_est"] = round(row["b_groups"]["always_fail"] * share_l, 1) if share_l is not None else None
        else:
            row["b_single_run"] = {"pass": sum(cases[h]["results"][0] == "pass" for h in b), "fail": sum(cases[h]["results"][0] == "fail" for h in b)}
        a = by_script["A"]
        row["a_failing_mean"] = round(sum(sum(r == "fail" for r in cases[h]["results"]) / len(cases[h]["results"]) for h in a), 1)
        row["b_failing_mean"] = round(sum(sum(r == "fail" for r in cases[h]["results"]) / len(cases[h]["results"]) for h in b), 1)
        # the same with the judge's class shares applied to the suite (an estimate)
        if jn:
            row["judge_est_counts"] = {k: round(n * jc[k] / jn, 1) for k in "ABCU"}
        row["confusion"] = dict(Counter(f"{x}->{y}" for x, y in pairs))
        G.assert_no_source_overlap(json.dumps(row), task)
        rows.append(row)
    (OUT / "summary.json").write_text(json.dumps(rows, indent=1))
    for r in rows:
        print(json.dumps(r))


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepare").set_defaults(fn=cmd_prepare)
    for name in ("judge", "convention"):
        j = sub.add_parser(name)
        j.add_argument("--cap", type=float, default=2.0)
        j.add_argument("--batch", type=int, default=10)
        j.add_argument("--model", default="gpt-5.6-luna")
        j.set_defaults(fn=(lambda a, k=name: _judge_loop(a, k)))
    sub.add_parser("summary").set_defaults(fn=cmd_summary)
    a = ap.parse_args()
    a.fn(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
