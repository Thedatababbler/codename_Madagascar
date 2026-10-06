#!/usr/bin/env python3
"""Run an authored / evaluation suite on the dataset's reference implementation and
report the case ids it fails there (spec §5 item 4). Training tasks only; the
reference never enters a workspace. Output: case ids and counts.

    uv run python scripts/sealed/reference_check.py --task tinydb --suite <dir> [--out <json>]
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import reference_root, task_python  # noqa: E402

_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")
_IGNORE = ("bin", "lib", "lib64", "include", "share", "pyvenv.cfg", ".venv", "venv", "__pycache__", ".git", "unit_tests", "check_tests")


def _collect(python: Path, suite: Path, cwd: Path) -> list[str]:
    try:
        proc = subprocess.run([str(python), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=", str(suite)],
                              cwd=cwd, capture_output=True, text=True, timeout=300, check=False)
    except subprocess.TimeoutExpired:
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if "::" in ln]


def suite_on_reference(task: str, suite: Path, *, per_test_timeout: int = 60, timeout: int = 900) -> dict[str, str]:
    """``{case_id: pass|fail}`` of ``suite`` run on the reference copy (suite mounted as spec_tests)."""
    python = task_python(task)
    if not python.is_file():
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / task
        shutil.copytree(reference_root(task), ref, ignore=shutil.ignore_patterns(*_IGNORE), symlinks=True)
        holder = Path(tmp) / "specroot"
        holder.mkdir()
        shutil.copytree(suite, holder / "spec_tests", ignore=shutil.ignore_patterns("__pycache__"))
        try:
            proc = subprocess.run(
                [str(python), "-m", "pytest", str(holder / "spec_tests"), "-q", "-rA", "--no-header", "-p", "no:cacheprovider",
                 "-o", "addopts=", "--continue-on-collection-errors", f"--timeout={per_test_timeout}", "--timeout-method=signal"],
                cwd=ref, capture_output=True, text=True, timeout=timeout, check=False,
                env={"PYTHONPATH": os.pathsep.join(p for p in [str(holder), str(ref), str(ref / "src") if (ref / "src").is_dir() else ""] if p),
                     "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(ref), "PYTHONDONTWRITEBYTECODE": "1"},
            )
            stdout = proc.stdout
        except subprocess.TimeoutExpired as exc:
            # a suite that hangs on the reference (a socket test that never returns): keep what was
            # reported; every case it never reached is a reference failure
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stdout += "\n" + "\n".join(f"FAILED {c}" for c in _collect(python, holder / "spec_tests", ref))
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        m = _LINE.match(line)
        if m:
            node = m.group(2)
            node = "spec_tests/" + node.split("spec_tests/", 1)[-1] if "spec_tests/" in node else node
            out[node] = "pass" if m.group(1) in ("PASSED", "XPASS") else "fail"
    return out


def probe_audit_on_reference(task: str, suite: Path, docs_text: str) -> dict:
    """What ``scripts/author_probe.py audit`` reports about the reference, computed here.

    Counts, the reference-failing case names (our suite's names, not held-out), whether
    each failing case quotes a document sentence, and the *exception type* that dominates
    the failures -- never the error line itself, which can carry reference values.
    """
    import re as _re
    from collections import Counter as _Counter
    python = task_python(task)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task
        shutil.copytree(reference_root(task), repo, dirs_exist_ok=True, symlinks=True, ignore=shutil.ignore_patterns(*_IGNORE))
        shutil.copytree(suite, repo / "spec_tests", ignore=shutil.ignore_patterns("__pycache__"))
        roots = [str(repo)] + ([str(repo / "src")] if (repo / "src").is_dir() else [])
        try:
            proc = subprocess.run(
                [str(python), "-m", "pytest", str(repo / "spec_tests"), "-q", "-p", "no:cacheprovider",
                 "-o", "addopts=", "--continue-on-collection-errors", "-rfE", "--timeout=120"],
                cwd=repo, capture_output=True, text=True, timeout=1200,
                env={**os.environ, "PYTHONPATH": os.pathsep.join(roots)},
            )
            out = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired:
            out = "TIMEOUT"
    counts = {k: 0 for k in ("passed", "failed", "error")}
    for k in counts:
        m = _re.search(rf"(\d+) {k}", out)
        if m:
            counts[k] = int(m.group(1))
    total = sum(counts.values())
    failing = _re.findall(r"(?:FAILED|ERROR) [^ \n]*::(\S+)", out)
    collection_errors = len(_re.findall(r"^ERROR [^:\n]+\.py\s*$", out, _re.M))
    backed, unsupported = [], []
    for name in failing:
        base = name.split("[")[0].split("::")[-1]
        quoted = None
        for path in suite.glob("*.py"):
            lines = path.read_text(errors="replace").splitlines()
            for i, line in enumerate(lines):
                if _re.match(rf"\s*(async\s+)?def {_re.escape(base)}\(", line):
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
        quotes = [_re.sub(r"^(PRD|Architecture|UML|DOC)[^\"]*\"|\"\s*$", "", c).strip('"').strip() for c in (quoted or [])]
        (backed if any(len(q) > 20 and " ".join(q.split()) in docs_text for q in quotes) else unsupported).append(base)
    types = _Counter(m.group(1) for m in _re.finditer(r"^E\s+([A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit))\b", out, _re.M))
    dominant = types.most_common(1)[0] if types else ("", 0)
    return {
        "ref_passed": counts["passed"], "ref_total": total,
        "ref_validity": round(counts["passed"] / total, 2) if total else None,
        "ref_collection_errors": collection_errors,
        "ref_fail_doc_backed": len(set(backed)), "ref_fail_unsupported": len(set(unsupported)),
        "ref_dominant_error": dominant[0] if dominant[1] >= 3 else "",
        "ref_unsupported_examples": sorted(set(unsupported))[:5],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--suite", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    res = suite_on_reference(args.task, args.suite)
    failed = sorted(k for k, v in res.items() if v == "fail")
    rec = {"task": args.task, "suite": str(args.suite), "total": len(res), "reference_failed": failed}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rec, indent=1), encoding="utf-8")
    print(json.dumps(rec))


if __name__ == "__main__":
    main()
