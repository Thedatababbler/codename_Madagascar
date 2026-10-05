#!/usr/bin/env python3
"""Per-case held-out comparison of two batches of the same task, attributed to milestones.

Re-runs the held-out suite the way ``scripts/eval_codeprojecteval.py`` does
(shipped workspace copied to a scratch directory, the dataset's unit_tests
copied in, the task's own interpreter), but with ``-rA`` so every case's
outcome is kept, then diffs the two arms and groups the moved cases by the
milestone ``scripts/sealed/heldout_attribution.py`` assigned them to. Analysis only:
nothing here feeds a search, a selection or a commit, and no workspace is
touched.

    uv run python scripts/sealed/heldout_case_diff.py --task tinydb \\
        --a outputs/cpe_evolution_baseline/<batch> --b outputs/cpe_evolution/<batch> \\
        --attribution outputs/evolution/sealed/attribution/tinydb.json --out <report.md>
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
from collections import defaultdict
from pathlib import Path

from orchestra.codeprojecteval.dataset import load_task

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import DATASET_ROOT, ENV_ROOT, heldout_dir, reference_root  # noqa: E402
_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")


def per_case(task_id: str, batch: Path, *, per_test_timeout: int = 30, timeout: int = 7200) -> dict[str, str]:
    task = load_task(task_id, dataset_root=DATASET_ROOT)
    # The committed canonical repository, as the evaluator ships it; the agent
    # workspace under ``workspaces/`` holds only the dataset inputs.
    committed = sorted((batch / task_id / "tasks").glob("*/canonical/repo")) if (batch / task_id / "tasks").is_dir() else []
    workspace = committed[0] if committed else batch / "workspaces" / task_id
    python = ENV_ROOT / task_id / "bin" / "python"
    if not workspace.is_dir() or not python.is_file():
        raise SystemExit(f"missing workspace {workspace} or interpreter {python}")
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task_id
        shutil.copytree(workspace, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git", "spec_tests", "repair_evidence", "unit_tests", "check_tests"))
        src = heldout_dir(task_id)
        if (repo / task.unit_tests).exists():
            shutil.rmtree(repo / task.unit_tests)
        shutil.copytree(src, repo / task.unit_tests)
        proc = subprocess.run(
            [str(python), "-m", "pytest", task.unit_tests, "-q", "-rA", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
             "--continue-on-collection-errors", f"--timeout={per_test_timeout}", "--timeout-method=signal"],
            cwd=repo, capture_output=True, text=True, timeout=timeout, check=False,
            env={"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
                 "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"},
        )
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        m = _LINE.match(line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--a", required=True, type=Path, help="batch dir of arm A (e.g. the baseline)")
    ap.add_argument("--b", required=True, type=Path, help="batch dir of arm B")
    ap.add_argument("--attribution", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--per-test-timeout", type=int, default=30)
    args = ap.parse_args()
    a = per_case(args.task, args.a, per_test_timeout=args.per_test_timeout)
    b = per_case(args.task, args.b, per_test_timeout=args.per_test_timeout)
    attribution: dict[str, str] = {}
    if args.attribution and args.attribution.is_file():
        data = json.loads(args.attribution.read_text(encoding="utf-8"))
        for mid, cases in (data.get("attribution") or data).items():
            for c in cases:
                attribution[c] = mid

    def passed(d, k):
        return d.get(k) in ("PASSED", "XPASS")

    keys = sorted(set(a) | set(b))
    lost = [k for k in keys if passed(a, k) and not passed(b, k)]
    gained = [k for k in keys if passed(b, k) and not passed(a, k)]
    by_ms: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"lost": [], "gained": []})
    for k in lost:
        by_ms[attribution.get(k.split("[", 1)[0], attribution.get(k, "?"))]["lost"].append(k)
    for k in gained:
        by_ms[attribution.get(k.split("[", 1)[0], attribution.get(k, "?"))]["gained"].append(k)
    na, nb = sum(passed(a, k) for k in keys), sum(passed(b, k) for k in keys)
    lines = [f"# {args.task}: held-out per case, A = {args.a.name}, B = {args.b.name}", "",
             f"cases {len(keys)}; passed A {na}, B {nb}; lost (A pass, B fail) {len(lost)}; gained {len(gained)}", ""]
    for mid, d in sorted(by_ms.items(), key=lambda kv: -(len(kv[1]['lost']) + len(kv[1]['gained']))):
        lines.append(f"## {mid}: lost {len(d['lost'])}, gained {len(d['gained'])}")
        lines += [f"- lost: {k}" for k in d["lost"]] + [f"- gained: {k}" for k in d["gained"]] + [""]
    text = "\n".join(lines)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        json.dump({"a": a, "b": b, "lost": lost, "gained": gained, "by_milestone": by_ms}, open(args.out.with_suffix(".json"), "w"), indent=1)
    print(text)


if __name__ == "__main__":
    main()
