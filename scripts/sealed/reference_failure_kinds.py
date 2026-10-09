#!/usr/bin/env python3
"""Why a suite fails on the reference: counts of exception class names only (no source, no message text).

    uv run python scripts/sealed/reference_failure_kinds.py --task imapclient --suite <dir>
"""

from __future__ import annotations

import argparse
import collections
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

_EXC = re.compile(r"^E\s+([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Exit|Interrupt|Failed|Warning))\b", re.M)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--suite", required=True, type=Path)
    a = ap.parse_args()
    python = task_python(a.task)
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / a.task
        shutil.copytree(reference_root(a.task), ref, ignore=shutil.ignore_patterns(*_IGNORE), symlinks=True)
        holder = Path(tmp) / "specroot"
        holder.mkdir()
        shutil.copytree(a.suite, holder / "spec_tests", ignore=shutil.ignore_patterns("__pycache__"))
        proc = subprocess.run(
            [str(python), "-m", "pytest", str(holder / "spec_tests"), "-q", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
             "--continue-on-collection-errors", "--timeout=60", "--timeout-method=signal", "--tb=short"],
            cwd=ref, capture_output=True, text=True, timeout=900, check=False,
            env={"PYTHONPATH": os.pathsep.join(p for p in [str(holder), str(ref), str(ref / "src") if (ref / "src").is_dir() else ""] if p),
                 "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(ref), "PYTHONDONTWRITEBYTECODE": "1"})
    kinds = collections.Counter(m.split(".")[-1] for m in _EXC.findall(proc.stdout))
    # attribute names come from the suite's own calls, not from the reference
    attrs = collections.Counter(re.findall(r"has no attribute '([A-Za-z_][A-Za-z0-9_]*)'", proc.stdout))
    owners = collections.Counter(re.findall(r"AttributeError: '?([A-Za-z_][A-Za-z0-9_.]*)'? (?:object|module) has no attribute", proc.stdout))
    print(json.dumps({"task": a.task, "exception_kinds": dict(kinds.most_common()), "missing_attributes": dict(attrs.most_common(10)),
                      "attribute_owners": dict(owners.most_common(5))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
