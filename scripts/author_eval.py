#!/usr/bin/env python3
"""Author-suite evaluation by memory replay (author-evolution spec §8.1, §10).

An *evaluation suite* is what one author version writes for one training milestone in an
author-only run (no implementer). It is scored on code that already exists: every retained
candidate workspace of the milestone in the ledger, plus the final repository of the main
evolution run. Held-out results come from the sealed cache; this script never opens the
held-out suite -- it writes evaluation records and hands them to
``scripts/sealed/heldout_matrix.py metrics``.

Groups (cycle 0): A = v10.1 (configs/roles, no author layer); B = the v11 prompt
(outputs/roles_v11); C = v10.1 + inventory + coverage + fix rounds + soft split
(configs/experiments/author_probe_inventory.yaml). Sample 0 of A is the frozen gate suite of
the main run (cached; no model call).

    uv run python scripts/author_eval.py select [--extra 8] > milestones.txt
    uv run python scripts/author_eval.py baseline --label A                    # register cached suites as A sample 0
    uv run python scripts/author_eval.py generate --label C --group C --sample 0 --select-file milestones.txt [--jobs 3]
    uv run python scripts/author_eval.py score --label C --sample 0
    uv run python scripts/author_eval.py metrics --label C --sample 0
    uv run python scripts/author_eval.py report A B C
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestra.codeprojecteval.case_symbols import suite_case_symbols  # noqa: E402
from orchestra.codeprojecteval.public_symbols import load_docs, parse_directory_tree  # noqa: E402
from orchestra.codeprojecteval.suite_audit import audit_suite  # noqa: E402
from orchestra.control.author.node_review import lenient_symbols  # noqa: E402

EVAL_ROOT = ROOT / "outputs" / "author_eval"
DATASET_ROOT = Path(os.environ.get("CPE_DATASET_ROOT") or "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
ENV_ROOT = Path(os.environ.get("CPE_ENV_ROOT") or "/root/codex-benchmarks/cpe_envs")
PLANS = ROOT / "configs" / "datasets" / "cpe_feature_plans"
CENSUS = ROOT / "docs" / "reports" / "heldout_census_20261005.json"
GROUPS = {
    "A": {"config": "configs/experiments/author_probe.yaml", "pool": None},
    "B": {"config": "configs/experiments/author_probe.yaml", "pool": "outputs/roles_v11"},
    "C": {"config": "configs/experiments/author_probe_inventory.yaml", "pool": None},
}
_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")


def _census() -> list[dict]:
    return json.loads(CENSUS.read_text(encoding="utf-8"))


def _manifest(label: str, sample: int) -> Path:
    return EVAL_ROOT / label / f"suites.s{sample}.json"


def _read(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default


def _packages(task: str) -> list[str]:
    cfg = json.loads((DATASET_ROOT / task / "config.json").read_text(encoding="utf-8"))
    return [str(cfg.get("source_code") or task).removeprefix("src/").split("/")[0]]


def _submodules(task: str) -> set[str]:
    tree = [t for text in load_docs(DATASET_ROOT / task / "docs").values() for t in parse_directory_tree(text)]
    return {t.rstrip("/").rsplit("/", 1)[-1].removesuffix(".py") for t in tree
            if (t.endswith(".py") or t.endswith("/")) and not t.rsplit("/", 1)[-1].startswith("__")}


# --- selection -----------------------------------------------------------------


def select(args) -> None:
    """Milestones with documented held-out failures, plus ``--extra`` others (fixed seed)."""
    rows = _census()
    informative = [r for r in rows if (r.get("doc") or 0) > 0]
    rest = sorted((r for r in rows if not (r.get("doc") or 0) > 0), key=lambda r: (r["task"], r["milestone"]))
    rng = random.Random(args.seed)
    extra = rng.sample(rest, min(args.extra, len(rest)))
    for r in informative + extra:
        print(f"{r['task']}:{r['milestone']}")


# --- suites ------------------------------------------------------------------------


def _main_final(task: str, milestone: str) -> tuple[str, str]:
    """(run directory, final repository) of the census run for the milestone."""
    for r in _census():
        if r["task"] == task and r["milestone"] == milestone and r.get("run"):
            run = ROOT / "outputs" / "cpe_evolution" / r["run"] / task
            fins = sorted(run.glob("tasks/*/canonical/repo"))
            return str(run), (str(fins[0]) if fins else "")
    return "", ""


def baseline(args) -> None:
    """A, sample 0: the frozen gate suites of the main evolution runs (v10.1, no model call)."""
    out = []
    for r in _census():
        run, final = _main_final(r["task"], r["milestone"])
        suite = Path(run) / "harness" / f"{r['milestone']}.spec_tests" if run else None
        if suite and suite.is_dir():
            out.append({"task": r["task"], "milestone": r["milestone"], "sample": 0, "run_id": Path(run).parent.name,
                        "suite_dir": str(suite), "soft_dirs": [], "note": "cached gate suite", "prompt_tokens": None,
                        "review": None})
    p = _manifest(args.label, 0)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"registered {len(out)} cached suites as {args.label} sample 0")


def generate(args) -> None:
    import author_probe as probe

    group = GROUPS[args.group]
    selects = [ln.strip().split(":", 1) for ln in Path(args.select_file).read_text().splitlines() if ln.strip()]
    tag = f"{args.label}-s{args.sample}"
    manifest = _manifest(args.label, args.sample)
    done = {(e["task"], e["milestone"]) for e in _read(manifest, []) if e.get("suite_dir")}
    todo = [(t, m) for t, m in selects if (t, m) not in done]
    print(f"{args.label} sample {args.sample}: {len(todo)} author runs ({len(done)} already done)", flush=True)

    def one(tm):
        t, m = tm
        suite, note = probe.run_probe(t, m, tag=tag, plans_dir=PLANS, config=group["config"], role_pool_dir=group["pool"])
        run_dir = probe.OUTPUT_ROOT / note.split()[0] / t
        softs = sorted(str(p) for p in (run_dir / "author_inventory" / m).glob("*.spec_tests_soft"))
        reviews = sorted((run_dir / "author_inventory" / m).glob("*.review.json"))
        tok = re.search(r"prompt_tokens=(\d+)", note)
        return {"task": t, "milestone": m, "sample": args.sample, "run_id": note.split()[0],
                "suite_dir": str(suite) if suite else "", "soft_dirs": softs, "note": note,
                "prompt_tokens": int(tok.group(1)) if tok else None,
                "review": json.loads(reviews[-1].read_text()) if reviews else None,
                "inventory": str(run_dir / "author_inventory" / f"{m}.inventory.json")
                if (run_dir / "author_inventory" / f"{m}.inventory.json").is_file() else ""}

    entries = _read(manifest, [])
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for rec in ex.map(one, todo):
            entries = [e for e in entries if (e["task"], e["milestone"]) != (rec["task"], rec["milestone"])] + [rec]
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps(entries, indent=1), encoding="utf-8")
            print(f"{rec['task']}:{rec['milestone']} -> {rec['note']} suite={'yes' if rec['suite_dir'] else 'MISSING'}", flush=True)


# --- scoring -----------------------------------------------------------------------


def run_suite(repo: Path, suite: Path, python: Path, *, timeout: int = 900, timeout_flags: bool = True,
              messages: dict | None = None) -> dict[str, str]:
    """``{spec_tests/file::[Class::]test: pass|fail}``; parametrisations folded (any fail = fail)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        holder = Path(tmp)
        (holder / "spec_tests").symlink_to(suite.resolve(), target_is_directory=True)
        try:
            proc = subprocess.run(
                [str(python), "-m", "pytest", str(holder / "spec_tests"), "-q", "-rA", "--no-header", "-p", "no:cacheprovider",
                 "-o", "addopts=", "--continue-on-collection-errors",
                 *(["--timeout=60", "--timeout-method=signal"] if timeout_flags else [])],
                cwd=repo, capture_output=True, text=True, timeout=timeout, check=False,
                env={"PYTHONPATH": os.pathsep.join(p for p in [str(holder), str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
                     "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"},
            )
            stdout = proc.stdout
        except subprocess.TimeoutExpired:
            stdout = ""
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        node = m.group(2)
        node = "spec_tests/" + node.split("spec_tests/", 1)[-1] if "spec_tests/" in node else node
        base = node.split("[", 1)[0]
        v = "pass" if m.group(1) in ("PASSED", "XPASS") else "fail"
        if messages is not None and v == "fail" and " - " in line:
            messages.setdefault(base, line.split(" - ", 1)[1][:300])
        out[base] = "fail" if v == "fail" or out.get(base) == "fail" else "pass"
    return out


def _workspaces(task: str, milestone: str) -> list[str]:
    from orchestra.control.evolution.ledger import ledger_root_default, read_jsonl
    ws = set()
    for f in ledger_root_default().glob("*/candidates.jsonl"):
        for r in read_jsonl(f):
            if str(r.get("task_id") or "").removeprefix("rb_") == task and r.get("milestone_id") == milestone and r.get("workspace_ref"):
                if Path(r["workspace_ref"]).is_dir():
                    ws.add(str(Path(r["workspace_ref"])).rstrip("/"))
    return sorted(ws)


def score(args) -> None:
    entries = _read(_manifest(args.label, args.sample), [])
    out_path = EVAL_ROOT / args.label / f"records.s{args.sample}.json"
    records = {(r["task"], r["milestone"]): r for r in _read(out_path, [])}
    for e in entries:
        key = (e["task"], e["milestone"])
        if key in records and not args.refresh or not e.get("suite_dir"):
            continue
        task, mid = key
        python = ENV_ROOT / task / "bin" / "python"
        _run, final = _main_final(task, mid)
        targets = ([final] if final else []) + _workspaces(task, mid)
        hard = Path(e["suite_dir"])
        softs = [Path(s) for s in e.get("soft_dirs") or []]
        pk, subs = _packages(task), _submodules(task)
        cases = [{"case_id": c, "tier": "hard", "symbols": s} for c, s in suite_case_symbols(hard, pk, subs, "spec_tests/").items()]
        for sd in softs:
            cases += [{"case_id": c, "tier": "soft", "symbols": s} for c, s in suite_case_symbols(sd, pk, subs, "spec_tests/").items()]
        t0 = time.time()
        results = {}
        for ws in targets:
            res = run_suite(Path(ws), hard, python)
            for sd in softs:
                res.update(run_suite(Path(ws), sd, python))
            results[ws] = res
        docs = load_docs(DATASET_ROOT / task / "docs")
        plan = json.loads((PLANS / f"{task}.plan.json").read_text(encoding="utf-8"))
        focus = next((m.get("focus_paths") or [] for m in plan["milestones"] if m["milestone_id"] == mid), [])
        audit = audit_suite(hard, docs, packages=pk, allowed_symbols=lenient_symbols(docs, str(PLANS / f"{task}.plan.json"), mid, focus))
        review = e.get("review") or {}
        inv_total = review.get("hard_total") if review else None
        records[key] = {
            "task": task, "milestone": mid, "label": args.label, "sample": args.sample, "final": final,
            "suite_dir": str(hard), "suite_cases": cases, "suite_results": results,
            "inventory": e.get("inventory") or "", "inventory_hard_total": inv_total,
            "inventory_hard_covered": review.get("hard_covered") if review else None,
            "cost": {"cases": len(cases), "seconds": round(time.time() - t0, 1),
                     "tokens": e.get("prompt_tokens") or 0, "rounds": review.get("fix_calls", 0) if review else 0},
            "audit_violations": len(audit.violations),
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(list(records.values()), indent=1), encoding="utf-8")
        print(f"scored {args.label} s{args.sample} {task}:{mid}  cases {len(cases)}  workspaces {len(targets)}  audit {len(audit.violations)}", flush=True)


def metrics(args) -> None:
    rec = EVAL_ROOT / args.label / f"records.s{args.sample}.json"
    out = EVAL_ROOT / args.label / f"metrics.s{args.sample}.json"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "sealed" / "heldout_matrix.py"), "metrics", str(rec), "--out", str(out)],
                   check=True, cwd=ROOT)


# --- report ------------------------------------------------------------------------

METRICS = [("true_miss", "true miss rate (main)"), ("cell_miss", "milestone-level miss (suite never fails validly)"), ("symbol_coverage", "symbol-level coverage"),
           ("ceiling_share", "ceiling share (report only)"), ("ref_fail_rate_hard", "reference-fail rate, hard"),
           ("ref_fail_rate_soft", "reference-fail rate, soft"), ("false_positive_rate", "false-positive rate"),
           ("discrimination", "discrimination (report only)"), ("inventory_coverage_hard", "inventory hard coverage"),
           ("inventory_gap_rate", "inventory gap rate"), ("audit_violations", "audit violations"),
           ("cost_cases", "cost: cases"), ("cost_tokens", "cost: prompt tokens"), ("cost_rounds", "cost: fix rounds")]


def report(args) -> None:
    from orchestra.control.author.metrics import stability

    lines = ["# Author evaluation by memory replay", "",
             "Evaluation suites scored on retained workspaces and the final repository; held-out read from the sealed cache. "
             "Each cell: mean over milestones where the metric is defined (n). Acceptance is also shown without tinydb "
             "(seen while v11 was designed); if the two disagree, the version without tinydb decides.", ""]
    for split in ("evolution", "acceptance", "acceptance_without"):
        lines += [f"## {split.replace('_', ' ')}", "", "| metric | " + " | ".join(args.labels) + " |",
                  "|---|" + "---|" * len(args.labels)]
        per_label = {}
        for lab in args.labels:
            per_label[lab] = [json.loads(p.read_text())["summary"].get(split, {}) for p in sorted((EVAL_ROOT / lab).glob("metrics.s*.json"))]
        for key, name in METRICS:
            cells = []
            for lab in args.labels:
                vals = [s.get(key) for s in per_label[lab] if s]
                if not vals or vals[0] is None:
                    cells.append("-")
                    continue
                n = per_label[lab][0].get(f"{key}_n")
                sd = stability(vals)
                cells.append(f"{vals[0]}" + (f" (n={n})" if n is not None else "") + (f", sd {sd}" if sd is not None else ""))
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        lines.append("")
    text = "\n".join(lines) + "\n"
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--extra", type=int, default=8)
    s.add_argument("--seed", type=int, default=20261005)
    s.set_defaults(fn=select)
    b = sub.add_parser("baseline")
    b.add_argument("--label", default="A")
    b.set_defaults(fn=baseline)
    g = sub.add_parser("generate")
    g.add_argument("--label", required=True)
    g.add_argument("--group", required=True, choices=sorted(GROUPS))
    g.add_argument("--sample", type=int, default=0)
    g.add_argument("--select-file", required=True)
    g.add_argument("--jobs", type=int, default=3)
    g.set_defaults(fn=generate)
    sc = sub.add_parser("score")
    sc.add_argument("--label", required=True)
    sc.add_argument("--sample", type=int, default=0)
    sc.add_argument("--refresh", action="store_true")
    sc.set_defaults(fn=score)
    m = sub.add_parser("metrics")
    m.add_argument("--label", required=True)
    m.add_argument("--sample", type=int, default=0)
    m.set_defaults(fn=metrics)
    r = sub.add_parser("report")
    r.add_argument("labels", nargs="+")
    r.add_argument("--out", default=None)
    r.set_defaults(fn=report)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
