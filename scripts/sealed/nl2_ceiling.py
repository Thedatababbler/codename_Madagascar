#!/usr/bin/env python3
"""NL2Repo ceiling decomposition (user's check, 2026-10-15). Sealed: reads the held-out suites and the references.

Reads stored runs only; starts no solving agent. The one model call is the sealed judge (capped at 1 quota point
by this script). Everything written to outputs/nl2_ceiling/ is case ids, labels and numbers -- never test source,
assertion text or literals (``assert_no_source_overlap`` on every file). The judge's prompts and replies go to
the sealed log directory /root/.adamas_sealed/nl2_ceiling_judge/, which the sandbox denies to every agent.

    uv run python scripts/sealed/nl2_ceiling.py runs        # which runs count, and why the others do not
    uv run python scripts/sealed/nl2_ceiling.py cases       # held-out per case on each run's final repository
    uv run python scripts/sealed/nl2_ceiling.py labels      # attribution + three-level script labels per task
    uv run python scripts/sealed/nl2_ceiling.py facts       # reference size, document length (numbers only)
    uv run python scripts/sealed/nl2_ceiling.py split       # gate suites on the final repo / the reference, A-class split
    uv run python scripts/sealed/nl2_ceiling.py judge [--cap 1.0]
    uv run python scripts/sealed/nl2_ceiling.py summary
"""

from __future__ import annotations

import argparse
import ast
import glob
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
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

OUT = ROOT / "outputs" / "nl2_ceiling"
SEALED_LOG = Path("/root/.adamas_sealed/nl2_ceiling_judge")
TASKS = ["nl2_aiofiles", "nl2_emoji", "nl2_ftfy", "nl2_python-dotenv", "nl2_python-jose", "nl2_python-pathspec",
         "nl2_tablib", "nl2_tenacity", "nl2_voluptuous"]
#: runs marked suspicious (EXP-20261011-01: the final code holds a byte-identical reference file)
SUSPICIOUS = {"cpe-20260908T111133Z": "final code holds the reference threadpool/binary.py (EXP-20261011-01)"}
_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")


def _write(path: Path, data, task: str | None = None) -> None:
    text = json.dumps(data, indent=1, sort_keys=True)
    for t in ([task] if task else TASKS):
        G.assert_no_source_overlap(text, t)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- runs


def _model_of(task_dir: Path) -> str:
    ev = task_dir / "events.jsonl"
    if not ev.is_file():
        return ""
    c = Counter(re.findall(r"gpt-[0-9.]+(?:-[a-z]+)?", ev.read_text(errors="replace")))
    return c.most_common(1)[0][0] if c else ""


def discover() -> list[dict]:
    found = []
    for task_dir in sorted({*map(Path, glob.glob(str(ROOT / "outputs/cpe_official_continuation/cpe-*/nl2_*"))),
                            *map(Path, glob.glob(str(ROOT / "outputs/cpe_milestones/cpe-*-nl2_*/nl2_*"))),
                            *map(Path, glob.glob(str(ROOT / "outputs/nl2sol_evolution_baseline/cpe-*-nl2_*/nl2_*")))}):
        task = task_dir.name
        if task not in TASKS or not task_dir.is_dir():
            continue
        batch = task_dir.parent
        te = next(iter(task_dir.glob("tasks/rb_*/task_execution.json")), None)
        subs = (_read(te).get("subtasks") or {}) if te else {}
        committed = sum(1 for v in subs.values() if v.get("status") == "committed")
        canon = next(iter(task_dir.glob("tasks/*/canonical/repo")), None)
        hv = batch / "hidden_eval.json"
        stored = _read(hv).get("mean_pass_rate") if hv.is_file() else None
        stamp = re.search(r"cpe-(\d{8}T\d{6}Z)", batch.name).group(1)
        series = "two_milestone_plans" if "cpe_official_continuation" in str(batch) else (
            "feature_plans" if "cpe_milestones" in str(batch) else "feature_plans_sol")
        rec = {"task": task, "batch": batch.name, "stamp": stamp, "series": series, "task_dir": str(task_dir),
               "canonical": str(canon) if canon else "", "milestones": len(subs), "committed": committed,
               "model": _model_of(task_dir), "stored_heldout": stored, "plan": str(task_dir / "milestone_plan_draft.json")}
        why = ""
        sus = next((v for k, v in SUSPICIOUS.items() if k in batch.name), "")
        if sus:
            why = "suspicious: " + sus
        elif rec["model"] != "gpt-5.5":
            why = f"model {rec['model'] or '?'} (not the gpt-5.5 series)"
        elif not subs or committed < len(subs):
            why = f"incomplete: {committed}/{len(subs)} milestones committed"
        elif stored is None:
            why = "pilot attempt before the 09-08 fixes, never scored"
        elif not canon:
            why = "no final repository"
        rec["included"] = not why
        rec["excluded_because"] = why
        found.append(rec)
    return found


def cmd_runs(_a) -> None:
    runs = discover()
    _write(OUT / "runs.json", runs)
    for r in runs:
        print(f"{r['task']:22s} {r['batch']:38s} {r['series']:20s} {r['committed']}/{r['milestones']} "
              f"stored={r['stored_heldout']} {'IN' if r['included'] else 'out: ' + r['excluded_because']}")


def included() -> list[dict]:
    return [r for r in _read(OUT / "runs.json") if r["included"]]


# --------------------------------------------------------------------------- held-out per case


def heldout_case_keys(task: str) -> list[str]:
    """Every held-out case as ``<file relative to the held-out dir>::<function>`` (methods by name, params folded)."""
    d = G.heldout_dir(task)
    keys = set()
    for p in sorted({*d.rglob("test_*.py"), *d.rglob("*_test.py")}):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        rel = str(p.relative_to(d))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                keys.add(f"{rel}::{fn.name}")
    return sorted(keys)


def _key_of(node: str, sub: str) -> str | None:
    parts = node.split("::")
    if len(parts) < 2:
        return None
    f = parts[0]
    f = f.split(sub + "/", 1)[-1] if (sub + "/") in f else f
    return f"{f}::{parts[-1].split('[', 1)[0]}"


def run_heldout_cases(task: str, repo_src: Path, *, per_test_timeout: int = 30) -> dict:
    """{case key: pass|fail|skip}, plus node-level counts. Confined (scoring mode), on a scratch copy."""
    from orchestra.sandbox.guard import run_confined

    python = G.task_python(task)
    sub = G.heldout_subdir_name(task)
    keys = heldout_case_keys(task)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task
        shutil.copytree(repo_src, repo, symlinks=True, ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", ".git", "spec_tests", "repair_evidence", sub, "check_tests"))
        shutil.copytree(G.heldout_dir(task), repo / sub)
        env = {"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
               "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = run_confined([str(python), "-m", "pytest", sub, "-q", "-rA", "--no-header", "-p", "no:cacheprovider",
                                 "-o", "addopts=", "--continue-on-collection-errors", f"--timeout={per_test_timeout}",
                                 "--timeout-method=signal"], cwd=repo, mode="scoring", role="nl2_ceiling",
                                capture_output=True, text=True, timeout=5400, check=False, env=env)
            stdout = proc.stdout
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    status: dict[str, str] = {}
    nodes = Counter()
    for line in stdout.splitlines():
        m = _LINE.match(line)
        if not m or "::" not in m.group(2):
            continue
        k = _key_of(m.group(2), sub)
        verdict = {"PASSED": "pass", "XPASS": "pass", "XFAIL": "skip", "SKIPPED": "skip"}.get(m.group(1), "fail")
        nodes[verdict] += 1
        prev = status.get(k)
        if prev == "fail" or verdict == "skip" and prev:
            continue
        status[k] = verdict if prev is None or verdict == "fail" else prev
    out = {k: status.get(k, "fail") for k in keys}       # not collected (a module that does not import) = failed
    return {"cases": out, "node_counts": dict(nodes),
            "case_counts": dict(Counter(out.values())),
            "case_pass_rate": round(sum(v == "pass" for v in out.values()) / max(1, sum(v != "skip" for v in out.values())), 4)}


def cmd_cases(_a) -> None:
    for r in included():
        p = OUT / "cases" / f"{r['batch']}.json"
        if p.is_file():
            continue
        t0 = time.time()
        res = run_heldout_cases(r["task"], Path(r["canonical"]))
        res.update({"task": r["task"], "batch": r["batch"]})
        _write(p, res, r["task"])
        print(r["task"], r["batch"], res["case_counts"], res["case_pass_rate"], "stored", r["stored_heldout"],
              f"{time.time() - t0:.0f}s", flush=True)


# --------------------------------------------------------------------------- attribution + script labels


def cmd_labels(_a) -> None:
    import doc_label
    import heldout_attribution as HA

    for task in sorted({r["task"] for r in included()}):
        root = G.dataset_root_for(task)
        HA.DATASET_ROOT = root
        doc_label.DATASET_ROOT = root
        cfg = G.task_config(task)
        packages = [str(cfg.get("source_code") or task).removeprefix("src/").split("/")[0]]
        syms = HA.case_symbols(task, packages)
        keys = heldout_case_keys(task)
        per_series = {}
        for r in [x for x in included() if x["task"] == task]:
            if r["series"] in per_series:
                continue
            attribution = HA.attribute(task, Path(r["plan"]))
            per_series[r["series"]] = attribution
        # labels do not depend on the plan: label once, with every case under a pseudo milestone if unattributed
        first = next(iter(per_series.values()))
        seen = {c for cs in first.values() for c in cs}
        attr = dict(first)
        attr["__unattributed__"] = [k for k in keys if k not in seen]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / f"{task}.json").write_text(json.dumps({"attribution": attr, "case_symbols": syms}))
            doc_label.ATTRIBUTION_ROOT = Path(tmp)
            labels = doc_label.label_task(task)
        out = {"task": task, "attribution": per_series,
               "labels": {k: {"depth": v["depth"], "reason": v["reason"], "paragraphs": v["paragraphs"]} for k, v in labels.items()},
               "symbols": {k: sorted(v) for k, v in syms.items()}}
        _write(OUT / "labels" / f"{task}.json", out, task)
        print(task, Counter(v["depth"] for v in labels.values()), {s: {m: len(c) for m, c in a.items()} for s, a in per_series.items()})


# --------------------------------------------------------------------------- facts


def cmd_facts(_a) -> None:
    out = {}
    for task in TASKS:
        ref = G.reference_root(task)
        src = ref / "src" if (ref / "src").is_dir() else ref
        files = [p for p in src.rglob("*.py") if "__pycache__" not in p.parts and not any(
            x in p.parts for x in ("unit_tests", "check_tests", "tests"))]
        loc = sum(len([ln for ln in p.read_text(errors="replace").splitlines() if ln.strip()]) for p in files)
        docs = sum(len(p.read_text(errors="replace")) for p in (G.dataset_root_for(task) / task / "docs").glob("*") if p.is_file())
        out[task] = {"reference_py_files": len(files), "reference_nonblank_lines": loc, "docs_chars": docs,
                     "heldout_cases": len(heldout_case_keys(task))}
        print(task, out[task])
    _write(OUT / "facts.json", out)


# --------------------------------------------------------------------------- gate suites: final repo, reference, citations


def _suite_hash(suite: Path) -> str:
    h = hashlib.sha1()
    for p in sorted(suite.rglob("*.py")):
        if "__pycache__" not in p.parts:
            h.update(p.relative_to(suite).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def suite_on_repo(task: str, suite: Path, repo_src: Path, *, per_test_timeout: int = 60) -> dict[str, str]:
    """A gate suite (our own, frozen) run on a final repository, mounted as spec_tests. Confined."""
    from orchestra.sandbox.guard import run_confined

    python = G.task_python(task)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task
        shutil.copytree(repo_src, repo, symlinks=True, ignore=shutil.ignore_patterns(
            "__pycache__", ".pytest_cache", ".git", "spec_tests", "repair_evidence", "unit_tests"))
        holder = Path(tmp) / "specroot"
        holder.mkdir()
        shutil.copytree(suite, holder / "spec_tests", ignore=shutil.ignore_patterns("__pycache__"))
        env = {"PYTHONPATH": os.pathsep.join(p for p in [str(holder), str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
               "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = run_confined([str(python), "-m", "pytest", str(holder / "spec_tests"), "-q", "-rA", "--no-header",
                                 "-p", "no:cacheprovider", "-o", "addopts=", "--continue-on-collection-errors",
                                 f"--timeout={per_test_timeout}", "--timeout-method=signal"], cwd=repo, mode="scoring",
                                role="nl2_ceiling_suite", capture_output=True, text=True, timeout=1800, check=False, env=env)
            stdout = proc.stdout
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        m = _LINE.match(line)
        if m and "::" in m.group(2):
            node = m.group(2)
            node = "spec_tests/" + node.split("spec_tests/", 1)[-1] if "spec_tests/" in node else node
            node = node.split("[", 1)[0]
            v = "pass" if m.group(1) in ("PASSED", "XPASS") else ("skip" if m.group(1) in ("SKIPPED", "XFAIL") else "fail")
            if out.get(node) != "fail":
                out[node] = v
    return out


def _norm_case(c: str) -> str:
    f, _, rest = c.partition("::")
    return f.rsplit("/", 1)[-1] + "::" + rest.split("[", 1)[0].split("::")[-1]


def cmd_split(_a) -> None:
    from orchestra.codeprojecteval.public_symbols import load_docs
    from orchestra.codeprojecteval.suite_audit import audit_suite, normalise
    from reference_check import suite_on_reference
    import doc_label

    for r in included():
        p = OUT / "suites" / f"{r['batch']}.json"
        if p.is_file():
            continue
        task = r["task"]
        doc_label.DATASET_ROOT = G.dataset_root_for(task)
        docs = load_docs(G.dataset_root_for(task) / task / "docs")
        suites = []
        for s in sorted(Path(r["task_dir"]).glob("harness/*.spec_tests")):
            refc = OUT / "reference_results" / task / f"{_suite_hash(s)}.json"
            if refc.is_file():
                ref = _read(refc)
            else:
                ref = {_norm_case(k): v for k, v in suite_on_reference(task, s).items()}
                _write(refc, ref, task)
            final = {_norm_case(k): v for k, v in suite_on_repo(task, s, Path(r["canonical"])).items()}
            aud = audit_suite(s, docs, packages=[])
            cites = {_norm_case("spec_tests/" + k): [normalise(q) for q in v] for k, v in aud.citations.items()}
            # citations are text from our own documents-derived suite, kept only as hashes here
            suites.append({"suite": s.name, "cases": sorted(set(final) | set(ref) | set(cites)),
                           "final": final, "reference": ref,
                           "citation_hashes": {k: [hashlib.sha1(q.encode()).hexdigest()[:12] for q in v] for k, v in cites.items()},
                           "cases_with_citations": sum(1 for v in cites.values() if v)})
            (OUT / "citations").mkdir(parents=True, exist_ok=True)
            (OUT / "citations" / f"{r['batch']}__{s.name}.json").write_text(json.dumps(cites), encoding="utf-8")
        _write(p, {"task": task, "batch": r["batch"], "suites": suites}, task)
        print(task, r["batch"], [(x["suite"], len(x["cases"]), x["cases_with_citations"],
                                   sum(v == "fail" for v in x["final"].values()), sum(v == "fail" for v in x["reference"].values()))
                                  for x in suites], flush=True)


def a_split(task: str, batch: str, case: str, labels: dict) -> str:
    """not_tested / tested_not_caught / caught_not_fixed / no_citations, for one failing A-class case."""
    import doc_label
    from orchestra.codeprojecteval.suite_audit import normalise
    from orchestra.control.author.metrics import _cites

    doc_label.DATASET_ROOT = G.dataset_root_for(task)
    paras = [normalise(x) for x in doc_label.doc_paragraphs(task)]
    lab = labels.get(case) or {}
    rest = [paras[i] for i in lab.get("paragraphs") or [] if 0 <= i < len(paras)]
    data = _read(OUT / "suites" / f"{batch}.json")
    any_cites = False
    touching: list[tuple[str, str]] = []
    for s in data["suites"]:
        cites = json.loads((OUT / "citations" / f"{batch}__{s['suite']}.json").read_text(encoding="utf-8"))
        for cid, quotes in cites.items():
            if not quotes:
                continue
            any_cites = True
            if s["reference"].get(cid) == "fail":
                continue                        # a case the reference fails is not a valid gate case
            if any(_cites(q, p) for q in quotes for p in rest):
                touching.append((s["suite"], cid))
    if not any_cites:
        return "no_citations"
    if not rest or not touching:
        return "not_tested"
    final = {(s["suite"], c): v for s in data["suites"] for c, v in s["final"].items()}
    if any(final.get(t) == "fail" for t in touching):
        return "caught_not_fixed"
    return "tested_not_caught"


# --------------------------------------------------------------------------- the sealed judge

JUDGE_SYSTEM = (
    "You are an independent grader. You compare automated tests with a project's design documents and say, for each "
    "test, how precisely the documents specify what the test checks. You never write code and never explain."
)
JUDGE_RULES = """For each test below answer with exactly one letter:
A - the documents explicitly give the specific value, format, exception type or behaviour the test asserts;
B - the documents describe this feature or behaviour, but do not give the specific detail the test asserts;
C - the documents do not describe the behaviour the test checks;
U - the test exercises no identifiable project function, class or module (cannot be graded).
Judge only what the test asserts (inputs the test itself supplies need not appear in the documents).
Reply with one JSON object and nothing else, mapping each test id to its letter, e.g. {"t01": "B", "t02": "A"}."""


def _test_source(task: str, case: str) -> str:
    """The test function (with its class header) and the same-module helpers and fixtures it names."""
    d = G.heldout_dir(task)
    rel, name = case.split("::", 1)
    path = d / rel
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    lines = src.splitlines()

    def seg(node) -> str:
        start = min([node.lineno] + [x.lineno for x in getattr(node, "decorator_list", [])])
        return "\n".join(lines[start - 1:node.end_lineno])

    target, cls = None, None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            target = node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                    target, cls = item, node
    if target is None:
        return ""
    used = {n.id for n in ast.walk(target) if isinstance(n, ast.Name)} | {a.arg for a in target.args.args}
    helpers = []
    pool = list(tree.body)
    conf = path.parent / "conftest.py"
    if conf.is_file():
        try:
            ctree = ast.parse(conf.read_text(encoding="utf-8", errors="replace"))
            clines = conf.read_text(encoding="utf-8", errors="replace").splitlines()
            for node in ctree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in used:
                    start = min([node.lineno] + [x.lineno for x in getattr(node, "decorator_list", [])])
                    helpers.append("# conftest\n" + "\n".join(clines[start - 1:node.end_lineno]))
        except SyntaxError:
            pass
    for node in pool:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in used and node is not cls:
            helpers.append(seg(node))
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in used for t in node.targets):
            helpers.append(seg(node))
    body = (f"class {cls.name}:  # (enclosing class)\n" if cls else "") + seg(target)
    extra = "\n\n".join(helpers)
    return body + ("\n\n# helpers used by the test\n" + extra[:4000] if extra else "")


def _docs_text(task: str) -> str:
    d = G.dataset_root_for(task) / task / "docs"
    return "\n\n".join(f"===== {p.name} =====\n{p.read_text(encoding='utf-8', errors='replace')}"
                       for p in sorted(d.glob("*")) if p.is_file())


def _call(model: str, system: str, user: str) -> tuple[str, dict]:
    import urllib.request

    from orchestra.settings import load_env_file

    load_env_file(ROOT / ".env")
    base = (os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    body = json.dumps({"model": model, "instructions": system, "input": user, "max_output_tokens": 4000})
    req = urllib.request.Request(base + "/responses", data=body.encode(), headers={
        "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}", "Content-Type": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=600).read())
    text = "".join(c.get("text", "") for o in d.get("output") or [] for c in (o.get("content") or []) if isinstance(c, dict))
    return text, d.get("usage") or {}


def judge_sample(task: str, failing: set[str], labels_file: dict, n: int = 40, seed: int = 20261015) -> list[str]:
    """All failing cases when <= n, else n stratified by milestone (proportional, at least one per stratum)."""
    cases = sorted(failing)
    if len(cases) <= n:
        return cases
    attr = next(iter(labels_file["attribution"].values()))
    ms = {c: m for m, cs in attr.items() for c in cs}
    strata: dict[str, list[str]] = defaultdict(list)
    for c in cases:
        strata[ms.get(c, "__unattributed__")].append(c)
    rng = random.Random(f"{seed}:{task}")
    quota = {m: max(1, round(n * len(v) / len(cases))) for m, v in strata.items()}
    while sum(quota.values()) > n:
        quota[max(quota, key=lambda m: quota[m])] -= 1
    while sum(quota.values()) < n:
        m = max(strata, key=lambda m: len(strata[m]) - quota[m])
        quota[m] += 1
    out = []
    for m, v in sorted(strata.items()):
        out += rng.sample(sorted(v), min(len(v), quota[m]))
    return sorted(out)


def failing_by_task() -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for r in included():
        res = _read(OUT / "cases" / f"{r['batch']}.json")
        out[r["task"]] |= {k for k, v in res["cases"].items() if v == "fail"}
    return out


def _window_reader(model: str):
    """Percent used of ``model``'s window on quota.sh (the window that moves with this model's use)."""
    import joint_experiment as JE

    def read() -> float | None:
        try:
            out = subprocess.run([JE.QUOTA], capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        block = out.split(model, 1)[-1] if model in out else ""
        m = re.search(r"window\s*:\s*(\d+(?:\.\d+)?)% used", block)
        return float(m.group(1)) if m else None

    return read


def cmd_judge(a) -> None:
    import joint_experiment as JE
    from quota_spend import Spend

    SEALED_LOG.mkdir(parents=True, exist_ok=True)
    JE.read_window = _window_reader(a.model)     # the cap reads the judge model's own window (2026-10-15: gpt-5.6-luna)
    spend = Spend()
    token_cap = int(a.cap * 2_000_000)          # calibration on this channel: about 2M tokens per point
    calls = 0
    res_p = OUT / "judge.json"
    state = _read(res_p) if res_p.is_file() else {"labels": {}, "calls": [], "stopped": ""}
    tokens = int(state.get("tokens") or 0)
    for task, failing in sorted(failing_by_task().items()):
        lf = _read(OUT / "labels" / f"{task}.json")
        sample = judge_sample(task, failing, lf)
        todo = [c for c in sample if c not in state["labels"].get(task, {})]
        docs = _docs_text(task)
        for i in range(0, len(todo), a.batch):
            if spend.poll() >= a.cap or tokens >= token_cap:
                state["stopped"] = f"cap reached: {spend.used()} points read, {tokens} tokens"
                _write(res_p, state)
                print(state["stopped"])
                return
            chunk = todo[i:i + a.batch]
            ids = {f"t{j + 1:02d}": c for j, c in enumerate(chunk)}
            tests = "\n\n".join(f"### test {tid}\n```python\n{_test_source(task, c)}\n```" for tid, c in ids.items())
            user = f"{JUDGE_RULES}\n\n## Design documents\n\n{docs}\n\n## Tests\n\n{tests}"
            try:
                text, usage = _call(a.model, JUDGE_SYSTEM, user)
            except Exception as exc:  # noqa: BLE001 -- stop at the first failure: repeated failures trip the proxy's counter
                detail = ""
                if hasattr(exc, "read"):
                    try:
                        detail = exc.read().decode(errors="replace")[:300]
                    except Exception:  # noqa: BLE001
                        detail = ""
                state["calls"].append({"task": task, "n": len(chunk), "error": f"{type(exc).__name__} {getattr(exc, 'code', '')}",
                                       "detail": detail})
                state["stopped"] = "first call failure: " + detail[:200]
                _write(res_p, state)
                print(task, "call failed", type(exc).__name__, getattr(exc, "code", ""), detail, flush=True)
                return
            calls += 1
            tokens += int(usage.get("total_tokens") or 0)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            (SEALED_LOG / f"{stamp}_{task}_{i}.json").write_text(json.dumps({"ids": ids, "prompt": user, "reply": text, "usage": usage}))
            m = re.search(r"\{.*\}", text, re.S)
            verdicts = {}
            try:
                verdicts = json.loads(m.group(0)) if m else {}
            except ValueError:
                pass
            got = {ids[t]: str(v).strip().upper()[:1] for t, v in verdicts.items() if t in ids and str(v).strip().upper()[:1] in "ABCU"}
            state["labels"].setdefault(task, {}).update(got)
            state["calls"].append({"task": task, "n": len(chunk), "answered": len(got), "tokens": int(usage.get("total_tokens") or 0),
                                   "model": a.model})
            state["spend_points_read"] = spend.used()
            state["tokens"] = tokens
            _write(res_p, state)
            print(task, f"{len(got)}/{len(chunk)}", usage.get("total_tokens"), "points", spend.used(), flush=True)
    state["stopped"] = state.get("stopped") or "done"
    state["spend_points_read"] = spend.poll()
    _write(res_p, state)
    print("judge done:", calls, "calls", tokens, "tokens", spend.used(), "points")


# --------------------------------------------------------------------------- summary


SCRIPT_TO_LETTER = {"detail_specified": "A", "behaviour_only": "B", "not_specified": "C", "unknown": "U"}


def cmd_summary(_a) -> None:
    runs = included()
    judge = (_read(OUT / "judge.json").get("labels") if (OUT / "judge.json").is_file() else {}) or {}
    facts = _read(OUT / "facts.json") if (OUT / "facts.json").is_file() else {}
    rows = []
    for r in runs:
        task = r["task"]
        lf = _read(OUT / "labels" / f"{task}.json")
        labels = lf["labels"]
        res = _read(OUT / "cases" / f"{r['batch']}.json")
        failing = sorted(k for k, v in res["cases"].items() if v == "fail")
        script = Counter(SCRIPT_TO_LETTER.get((labels.get(c) or {}).get("depth", "unknown"), "U") for c in failing)
        jl = judge.get(task, {})
        judged = [c for c in failing if c in jl]
        jc = Counter(jl[c] for c in judged)
        a_script = [c for c in failing if (labels.get(c) or {}).get("depth") == "detail_specified"]
        a_judge = [c for c in judged if jl[c] == "A"]
        split_s = Counter(a_split(task, r["batch"], c, labels) for c in a_script) if (OUT / "suites" / f"{r['batch']}.json").is_file() else {}
        split_j = Counter(a_split(task, r["batch"], c, labels) for c in a_judge) if (OUT / "suites" / f"{r['batch']}.json").is_file() else {}
        rows.append({"task": task, "batch": r["batch"], "series": r["series"], "milestones": r["milestones"],
                     "heldout_case_pass_rate": res["case_pass_rate"], "stored_heldout": r["stored_heldout"],
                     "cases": len(res["cases"]), "failing": len(failing), "script": dict(script),
                     "judged": len(judged), "judge": dict(jc), "a_split_script": dict(split_s), "a_split_judge": dict(split_j),
                     "facts": facts.get(task, {})})
    agree = {}
    for task, jl in judge.items():
        labels = _read(OUT / "labels" / f"{task}.json")["labels"]
        pairs = [(SCRIPT_TO_LETTER.get((labels.get(c) or {}).get("depth", "unknown"), "U"), v) for c, v in jl.items()]
        agree[task] = {"n": len(pairs), "three_level": round(sum(a == b for a, b in pairs) / len(pairs), 4) if pairs else None,
                       "a_vs_not": round(sum((a == "A") == (b == "A") for a, b in pairs) / len(pairs), 4) if pairs else None,
                       "confusion": dict(Counter(f"{a}->{b}" for a, b in pairs))}
    # variability: pairwise disagreement of case outcomes between runs of one task (same case set)
    var = {}
    by_task = defaultdict(list)
    for r in runs:
        by_task[r["task"]].append(_read(OUT / "cases" / f"{r['batch']}.json")["cases"])
    for task, rs in by_task.items():
        ds = []
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                ks = set(rs[i]) & set(rs[j])
                ds.append(round(sum(rs[i][k] != rs[j][k] for k in ks) / max(1, len(ks)), 4))
        var[task] = {"runs": len(rs), "pairwise_disagreement": ds}
    _write(OUT / "summary.json", {"rows": rows, "agreement": agree, "variability": var})
    for row in rows:
        print(json.dumps(row))
    print(json.dumps(agree))
    print(json.dumps(var))


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("runs", cmd_runs), ("cases", cmd_cases), ("labels", cmd_labels), ("facts", cmd_facts),
                     ("split", cmd_split), ("summary", cmd_summary)):
        sub.add_parser(name).set_defaults(fn=fn)
    j = sub.add_parser("judge")
    j.add_argument("--cap", type=float, default=1.0)
    j.add_argument("--batch", type=int, default=10)
    j.add_argument("--model", default="gpt-5.6-luna")   # gpt-5.5 withdrawn from the channel 2026-10-15
    j.set_defaults(fn=cmd_judge)
    a = ap.parse_args()
    a.fn(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
