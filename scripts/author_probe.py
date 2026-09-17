#!/usr/bin/env python3
"""Author-only probe: judge the test author's suites without running a task.

One milestone of a frozen feature plan is replayed on the pristine repository
with the `author_only` template (a single test_author slot) and every search
off, so the run costs one author call and leaves `<mid>.spec_tests` in the
harness directory. Each frozen suite is then audited offline:

  * on the dataset's reference implementation (never in a workspace):
    validity `passed/total`, collection errors, and which failing tests quote
    a real document sentence;
  * statically, for the workaround class the v10 author rules forbid:
    priming calls (register_builtins / load_plugins / configure / _init...
    before the behaviour under test), shims (sys.path, sys.modules, import
    fallbacks, importorskip, skip), reaching inside (`obj._x`), broad
    `pytest.raises(Exception)`, module-top project imports;
  * breadth: cases, citation ratio, timeout marks.

    uv run python scripts/author_probe.py run --tag v10 \\
        --select nl2_tablib:shared_contracts_and_registry ...
    uv run python scripts/author_probe.py audit --label v9-run <batch>/<task> ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = Path(os.environ.get("CPE_DATASET_ROOT") or "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
ENV_ROOT = Path(os.environ.get("CPE_ENV_ROOT") or "/root/codex-benchmarks/cpe_envs")
OUTPUT_ROOT = ROOT / "outputs" / "author_probe"

PRIMING = re.compile(
    r"\.(register_builtins|register_all|register_defaults|load_builtins|load_plugins|"
    r"load_defaults|bootstrap|initiali[sz]e|setup_all|autodiscover|_init\w*|_register\w*|_load\w*)\("
)
SHIMS = {
    "sys_path": re.compile(r"sys\.path\.(insert|append)"),
    "sys_modules": re.compile(r"sys\.modules\[|sys\.modules\.pop|del sys\.modules"),
    "import_fallback": re.compile(r"except\s*\(?\s*(ImportError|ModuleNotFoundError)"),
    "importorskip_or_skip": re.compile(r"pytest\.(importorskip|skip)\("),
    "reload": re.compile(r"importlib\.reload\("),
}
PRIVATE = re.compile(r"(?<![\w.])[A-Za-z]\w*\._(?!_)[A-Za-z]\w*")
BROAD_RAISES = re.compile(r"pytest\.raises\(\s*(Exception|BaseException)\s*[,)]")
CITATION = re.compile(r"#\s*(PRD|Architecture|UML|Directory|Design|README)\b[^\n]*\"")


def _packages(task: str) -> list[str]:
    cfg = json.loads((DATASET_ROOT / task / "config.json").read_text())
    src = str(cfg.get("source_code") or "").strip("/")
    head = src.split("/")[-1] if src else task
    if head == "src":
        head = task
    return [head]


def _functions(text: str) -> list[tuple[str, str, str]]:
    """(kind, name, body) for every top-level or class-level function, kind in test/fixture/other."""
    lines = text.splitlines()
    out = []
    starts = [i for i, ln in enumerate(lines) if re.match(r"\s*(async\s+)?def \w+\(", ln)]
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        name = re.match(r"\s*(?:async\s+)?def (\w+)\(", lines[i]).group(1)
        j = i - 1
        deco = []
        while j >= 0 and (lines[j].strip().startswith("@") or lines[j].strip().startswith("#") or not lines[j].strip()):
            deco.append(lines[j])
            j -= 1
        head = "\n".join(reversed(deco))
        body = "\n".join(lines[i:end])
        kind = "test" if name.startswith("test") else ("fixture" if "fixture" in head else "other")
        out.append((kind, name, head + "\n" + body))
    return out


def static_audit(suite: Path, packages: list[str], docs: str = "") -> dict:
    texts = {p.name: p.read_text(errors="replace") for p in sorted(suite.glob("*.py"))}
    whole = "\n".join(texts.values())
    funcs = [f for t in texts.values() for f in _functions(t)]
    tests = [f for f in funcs if f[0] == "test"]
    fixtures = [f for f in funcs if f[0] == "fixture"]

    def priming_in(body: str, name: str) -> list[str]:
        hits = []
        # The citation comment above a test quotes the helper's own sentence.
        for m in PRIMING.finditer(re.sub(r"#[^\n]*", "", body)):
            helper = m.group(1)
            if helper.strip("_").lower() in name.lower():
                continue  # a test *of* the helper
            hits.append(helper)
        return hits

    primed_tests = [n for _, n, b in tests if priming_in(b, n)]
    primed_fixtures = [n for _, n, b in fixtures if priming_in(b, n)]
    module_level = [ln for t in texts.values() for ln in t.splitlines() if not ln.startswith((" ", "\t"))]
    module_priming = [ln.strip() for ln in module_level if PRIMING.search(ln)]
    helpers = Counter(h for _, n, b in tests + fixtures for h in priming_in(b, n))

    shim_hits = {k: len(rx.findall(whole)) for k, rx in SHIMS.items()}
    pkg_rx = re.compile(r"^(from|import)\s+(%s)\b" % "|".join(map(re.escape, packages)))
    top_imports = [ln.strip() for ln in module_level if pkg_rx.match(ln)]
    # `tablib._vendor.dbfpy` is a documented path: a private-looking name the
    # documents themselves use is not reaching inside.
    def undocumented_private(body: str) -> bool:
        return any(m.group(0).split(".")[-1] not in docs for m in PRIVATE.finditer(re.sub(r"#[^\n]*", "", body)))

    private_tests = [n for _, n, b in tests if undocumented_private(b)]
    cited = [n for _, n, b in tests if CITATION.search(b)]
    return {
        "files": len(texts),
        "cases": len(tests),
        "fixtures": len(fixtures),
        "cited_ratio": round(len(cited) / len(tests), 2) if tests else 0.0,
        "timeout_marks": len(re.findall(r"mark\.timeout\(", whole)),
        "priming_tests": len(primed_tests),
        "priming_fixtures": len(primed_fixtures),
        "priming_module_level": len(module_priming),
        "priming_helpers": dict(helpers.most_common(4)),
        "shims": {k: v for k, v in shim_hits.items() if v},
        "shim_total": sum(shim_hits.values()),
        "private_access_tests": len(private_tests),
        "broad_raises": len(BROAD_RAISES.findall(whole)),
        "module_top_project_imports": len(top_imports),
        "private_examples": private_tests[:4],
        "primed_examples": (primed_fixtures + primed_tests)[:4],
    }


def _reference_repo(task: str) -> Path:
    dest = Path(tempfile.mkdtemp(prefix=f"probe_ref_{task}_"))
    shutil.copytree(DATASET_ROOT / task, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("unit_tests", "check_tests", "__pycache__", ".git"))
    return dest


def _doc_text(task: str) -> str:
    docs = DATASET_ROOT / task / "docs"
    return " ".join(" ".join(p.read_text(errors="replace").split()) for p in docs.glob("*") if p.is_file())


def run_on_reference(task: str, suite: Path) -> dict:
    py = ENV_ROOT / task / "bin" / "python"
    repo = _reference_repo(task)
    # Copied into the repository as `spec_tests/`, where the author wrote it:
    # `Path(__file__).parents[1]` is then the repository, as in a workspace.
    holder = repo
    shutil.copytree(suite, repo / "spec_tests")
    roots = [str(repo)] + ([str(repo / "src")] if (repo / "src").is_dir() else [])
    try:
        proc = subprocess.run(
            [str(py), "-m", "pytest", str(repo / "spec_tests"), "-q", "-p", "no:cacheprovider",
             "-o", "addopts=", "--continue-on-collection-errors", "-rfE", "--timeout=120"],
            cwd=repo, capture_output=True, text=True, timeout=1200,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(roots)},
        )
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        out = "TIMEOUT"
    finally:
        shutil.rmtree(repo, ignore_errors=True)
    counts = {k: 0 for k in ("passed", "failed", "error")}
    for k in counts:
        m = re.search(rf"(\d+) {k}", out)
        if m:
            counts[k] = int(m.group(1))
    total = sum(counts.values())
    failing = re.findall(r"(?:FAILED|ERROR) [^ \n]*::(\S+)", out)
    collection_errors = len(re.findall(r"^ERROR [^:\n]+\.py\s*$", out, re.M))
    docs = _doc_text(task)
    backed, unsupported = [], []
    for name in failing:
        base = name.split("[")[0].split("::")[-1]
        quoted = None
        for path in suite.glob("*.py"):
            lines = path.read_text(errors="replace").splitlines()
            for i, line in enumerate(lines):
                if re.match(rf"\s*(async\s+)?def {re.escape(base)}\(", line):
                    j, comments = i - 1, []
                    while j >= 0 and lines[j].strip().startswith("@"):
                        j -= 1
                    while j >= 0 and lines[j].strip().startswith("#"):
                        comments.append(lines[j].strip("# ").strip())
                        j -= 1
                    quoted = comments
                    break
            if quoted is not None:
                break
        quotes = [re.sub(r"^(PRD|Architecture|UML)[^\"]*\"|\"\s*$", "", c).strip('"').strip() for c in (quoted or [])]
        (backed if any(len(q) > 20 and " ".join(q.split()) in docs for q in quotes) else unsupported).append(base)
    signatures = Counter(
        re.sub(r"0x[0-9a-f]+|/tmp/\S+", "…", ln.strip())[:110]
        for ln in out.splitlines() if ln.startswith("E   ") and not ln.startswith("E    +")
    )
    dominant = signatures.most_common(1)[0] if signatures else ("", 0)
    return {
        "ref_passed": counts["passed"], "ref_total": total,
        "ref_validity": round(counts["passed"] / total, 2) if total else None,
        "ref_collection_errors": collection_errors,
        "ref_fail_doc_backed": len(set(backed)), "ref_fail_unsupported": len(set(unsupported)),
        "ref_dominant_error": dominant[0] if dominant[1] >= 3 else "",
        "ref_unsupported_examples": sorted(set(unsupported))[:5],
    }


def audit_suite(task: str, suite: Path, *, label: str, run: str) -> dict:
    row = {"label": label, "task": task, "milestone": suite.name.replace(".spec_tests", ""), "run": run,
           "suite_path": str(suite)}
    row.update(static_audit(suite, _packages(task), _doc_text(task)))
    row.update(run_on_reference(task, suite))
    return row


def audit_run_dir(run_dir: Path, *, label: str, task: str | None = None) -> list[dict]:
    task = task or run_dir.name
    suites = sorted(p for p in (run_dir / "harness").glob("*.spec_tests") if p.is_dir())
    return [audit_suite(task, s, label=label, run=run_dir.parent.name) for s in suites]


# ----------------------------------------------------------------------------- run


def _single_milestone_plan(plan_path: Path, milestone_id: str, dest: Path) -> Path:
    plan = json.loads(plan_path.read_text())
    ms = [m for m in plan["milestones"] if m["milestone_id"] == milestone_id]
    if not ms:
        raise SystemExit(f"{plan_path.name}: no milestone {milestone_id}")
    m = dict(ms[0])
    author = next((a for a in m.get("agents") or [] if a.get("role") == "test_author"), None) or {
        "slot": "test_author", "role": "test_author",
        "mandate": "Write the specification suite for this milestone from the design documents.",
    }
    m["template_id"] = "author_only"
    m["agents"] = [dict(author, slot="test_author")]
    milestones = [m]
    if plan["milestones"][-1]["milestone_id"] != milestone_id:
        # The parser makes the last milestone of any plan the integration one
        # (milestone_planner.parse_plan_payload), which would hand an
        # implementation milestone the integration brief. A copy of the plan's
        # real last milestone follows the probe; it depends on the probe, which
        # fails its gate on the unimplemented repository, so it is skipped
        # ("blocked by failed dependency") and never calls a model.
        tail = dict(plan["milestones"][-1])
        tail["template_id"] = "author_only"
        tail["agents"] = [dict(author, slot="test_author")]
        tail["depends_on"] = [milestone_id]
        milestones.append(tail)
    out = dict(plan, milestones=milestones, generator=f"author_probe:{plan.get('generator', '')}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2))
    return dest


def run_probe(task: str, milestone_id: str, *, tag: str, plans_dir: Path, config: str) -> tuple[Path | None, str]:
    plan = _single_milestone_plan(plans_dir / f"{task}.plan.json", milestone_id,
                                  OUTPUT_ROOT / "plans" / tag / f"{task}.{milestone_id}.plan.json")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"author-{tag}-{stamp}-{task}-{milestone_id}"[:120]
    log_dir = OUTPUT_ROOT / "logs" / tag
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(ROOT / ".venv" / "bin" / "python"), "-m", "orchestra.cli.run_codeprojecteval_decomp",
           "--config", config, "--task-id", task, "--plan-file", str(plan), "--arm", "author_probe",
           "--run-id", run_id, "--dataset-root", str(DATASET_ROOT), "--output-root", str(OUTPUT_ROOT)]
    with (log_dir / f"{task}.{milestone_id}.log").open("w") as log:
        proc = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
    suite = OUTPUT_ROOT / run_id / task / "harness" / f"{milestone_id}.spec_tests"
    note = f"{run_id} rc={proc.returncode}"
    summary = OUTPUT_ROOT / run_id / task / "summary.json"
    if summary.is_file():
        objectives = json.loads(summary.read_text()).get("milestone_objectives") or []
        tokens = sum(int(o.get("prompt_tokens") or 0) for o in objectives)
        note += f" prompt_tokens={tokens}"
        if tokens == 0:
            # The author never answered: an exhausted account window fails the
            # call upstream (HTTP 400 invalid_prompt) and the run ends clean.
            note += " NO MODEL CALL (account window?)"
    return (suite if suite.is_dir() else None), note


COLUMNS = ["label", "task", "milestone", "cases", "cited_ratio", "priming_tests", "priming_fixtures",
           "shim_total", "private_access_tests", "broad_raises", "module_top_project_imports",
           "ref_validity", "ref_collection_errors", "ref_fail_unsupported"]


def print_table(rows: list[dict]) -> None:
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in COLUMNS}
    print(" | ".join(c.ljust(widths[c]) for c in COLUMNS))
    for r in rows:
        print(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in COLUMNS))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--tag", required=True)
    r.add_argument("--select", action="append", required=True, help="task:milestone_id")
    r.add_argument("--plans-dir", type=Path, default=ROOT / "configs/datasets/nl2repo_feature_plans")
    r.add_argument("--config", default="configs/experiments/author_probe.yaml")
    r.add_argument("--jobs", type=int, default=3)
    rs = sub.add_parser("reaudit", help="recompute every column of existing audit files in place")
    rs.add_argument("files", nargs="+", type=Path)
    a = sub.add_parser("audit")
    a.add_argument("--label", required=True)
    a.add_argument("run_dirs", nargs="+", type=Path, help="<batch>/<task> directories")
    a.add_argument("--milestone", action="append", default=None)
    for p in (r, a):
        p.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    rows: list[dict] = []
    if args.cmd == "reaudit":
        for f in args.files:
            data = json.loads(f.read_text())
            for r in data:
                suite = Path(r["suite_path"]) if str(r.get("suite_path") or "").endswith(".spec_tests") else None
                if suite is None or not suite.is_dir():
                    root = OUTPUT_ROOT if r["run"].startswith("author-") else ROOT / "outputs" / "cpe_milestones"
                    suite = root / r["run"] / r["task"] / "harness" / f"{r['milestone']}.spec_tests"
                assert suite.is_dir(), suite
                r.update(static_audit(suite, _packages(r["task"]), _doc_text(r["task"])))
                r.update(run_on_reference(r["task"], suite))
                r["suite_path"] = str(suite)
            f.write_text(json.dumps(data, indent=2))
            rows += data
        print_table(rows)
        return
    if args.cmd == "run":
        selects = [s.split(":", 1) for s in args.select]
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(run_probe, t, m, tag=args.tag, plans_dir=args.plans_dir, config=args.config): (t, m)
                    for t, m in selects}
            for fut, (t, m) in futs.items():
                suite, note = fut.result()
                print(f"{t}:{m} -> {note} suite={'yes' if suite else 'MISSING'}", flush=True)
                if suite:
                    rows.append(audit_suite(t, suite, label=args.tag, run=note.split()[0]))
        out = args.out or OUTPUT_ROOT / f"{args.tag}.audit.json"
    else:
        for d in args.run_dirs:
            for row in audit_run_dir(d, label=args.label):
                if args.milestone and row["milestone"] not in args.milestone:
                    continue
                rows.append(row)
        out = args.out or OUTPUT_ROOT / f"{args.label}.audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(out.read_text()) if out.is_file() else []
    out.write_text(json.dumps(existing + rows, indent=2))
    print_table(rows)
    print(f"wrote {out} ({len(rows)} suites)")


if __name__ == "__main__":
    main()
