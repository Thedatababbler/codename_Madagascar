#!/usr/bin/env python3
"""Grade the authored suites themselves, offline, with the reference in hand.

A suite is the milestone's yardstick, so its quality decides what the search
optimises. Two questions per frozen suite, neither answerable from inside a
run: is it *valid* -- does it pass against the dataset's reference
implementation, which never enters a workspace -- and is it *deep* -- does it
drive the structure into the regimes the held-out grades (bulk operations,
structural transitions, persistence round-trips)? A test that fails on the
reference asserts behaviour the documents do not specify; a suite with no
bulk operation cannot see a split-path bug (EXP-20260904-02).

    uv run python scripts/audit_authored_suites.py <batch_dir>/<task> [--task T]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DATASET = Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
ENVS = Path("/root/codex-benchmarks/cpe_envs")

DEPTH_PATTERNS = {
    "bulk_loop_max": r"range\((\d+)\)",
    "parametrize": r"@pytest\.mark\.parametrize",
    "split_or_merge": r"\bsplit|\bmerge|overflow|rebalanc",
    "reopen_or_persist": r"reopen|close\(\)|persist|round.?trip|reload|flush",
    "error_paths": r"pytest\.raises",
    "boundary": r"\bempty\b|boundary|\bmax|\bmin\b|zero|limit",
}


def _run_suite(repo: Path, suite: Path, py: Path) -> tuple[int, int, str]:
    holder = Path(tempfile.mkdtemp(prefix="audit_specroot_"))
    (holder / "spec_tests").symlink_to(suite.resolve(), target_is_directory=True)
    proc = subprocess.run(
        [str(py), "-m", "pytest", str(suite.resolve()), "-q", "-p", "no:cacheprovider",
         "-o", "addopts=", "--continue-on-collection-errors", "-rfE"],
        cwd=repo, capture_output=True, text=True, timeout=900,
        env={**__import__("os").environ, "PYTHONPATH": f"{holder}:{repo}"},
    )
    out = proc.stdout + proc.stderr
    counts = {k: 0 for k in ("passed", "failed", "error")}
    for k in counts:
        m = re.search(rf"(\d+) {k}", out)
        if m:
            counts[k] = int(m.group(1))
    return counts["passed"], sum(counts.values()), out


def _reference_repo(task: str) -> Path:
    """The dataset's own implementation, copied without its tests."""
    dest = Path(tempfile.mkdtemp(prefix=f"audit_ref_{task}_"))
    shutil.copytree(DATASET / task, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("unit_tests", "check_tests", "__pycache__", ".git"))
    return dest


def _doc_text(task: str) -> str:
    docs = DATASET / task / "docs"
    return " ".join(
        " ".join(p.read_text(errors="replace").split()) for p in docs.glob("*") if p.is_file()
    )


def classify(suite: Path, failing: list[str], task: str) -> tuple[list[str], list[str]]:
    """Split reference failures by whether the test quotes a real sentence."""
    docs = _doc_text(task)
    backed, unsupported = [], []
    for name in failing:
        base = name.split("[")[0]
        quoted = None
        for path in suite.glob("*.py"):
            lines = path.read_text(errors="replace").splitlines()
            for i, line in enumerate(lines):
                if re.match(rf"\s*def {re.escape(base)}\(", line):
                    comments = []
                    j = i - 1
                    while j >= 0 and lines[j].strip().startswith("#"):
                        comments.append(lines[j].strip("# ").strip())
                        j -= 1
                    quoted = comments
                    break
            if quoted is not None:
                break
        quotes = [re.sub(r"^(PRD|Architecture|UML)[^\"]*\"|\"\s*$", "", c).strip('"').strip() for c in (quoted or [])]
        hit = any(len(q) > 20 and " ".join(q.split()) in docs for q in quotes)
        (backed if hit else unsupported).append(name)
    return backed, unsupported


def depth(suite: Path) -> dict:
    text = "\n".join(p.read_text(errors="replace") for p in suite.glob("*.py"))
    cases = len(re.findall(r"^\s*def test_", text, re.M))
    out = {"cases": cases}
    for key, pat in DEPTH_PATTERNS.items():
        hits = re.findall(pat, text, re.I)
        out[key] = max((int(h) for h in hits), default=0) if key == "bulk_loop_max" else len(hits)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path, help="<batch>/<task> directory of one run")
    ap.add_argument("--task", default=None)
    args = ap.parse_args()
    task = args.task or args.run_dir.name
    py = ENVS / task / "bin" / "python"
    harness = args.run_dir / "harness"
    ref = _reference_repo(task)
    # Each suite is graded against the workspace of its own milestone: the
    # sort order of milestone names is not their execution order.
    by_milestone = {p.parent.name: p for p in (args.run_dir / "tasks").glob("*/workspaces/*/repo")}

    rows = []
    for suite in sorted(harness.glob("*.spec_tests")):
        if not suite.is_dir():
            continue
        milestone = suite.name.replace(".spec_tests", "")
        v_pass, v_total, v_out = _run_suite(ref, suite, py)
        own = by_milestone.get(milestone)
        d_pass, d_total, _ = _run_suite(own, suite, py) if own else (0, 0, "")
        failing = re.findall(r"(?:FAILED|ERROR) [^ ]*::(\S+)", v_out)
        collection_errors = len(re.findall(r"^ERROR [^:\n]+\.py\s*$", v_out, re.M))
        # A failure on the reference is invention only when the test's quoted
        # sentence is not in the documents. Where it is, the reference deviates
        # from its own specification and the author was right to assert it.
        backed, unsupported = classify(suite, failing, task)
        rows.append({
            "milestone": milestone,
            "validity": f"{v_pass}/{v_total}",
            "collection_errors": collection_errors,
            "doc_backed_but_reference_disagrees": backed[:8],
            "unsupported_by_docs": unsupported[:8],
            "on_delivered": f"{d_pass}/{d_total}",
            **depth(suite),
        })
    print(json.dumps({"task": task, "suites": rows}, indent=2))


if __name__ == "__main__":
    main()
