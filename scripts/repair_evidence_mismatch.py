#!/usr/bin/env python3
"""Why the persistent failures handed to R0 pass in the repair-evidence run (2026-10-15, tests only, no agent).

For the five R0 runs of the sandbox baseline whose evidence run passed every persistent failure:
  gate     -- the predecessor suites re-run on the incumbent exactly as the diagnosis did
              (prior_suites.rewrite_command on the milestone's gate command, run_authoritative_harness_command), x2;
  evidence -- build_repair_evidence on the same incumbent with the same persistent list.
The incumbent is copied under outputs/repair_evidence_mismatch/work/ first so the stored run is not touched.

    uv run python scripts/repair_evidence_mismatch.py gate|evidence|all [--only task:milestone]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestra.control.fast_loop.prior_suites import PriorSuite, gate_command_of, rewrite_command  # noqa: E402
from orchestra.control.fast_loop.committed_cases import read_committed_cases  # noqa: E402
from orchestra.harness.command_runner import run_authoritative_harness_command  # noqa: E402
from orchestra.harness.progress import behaviour_failures, behaviour_passed, parse_progress  # noqa: E402
from orchestra.control.fast_loop.persistence import failure_key  # noqa: E402
from orchestra.ir.graph import load_graph  # noqa: E402

BASE = ROOT / "outputs" / "cpe_sandbox_baseline"
OUT = ROOT / "outputs" / "repair_evidence_mismatch"
CASES = [("cpe-20261012T081623Z-djangorestframework-simplejwt", "djangorestframework-simplejwt", "request_authentication"),
         ("cpe-20261012T101048Z-voluptuous", "voluptuous", "utilities_and_humanize"),
         ("cpe-20261012T101048Z-voluptuous", "voluptuous", "public_api_integration"),
         ("cpe-20261012T140933Z-zxcvbn", "zxcvbn", "feedback_and_public_estimator"),
         ("cpe-20261012T140933Z-zxcvbn", "zxcvbn", "cli_packaging_and_full_integration")]


def run_dir(batch: str, task: str) -> Path:
    return BASE / batch / task


def state_of(batch: str, task: str) -> dict:
    return json.loads((run_dir(batch, task) / "tasks" / f"rb_{task}" / "task_execution.json").read_text())


def priors_of(batch: str, task: str, mid: str) -> list[PriorSuite]:
    te = state_of(batch, task)
    out = []
    for dep in (te["fast_loop_states"][mid]["routing"].get("incumbent_prior_suites") or {}):
        cmd, _ = gate_command_of(load_graph(te["subtasks"][dep]["spec"]["local_graph_template"]))
        spec = cmd[cmd.index("--spec-tests") + 1]
        rec = read_committed_cases(spec) or {"passed": [], "failed": []}
        arg = lambda f: cmd[cmd.index(f) + 1] if f in cmd else ""  # noqa: E731
        out.append(PriorSuite(dep, spec, arg("--contracts"), arg("--level") or "integration",
                              frozenset(rec["passed"]), frozenset(rec["failed"])))
    return out


def initial_copy(batch: str, task: str) -> Path:
    """The task-level workspace (dataset inputs only) the diagnosis actually ran the predecessor suites in."""
    src = BASE / batch / "workspaces" / task
    dst = OUT / "work" / task / "_initial" / "repo"
    if not dst.exists():
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dst


def incumbent_copy(batch: str, task: str, mid: str) -> Path:
    src = run_dir(batch, task) / "tasks" / f"rb_{task}" / "workspaces" / mid / "repo"
    dst = OUT / "work" / task / mid / "repo"
    if not dst.exists():
        shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dst


def persistent_of(batch: str, task: str, mid: str) -> list[str]:
    te = state_of(batch, task)
    r0 = next(c for c in te["fast_loop_states"][mid]["candidates"] if c["candidate_id"] == "cand_R0")
    return list(r0["metadata"]["persistent_failures"])


async def gate(batch: str, task: str, mid: str, times: int = 2, where: str = "incumbent") -> dict:
    te = state_of(batch, task)
    own, timeout = gate_command_of(load_graph(te["subtasks"][mid]["spec"]["local_graph_template"]))
    repo = incumbent_copy(batch, task, mid) if where == "incumbent" else initial_copy(batch, task)
    res = {"cwd": str(repo)}
    for prior in priors_of(batch, task, mid):
        cmd = rewrite_command(own, prior)
        runs = []
        for i in range(times):
            ok, code, stdout, stderr = await run_authoritative_harness_command(cwd=str(repo), command=cmd, timeout_seconds=timeout)
            score, stages, _ = parse_progress(stdout)
            failed = sorted(behaviour_failures(stages)) if stages else []
            passed = sorted(behaviour_passed(stages)) if stages else []
            (OUT / "logs").mkdir(parents=True, exist_ok=True)
            (OUT / "logs" / f"gate_{where}_{task}_{mid}_{prior.milestone_id}_{i}.txt").write_text(
                f"$ cwd={repo}\n$ {' '.join(cmd)}\nexit={code}\n--- stdout (last 4000, as the diagnosis saw it)\n{stdout}\n--- stderr\n{stderr}\n")
            regr = sorted(k for k in prior.committed_passed if k in {failure_key(n) for n in failed})
            runs.append({"regressions": len(regr), "committed_passed": len(prior.committed_passed), "exit": code, "score": score, "stages": [f"{getattr(s, 'stage', '?')} {getattr(s, 'passed_units', '?')}/{getattr(s, 'total_units', '?')}" for s in (stages or [])],
                         "failed": len(failed), "passed": len(passed)})
        res[prior.milestone_id] = {"command": cmd, "runs": runs}
    return res


def evidence(batch: str, task: str, mid: str) -> dict:
    from orchestra.control.fast_loop.repair_evidence import build_repair_evidence

    repo = incumbent_copy(batch, task, mid)
    ev = build_repair_evidence(repo, persistent_of(batch, task, mid))
    if ev is None:
        return {"evidence": None}
    text = (Path(ev) / "failures.md").read_text()
    tail = [ln for ln in text.splitlines() if " passed" in ln or " failed" in ln or "error" in ln.lower()][-3:]
    return {"evidence": str(ev), "summary": tail}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["gate", "evidence", "all"])
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    from orchestra.sandbox.guard import require_sandbox

    require_sandbox("repair evidence mismatch replay")
    out = {}
    for batch, task, mid in CASES:
        if a.only and a.only != f"{task}:{mid}":
            continue
        r = {}
        if a.what in ("gate", "all"):
            r["gate_initial"] = asyncio.run(gate(batch, task, mid, where="initial"))
            r["gate"] = asyncio.run(gate(batch, task, mid))
        if a.what in ("evidence", "all"):
            r["evidence"] = evidence(batch, task, mid)
        out[f"{task}:{mid}"] = r
        print(task, mid, "done", flush=True)
    p = OUT / f"result_{a.what}.json"
    p.write_text(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
