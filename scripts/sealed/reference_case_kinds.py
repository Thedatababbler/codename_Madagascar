#!/usr/bin/env python3
"""Per-case result of an authored suite on the reference implementation, with a failure category
(author fake-object fix, test A1). Training tasks only.

Output per case: pass|fail, the exception class, and one category:
- fake_object: the test's own double does not match the real interface (AttributeError on a
  Fake*/Mock/stub object, "unexpected keyword argument", wrong positional count against a test-defined callable);
- environment: missing executable / file / permission / exit 127 / CalledProcessError / timeout / missing module;
- assertion: the reference returns a different value than the test expects (often a detail the documents leave open);
- other.

No message text, path or source line leaves this script: case ids (our suite's), class names, categories.

    uv run python scripts/sealed/reference_case_kinds.py --task cookiecutter --suite <dir> --out <json>
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import reference_root, task_python  # noqa: E402
from reference_check import _IGNORE  # noqa: E402

_RES = re.compile(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL|SKIPPED) (\S+)")
_HDR = re.compile(r"^_{3,} (?:ERROR at \w+ of )?(\S+?) _{3,}$")
_EXC = re.compile(r"^(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Exit|Failed|Warning|Interrupt))\b", re.M)


def classify(block: str) -> tuple[str, str]:
    m = _EXC.findall(block)
    exc = m[0].split(".")[-1] if m else ""
    low = block.lower()
    env_exc = {"FileNotFoundError", "PermissionError", "CalledProcessError", "ModuleNotFoundError", "TimeoutError",
               "ConnectionRefusedError", "NotADirectoryError", "IsADirectoryError"}
    if exc in env_exc or "+++ timeout" in low or "failed: timeout" in low or "exit status 127" in low \
            or "no such file or directory" in low or "command not found" in low:
        return exc, "environment"
    if exc in ("TypeError", "AttributeError") and (
            re.search(r"'(fake|mock|stub|dummy|recording|scripted)\w*' object", low)
            or re.search(r"\b_?(fake|stub|dummy|recording|scripted|mock)\w*\(\) (takes|got|missing)", low)
            or re.search(r"<locals>\.\w+\(\) (takes|got|missing)", low) or "settimeout" in low):
        return exc, "fake_object"
    if exc == "AssertionError":
        return exc, "assertion"
    return exc, "other"


def run(task: str, suite: Path) -> dict:
    import xml.etree.ElementTree as ET

    python = task_python(task)
    cases: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / task
        shutil.copytree(reference_root(task), ref, ignore=shutil.ignore_patterns(*_IGNORE), symlinks=True)
        holder = Path(tmp) / "specroot"
        holder.mkdir()
        shutil.copytree(suite, holder / "spec_tests", ignore=shutil.ignore_patterns("__pycache__"))
        tk = Path(__file__).resolve().parents[2] / "src" / "orchestra" / "harness" / "testkit"
        paths = [str(holder), str(ref), str(ref / "src") if (ref / "src").is_dir() else "", str(tk) if tk.is_dir() else ""]
        xml = Path(tmp) / "report.xml"
        try:
            subprocess.run(
                [str(python), "-m", "pytest", str(holder / "spec_tests"), "-q", "--no-header", "-p", "no:cacheprovider",
                 "-o", "addopts=", "-o", "junit_family=xunit1", "--continue-on-collection-errors", "--timeout=60",
                 "--timeout-method=signal", "--tb=short", f"--junitxml={xml}"],
                cwd=ref, capture_output=True, text=True, timeout=1500, check=False,
                env={"PYTHONPATH": os.pathsep.join(p for p in paths if p), "PATH": f"{python.parent}:/usr/bin:/bin",
                     "HOME": str(ref), "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired:
            pass
        if not xml.is_file():
            return {"task": task, "total": 0, "cases": {}, "note": "no report (suite timed out or crashed)"}
        for tc in ET.parse(xml).getroot().iter("testcase"):
            cls = tc.get("classname") or ""
            name = (tc.get("name") or "").split("[", 1)[0]
            parts = cls.split(".")
            if "spec_tests" in parts:
                parts = parts[parts.index("spec_tests") + 1:]
            # module path + optional class, as pytest spells the node id
            mod_parts, klass = [], []
            for i, p_ in enumerate(parts):
                if p_.startswith("test") or i < len(parts) - 1 and not p_[:1].isupper():
                    mod_parts.append(p_)
                else:
                    klass = parts[i:]
                    break
            node = "spec_tests/" + "/".join(mod_parts) + ".py::" + "::".join([*klass, name])
            bad = next((e for e in tc if e.tag in ("failure", "error")), None)
            if bad is None:
                if node not in cases:
                    cases[node] = {"ref": "pass"}
                continue
            block = (bad.get("message") or "") + "\n" + (bad.text or "")
            exc, kind = classify(block)
            cases[node] = {"ref": "fail", "exc": exc or (bad.get("type") or "").split(".")[-1], "kind": kind}
    return {"task": task, "total": len(cases), "cases": cases}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--suite", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    res = run(a.task, a.suite)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps({"task": a.task, "total": res["total"], "failed": sum(1 for c in res["cases"].values() if c["ref"] == "fail")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
