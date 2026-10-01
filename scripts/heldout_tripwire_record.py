#!/usr/bin/env python3
"""Write the sealed held-out tripwire rows (self-evolution spec §5.4). Training tasks only.

For every candidate record of a run that kept a workspace, run the held-out
cases attributed to that record's milestone (``scripts/heldout_attribution.py``
output) on the candidate's repository, and append
``{record_id, heldout_attributed_subset_pass_rate}`` to the sealed file. The
held-out suite is copied from the dataset into a scratch directory exactly
as the scorer does; nothing is written into any workspace, and nothing but
the pass rate leaves this process.

    uv run python scripts/heldout_tripwire_record.py --run outputs/cpe_evolution/<batch>/<task> \\
        --attribution outputs/evolution/sealed/attribution/<task>.json [--ledger outputs/evolution/ledger]
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestra.codeprojecteval.dataset import load_task  # noqa: E402
from orchestra.control.evolution.ledger import read_jsonl, split_of  # noqa: E402
from orchestra.control.evolution.tripwire import record_tripwire  # noqa: E402

DATASET_ROOT = Path(os.environ.get("CPE_DATASET_ROOT") or "/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")
ENV_ROOT = Path(os.environ.get("CPE_ENV_ROOT") or "/root/codex-benchmarks/cpe_envs")


def subset_pass_rate(task_id: str, workspace: Path, cases: list[str], *, timeout: int = 1800) -> float | None:
    task = load_task(task_id, dataset_root=DATASET_ROOT)
    python = ENV_ROOT / task_id / "bin" / "python"
    if not python.is_file() or not workspace.is_dir() or not cases:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task_id
        shutil.copytree(workspace, repo, ignore=shutil.ignore_patterns("__pycache__", ".git", "spec_tests", "repair_evidence"))
        src = task.repo_root / task.unit_tests
        if src.is_dir():
            shutil.copytree(src, repo / task.unit_tests, dirs_exist_ok=True)
        node_ids = [f"{task.unit_tests}/{c}" for c in cases]
        proc = subprocess.run(
            [str(python), "-m", "pytest", *node_ids, "-q", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
             "--continue-on-collection-errors", "--timeout=30", "--timeout-method=signal"],
            cwd=repo, capture_output=True, text=True, timeout=timeout, check=False,
            env={"PYTHONPATH": os.pathsep.join(p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p),
                 "PATH": f"{python.parent}:/usr/bin:/bin", "HOME": str(repo)},
        )
    passed = sum(int(m.group(1)) for m in re.finditer(r"(\d+) passed", proc.stdout))
    return passed / len(cases)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--attribution", required=True, type=Path)
    ap.add_argument("--ledger", type=Path, default=None)
    args = ap.parse_args()
    task_id = args.run.name
    if split_of(task_id) != "train":
        raise SystemExit(f"{task_id} is not a training task; the tripwire is recorded on training tasks only")
    attribution = json.loads(args.attribution.read_text(encoding="utf-8")).get("attribution") or {}
    rows = []
    from orchestra.control.evolution.ledger import ledger_root_default

    for cand_file in (args.ledger or ledger_root_default()).glob("*/candidates.jsonl"):
        for rec in read_jsonl(cand_file):
            if str(rec.get("run_dir") or "") != str(args.run) or not rec.get("workspace_ref"):
                continue
            cases = attribution.get(rec["milestone_id"]) or []
            rate = subset_pass_rate(task_id, Path(rec["workspace_ref"]), cases)
            if rate is None:
                continue
            rows.append({"record_id": rec["record_id"], "heldout_attributed_subset_pass_rate": rate})
    path = record_tripwire(rows)
    print(f"sealed {len(rows)} row(s) into {path}")


if __name__ == "__main__":
    main()
