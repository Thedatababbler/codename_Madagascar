#!/usr/bin/env python3
"""Which authored tests actually separate the candidates?

The aggregate probe reported r = -0.204 over nine candidates whose authored scores
spanned 0.069. A score built mostly from tests that every candidate fails carries a
constant, and a constant cannot rank anything: the ranking would then rest on the
handful of tests that moved, which is a much weaker claim than the aggregate number
looks like. This splits the suite into the part that is dead weight (all candidates
fail, or all pass) and the part that discriminates, and re-correlates using only the
latter.

Offline: reuses the suite and patches the aggregate probe already produced.

    uv run python scripts/probe_spec_axis_pertest.py
"""

from __future__ import annotations

import json
import shutil
import statistics as st
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from probe_spec_axis_signal import (
    HIDDEN_PER_CANDIDATE,
    PROBE_DIR,
    TASK_ID,
    _candidate_repos,
)

from orchestra.codeprojecteval import load_task
from orchestra.codeprojecteval.dataset import DEFAULT_ENV_ROOT, build_agent_workspace


def outcomes(python: Path, repo: Path) -> dict[str, bool]:
    """nodeid -> passed, for every test the suite collected in `repo`."""
    report = repo / "junit.xml"
    subprocess.run(
        [
            str(python), "-m", "pytest", "spec_tests", "-q", "--no-header",
            "-p", "no:cacheprovider", "-o", "addopts=",
            "--continue-on-collection-errors", "--timeout=30", "--timeout-method=signal",
            f"--junitxml={report}",
        ],
        cwd=repo,
        env={
            "PYTHONPATH": str(repo),
            "PATH": f"{python.parent}:/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if not report.is_file():
        return {}
    result: dict[str, bool] = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        nodeid = f"{case.get('classname')}::{case.get('name')}"
        bad = any(child.tag in {"failure", "error"} for child in case)
        result[nodeid] = not bad
    return result


def main() -> int:
    suite = PROBE_DIR / "spec_tests"
    if not suite.is_dir():
        raise SystemExit(f"no authored suite at {suite}; run the aggregate probe first")
    python = DEFAULT_ENV_ROOT / TASK_ID / "bin" / "python"
    task = load_task(TASK_ID)
    hidden = json.loads(HIDDEN_PER_CANDIDATE.read_text(encoding="utf-8"))

    per_candidate: dict[str, dict[str, bool]] = {}
    for key, repo in _candidate_repos().items():
        shutil.copytree(suite, repo / "spec_tests", dirs_exist_ok=True)
        per_candidate[key] = outcomes(python, repo)

    pristine_root = Path(tempfile.mkdtemp(prefix="spec-axis-pristine-"))
    pristine = build_agent_workspace(task, pristine_root / "repo")
    shutil.copytree(suite, pristine / "spec_tests")
    baseline = outcomes(python, pristine)

    every = sorted({n for out in per_candidate.values() for n in out})
    always_fail = [n for n in every if not any(out.get(n, False) for out in per_candidate.values())]
    always_pass = [n for n in every if all(out.get(n, False) for out in per_candidate.values())]
    varying = [n for n in every if n not in set(always_fail) | set(always_pass)]

    print(f"authored 测试总数: {len(every)}  (基线可跑 {len(baseline)})")
    print(f"  所有候选都失败: {len(always_fail)}  <- 常数,对排序无贡献")
    print(f"  所有候选都通过: {len(always_pass)}  <- 常数,对排序无贡献")
    print(f"  有区分度:       {len(varying)}\n")

    if varying:
        print("区分性测试:")
        for n in varying:
            passers = [k for k, out in sorted(per_candidate.items()) if out.get(n)]
            print(f"  {n}\n      通过 {len(passers)}/{len(per_candidate)}: {', '.join(passers)}")
        print()

    rows = []
    for key, out in sorted(per_candidate.items()):
        passed = sum(1 for n in varying if out.get(n))
        score = passed / len(varying) if varying else None
        hid = (hidden.get(key) or [None])[0]
        rows.append((key, score, hid))
    print(f"{'候选':44s} {'区分部分':>9s} {'隐藏':>7s}")
    for key, score, hid in rows:
        s = "n/a" if score is None else f"{score:.3f}"
        h = "n/a" if hid is None else f"{hid:.3f}"
        print(f"{key:44s} {s:>9s} {h:>7s}")

    pairs = [(s, h) for _, s, h in rows if s is not None and h is not None]
    if len(pairs) >= 3 and len({s for s, _ in pairs}) > 1:
        r = st.correlation([s for s, _ in pairs], [h for _, h in pairs])
        print(f"\n仅用区分性测试的皮尔逊相关 r = {r:.3f}")

    (PROBE_DIR / "pertest.json").write_text(
        json.dumps(
            {
                "always_fail": always_fail,
                "always_pass": always_pass,
                "varying": varying,
                "per_candidate": per_candidate,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {PROBE_DIR / 'pertest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
