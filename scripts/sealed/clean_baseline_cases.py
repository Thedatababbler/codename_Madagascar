#!/usr/bin/env python3
"""Per-case held-out results of several batches of one task, with a failure category (clean-baseline analysis, 2026-10-14).

Runs the held-out suite on each batch's committed canonical repository exactly as
``heldout_case_diff.py`` does (scratch copy, dataset unit_tests overlaid, the task's
interpreter, confined with the network rules), and keeps per case only:

- the case id, its outcome (pass / fail / missing), the milestone
  ``heldout_attribution.py`` assigned it to;
- a failure category from the exception type pytest reports: import, attr_name,
  type, assertion, timeout, other. A case that was not collected takes the category
  of its module's collection error.

No message text, no source, no assertion detail leaves this script.

    uv run python scripts/sealed/clean_baseline_cases.py --task rsa --batch <batch> [--batch <batch> ...] \\
        --attribution outputs/evolution/sealed/attribution/rsa.json --out <json>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import DATASET_ROOT, ENV_ROOT, heldout_dir  # noqa: E402

from orchestra.codeprojecteval.dataset import load_task  # noqa: E402
from orchestra.sandbox.guard import run_confined  # noqa: E402

_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)(?: - (\S+)(?: (\S+ \S+ \S+))?)?")
EXC_TYPES: dict[str, int] = {}          # exception class names only, for checking the categories


def category(exc: str) -> str:
    e = (exc or "").rstrip(":")
    if e.endswith(("ImportError", "ModuleNotFoundError")):
        return "import"
    if e.endswith(("AttributeError", "NameError")):
        return "attr_name"
    if e.endswith("TypeError"):
        return "type"
    if e.endswith("AssertionError") or e == "assert":
        return "assertion"
    if "Timeout" in e or e.endswith("Failed") and "imeout" in exc:
        return "timeout"
    return "other"


def _junit_types(xml: Path) -> dict[str, str]:
    """'<test name>@<module stem>' -> exception type of its failure or error (the type attribute only)."""
    import xml.etree.ElementTree as ET

    out: dict[str, str] = {}
    if not xml.is_file():
        return out
    for tc in ET.parse(xml).getroot().iter("testcase"):
        bad = tc.find("failure")
        if bad is None:
            bad = tc.find("error")
        if bad is None:
            continue
        typ = bad.get("type") or ""
        msg = bad.get("message") or ""
        if not typ:
            # unittest-style failures carry no type attribute; the message starts with the exception class name.
            # Only that name is kept, never the message.
            mt = re.match(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Failed|Warning|Timeout))\b", msg)
            typ = mt.group(1) if mt else ("AssertionError" if msg.lstrip().startswith("assert") else "")
        if typ.endswith("Failed") and "DID NOT RAISE" in msg:
            typ = "AssertionError"
        elif typ.endswith("Failed") and "Timeout" in msg:
            typ = "Timeout"
        cls = (tc.get("classname") or "").split(".")
        stem = next((c for c in cls if c.startswith("test")), cls[-1] if cls else "")
        out[(tc.get("name") or "").split("[", 1)[0] + "@" + stem] = typ
    return out


def per_case(task_id: str, batch: Path, *, per_test_timeout: int = 30, timeout: int = 7200) -> tuple[dict, dict]:
    task = load_task(task_id, dataset_root=DATASET_ROOT)
    committed = sorted((batch / task_id / "tasks").glob("*/canonical/repo"))
    workspace = committed[0] if committed else batch / "workspaces" / task_id
    python = ENV_ROOT / task_id / "bin" / "python"
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task_id
        shutil.copytree(workspace, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "spec_tests",
                                                                       "repair_evidence", "unit_tests", "check_tests"))
        if (repo / task.unit_tests).exists():
            shutil.rmtree(repo / task.unit_tests)
        shutil.copytree(heldout_dir(task_id), repo / task.unit_tests)
        xml = Path(tmp) / "report.xml"
        proc = run_confined(
            [str(python), "-m", "pytest", task.unit_tests, "-q", "-rA", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
             "-o", "junit_family=xunit1", f"--junitxml={xml}",
             "--continue-on-collection-errors", f"--timeout={per_test_timeout}", "--timeout-method=signal"],
            cwd=repo, mode="scoring", role="clean_baseline_cases", capture_output=True, text=True, timeout=timeout, check=False,
            env={"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
                 "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        types = _junit_types(xml)
    cases: dict[str, list[str]] = {}
    modules: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        status, node, exc = m.group(1), m.group(2), m.group(3) or ""
        if exc == "Failed:" and (m.group(4) or "").startswith("DID NOT RAISE"):
            exc = "AssertionError"                  # pytest.raises that did not raise: an assertion on behaviour
        if "::" not in node:                       # a module that failed to collect
            if status == "ERROR":
                modules[node] = category(exc)
            continue
        base = node.split("[", 1)[0]
        ok = status in ("PASSED", "XPASS")
        if status == "SKIPPED":
            continue
        if not exc or exc == "Failed:":
            exc = types.get(node.split("[", 1)[0].split("::")[-1] + "@" + Path(node.split("::")[0]).stem, exc)
        if status in ("FAILED", "ERROR"):
            key = exc.rstrip(":").split(".")[-1]
            EXC_TYPES[key] = EXC_TYPES.get(key, 0) + 1
        prev = cases.get(base)
        if prev is None or prev[0] == "pass":       # parametrisations folded: any failure fails the case
            cases[base] = ["pass", ""] if ok else ["fail", category(exc)]
    return cases, modules


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--batch", action="append", type=Path, required=True)
    ap.add_argument("--attribution", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    data = json.loads(a.attribution.read_text(encoding="utf-8"))
    def norm(c: str) -> str:
        f, _, rest = c.partition("::")
        return Path(f).name + "::" + rest.split("[", 1)[0].split("::")[-1]      # attribution ids carry no class

    attribution = {norm(c): mid for mid, cs in (data.get("attribution") or data).items() if isinstance(cs, list) for c in cs}
    runs = {}
    for b in a.batch:
        cases, modules = per_case(a.task, b)
        runs[str(b)] = {"cases": cases, "collection_errors": modules}
    allc = sorted({c for r in runs.values() for c in r["cases"]})
    for r in runs.values():
        for c in allc:
            if c not in r["cases"]:
                mod = c.split("::", 1)[0]
                r["cases"][c] = ["missing", next((v for k, v in r["collection_errors"].items() if k.endswith(mod) or mod.endswith(k)), "other")]
    out = {"task": a.task, "cases": allc, "milestone_of": {c: attribution.get(norm(c), "?") for c in allc},
           "runs": {b: r["cases"] for b, r in runs.items()},
           "pass_counts": {b: sum(v[0] == "pass" for v in r["cases"].values()) for b, r in runs.items()},
           "exception_types": EXC_TYPES}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({"task": a.task, "cases": len(allc), "pass_counts": out["pass_counts"], "exception_types": EXC_TYPES}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
