#!/usr/bin/env python3
"""Run the frozen soft suites of a run and record ``soft_results`` (author-evolution spec §2.6).

Soft cases never reach the gate, the unified acceptance or the default repair; they are
measured here, after the fact, at least on the committed final repository and, with
``--candidates``, on every retained candidate workspace of the milestone, so the evolver can
see when soft cases fail. Each soft suite is mounted as ``spec_tests`` (its files import
each other under that name), run with the task's interpreter, and the per-case outcome is
written to ``<run>/author_inventory/soft_results.json`` and into the ledger's milestone
record for that run (field ``soft_results``).

    uv run python scripts/run_soft_suites.py outputs/cpe_evolution/<batch>/<task> [--candidates]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ENV_ROOT = Path(os.environ.get("CPE_ENV_ROOT") or "/root/codex-benchmarks/cpe_envs")
_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+)")


def run_suite(repo: Path, suite: Path, python: Path, *, timeout: int = 900) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        holder = Path(tmp)
        (holder / "spec_tests").symlink_to(suite.resolve(), target_is_directory=True)
        proc = subprocess.run(
            [str(python), "-m", "pytest", str(holder / "spec_tests"), "-q", "-rA", "--no-header", "-p", "no:cacheprovider",
             "-o", "addopts=", "--continue-on-collection-errors", "--timeout=60", "--timeout-method=signal"],
            cwd=repo, capture_output=True, text=True, timeout=timeout, check=False,
            env={"PYTHONPATH": os.pathsep.join(p for p in [str(holder), str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
                 "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(repo), "PYTHONDONTWRITEBYTECODE": "1"},
        )
    out = {}
    for line in proc.stdout.splitlines():
        m = _LINE.match(line)
        if m:
            node = m.group(2)
            out["spec_tests/" + node.split("spec_tests/", 1)[-1] if "spec_tests/" in node else node] = \
                "pass" if m.group(1) in ("PASSED", "XPASS") else "fail"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--candidates", action="store_true", help="also every retained candidate workspace of the milestone")
    args = ap.parse_args()
    run = args.run.resolve()
    task = run.name.removeprefix("rb_")
    python = ENV_ROOT / task / "bin" / "python"
    inv_dir = run / "author_inventory"
    finals = sorted(run.glob("tasks/*/canonical/repo"))
    results: dict[str, dict] = {}
    for soft in sorted(inv_dir.glob("*/*.spec_tests_soft")):
        mid = soft.parent.name
        targets = {"final": finals[0]} if finals else {}
        if args.candidates:
            for ws in sorted((run / "fast_loop").rglob(f"{mid}/candidates/*/repo")) if (run / "fast_loop").is_dir() else []:
                targets[ws.parent.name] = ws
        per = {name: run_suite(repo, soft, python) for name, repo in targets.items()}
        results.setdefault(mid, {})[soft.name] = per
        f = per.get("final", {})
        print(f"{mid:42s} {soft.name:28s} final: {sum(v == 'fail' for v in f.values())}/{len(f)} soft cases fail"
              + (f"; {len(per) - 1} candidate(s)" if len(per) > 1 else ""))
    inv_dir.mkdir(parents=True, exist_ok=True)
    (inv_dir / "soft_results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    # ledger: attach to this run's milestone records
    from orchestra.control.evolution.ledger import ledger_root_default, read_jsonl

    for mf in ledger_root_default().glob("*/milestones.jsonl"):
        recs = list(read_jsonl(mf))
        changed = False
        for r in recs:
            if str(r.get("milestone_id")) in results and str(r.get("run_dir") or "").rstrip("/") == str(run):
                r["soft_results"] = results[str(r["milestone_id"])]
                changed = True
        if changed:
            mf.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


if __name__ == "__main__":
    main()
