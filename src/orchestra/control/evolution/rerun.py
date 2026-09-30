"""Re-running bank checkpoints to pair a new row or F entry against its control (spec §7).

A re-run is a real execution of one milestone from a bank entry's
predecessor snapshot, through the standard procedure, with one thing forced:
a playbook row (``ADAMAS_FORCE_ROW=<row_id>``, §7.2) or a first-pass entry
(``ADAMAS_FORCE_F=<entry_id>``, §7.3). Both versions are run on the same
checkpoint ``reps`` times and compared case by case; the numbers that come
out are stable fixes and stable regressions, never a comparison with a
historical score.

This module plans re-runs, launches them through the CLI (one subprocess per
run, ``concurrency`` at a time, ``max_runs_per_cycle`` in total), reads the
ledger records they wrote, and computes the paired statistics and the F-entry
verdict (§7.4). The launcher is the only part that spends a model call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestra.control.evolution.bank import BankEntry
from orchestra.control.evolution.ledger import read_jsonl

FORCE_ROW_ENV = "ADAMAS_FORCE_ROW"
FORCE_F_ENV = "ADAMAS_FORCE_F"
RERUN_ROOT = Path("outputs") / "evolution" / "reruns"


@dataclass(frozen=True)
class RerunJob:
    entry_id: str
    task_id: str
    milestone_id: str
    kind: str            # "row" | "f_entry"
    variant: str         # row id or F entry id
    control: str         # "R0" or "F0"
    rep: int
    run_id: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class RerunPlan:
    jobs: list[RerunJob]
    skipped: dict[str, str]

    @property
    def runs(self) -> int:
        return len(self.jobs)


def plan_row_reruns(
    entries: list[BankEntry], *, row_id: str, error_class: str, reps: int, needed: int, max_runs: int,
) -> RerunPlan:
    """§7.2: pair R0 against ``row_id`` on ``needed`` entries of ``error_class`` (each run pairs both)."""
    jobs: list[RerunJob] = []
    skipped: dict[str, str] = {}
    picked = [e for e in entries if error_class in set(e.persistent_failures.values()) and not e.suite_stale]
    for e in picked[:needed]:
        for rep in range(reps):
            if len(jobs) >= max_runs:
                skipped[e.entry_id] = "max_runs_per_cycle"
                break
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            jobs.append(RerunJob(
                entry_id=e.entry_id, task_id=e.task_id, milestone_id=e.milestone_id, kind="row", variant=row_id,
                control="R0", rep=rep, run_id=f"rerun-{row_id.replace('~', '_')}-{stamp}-{e.task_id}-{e.milestone_id}-r{rep}"[:120],
                env={FORCE_ROW_ENV: f"{e.milestone_id}={row_id}"},
            ))
    for e in [x for x in entries if x.suite_stale]:
        skipped[e.entry_id] = "suite_stale"
    return RerunPlan(jobs=jobs, skipped=skipped)


def plan_f_reruns(
    entries: list[BankEntry], *, entry_id: str, matches: Any, reps: int, needed: int, max_runs: int, success_share: float = 0.3,
) -> RerunPlan:
    """§7.3: F0 vs F_k on entries whose features match, mixing in successes at ``success_share``."""
    jobs: list[RerunJob] = []
    skipped: dict[str, str] = {}
    matched = [e for e in entries if not e.suite_stale and matches(e.features)]
    failed = [e for e in matched if e.outcome != "success"]
    ok = [e for e in matched if e.outcome == "success"]
    n_ok = int(round(needed * success_share))
    chosen = failed[: max(0, needed - n_ok)] + ok[:n_ok]
    for e in chosen:
        for version in ("F0", entry_id):
            for rep in range(reps):
                if len(jobs) >= max_runs:
                    skipped[e.entry_id] = "max_runs_per_cycle"
                    break
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                jobs.append(RerunJob(
                    entry_id=e.entry_id, task_id=e.task_id, milestone_id=e.milestone_id, kind="f_entry", variant=version,
                    control="F0", rep=rep, run_id=f"rerun-{version}-{stamp}-{e.task_id}-{e.milestone_id}-r{rep}"[:120],
                    env={FORCE_F_ENV: f"{e.milestone_id}={version}"},
                ))
    for e in [x for x in entries if x.suite_stale]:
        skipped[e.entry_id] = "suite_stale"
    return RerunPlan(jobs=jobs, skipped=skipped)


def cli_command(job: RerunJob, entry: BankEntry, *, config: str, output_root: Path, dataset_root: str | None) -> list[str]:
    """The CLI invocation for one job: the milestone alone, from the entry's snapshot, with the original harness."""
    cmd = [
        sys.executable, "-m", "orchestra.cli.run_codeprojecteval_decomp", "--config", config,
        "--task-id", entry.task_id, "--arm", "rerun", "--run-id", job.run_id, "--output-root", str(output_root),
        "--only-milestone", entry.milestone_id,
        "--base-snapshot", f"{entry.predecessor_snapshot.get('repo', '')}@{entry.predecessor_snapshot.get('revision', '')}",
        "--inherit-harness", str(Path(entry.run_dir) / "harness"),
        "--plan-file", str(Path(entry.run_dir) / "milestone_plan_draft.json"),
    ]
    if dataset_root:
        cmd += ["--dataset-root", dataset_root]
    return cmd


def launch(jobs: list[RerunJob], entries: dict[str, BankEntry], *, config: str, output_root: Path | None = None,
           dataset_root: str | None = None, concurrency: int = 2, dry_run: bool = False) -> list[dict[str, Any]]:
    """Run the jobs ``concurrency`` at a time; returns one result dict per job."""
    root = Path(output_root) if output_root else RERUN_ROOT
    root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    pending = list(jobs)
    running: list[tuple[RerunJob, subprocess.Popen | None]] = []
    while pending or running:
        while pending and len(running) < max(1, concurrency):
            job = pending.pop(0)
            cmd = cli_command(job, entries[job.entry_id], config=config, output_root=root, dataset_root=dataset_root)
            if dry_run:
                results.append({"run_id": job.run_id, "command": cmd, "dry_run": True})
                continue
            env = {**os.environ, **job.env}
            log = (root / f"{job.run_id}.log").open("w", encoding="utf-8")
            proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, text=True)
            running.append((job, proc))
        still: list[tuple[RerunJob, subprocess.Popen | None]] = []
        for job, proc in running:
            if proc is None or proc.poll() is not None:
                results.append({"run_id": job.run_id, "returncode": proc.returncode if proc else None, "run_dir": str(root / job.run_id / job.task_id)})
            else:
                still.append((job, proc))
        running = still
        if running:
            import time

            time.sleep(5)
    return results


# --------------------------------------------------------------------------- paired statistics


def ledger_records_of(run_dir: Path, *, ledger_root: Path | None = None) -> list[dict[str, Any]]:
    """Candidate records a re-run wrote (any version directory), tagged ``source=rerun``."""
    root = Path(ledger_root) if ledger_root else Path("outputs") / "evolution" / "ledger"
    out = []
    for p in root.glob("*/candidates.jsonl"):
        for rec in read_jsonl(p):
            if str(rec.get("run_dir") or "") == str(run_dir):
                rec = dict(rec)
                rec["source"] = "rerun"
                out.append(rec)
    return out


def paired_case_stats(control_runs: list[dict[str, str]], variant_runs: list[dict[str, str]]) -> dict[str, Any]:
    """§7.1 item 2: stable fixes (control fails every rep, variant passes every rep) and stable regressions."""
    cases = set()
    for r in control_runs + variant_runs:
        cases.update(r.keys())
    fixes, regressions = [], []
    for k in sorted(cases):
        c = [r.get(k) for r in control_runs]
        v = [r.get(k) for r in variant_runs]
        if c and v and all(x == "fail" for x in c) and all(x == "pass" for x in v):
            fixes.append(k)
        if c and v and all(x == "pass" for x in c) and all(x == "fail" for x in v):
            regressions.append(k)
    return {"stable_fixes": fixes, "stable_regressions": regressions, "net": len(fixes) - len(regressions),
            "reps": (len(control_runs), len(variant_runs))}


def pair_row_records(records: list[dict[str, Any]], row_id: str) -> dict[str, Any]:
    """Within one re-run: R0's per-case results vs the forced row's."""
    r0 = [r for r in records if r.get("candidate_kind") == "R0"]
    row = [r for r in records if r.get("row_id", "").split("~", 1)[0] == row_id]
    return paired_case_stats([r["per_case_results"] for r in r0], [r["per_case_results"] for r in row])


@dataclass
class FVerdict:
    promote: bool
    reasons: list[str]
    net: int
    cost_ratio: float
    target_class_drop: bool
    checkpoints: int


def f_entry_verdict(
    *, net_effect: int, cost_variant: float, cost_control: float, target_before: int, target_after: int,
    checkpoints: int, m_f: int = 5, cost_tolerance: float = 0.15, tripwire_fired: bool = False,
) -> FVerdict:
    """§7.4: every condition must hold for trial -> active."""
    reasons = []
    ratio = (cost_variant / cost_control) if cost_control > 0 else (1.0 if cost_variant == 0 else float("inf"))
    if net_effect < 0:
        reasons.append(f"net effect {net_effect} < 0")
    if ratio > 1.0 + cost_tolerance:
        reasons.append(f"cost ratio {ratio:.2f} > {1 + cost_tolerance:.2f}")
    drop = target_after < target_before
    if not drop:
        reasons.append("target-class persistent failures did not fall")
    if checkpoints < m_f:
        reasons.append(f"{checkpoints} checkpoints < m_f={m_f}")
    if tripwire_fired:
        reasons.append("held-out tripwire fired")
    return FVerdict(promote=not reasons, reasons=reasons, net=net_effect, cost_ratio=ratio, target_class_drop=drop, checkpoints=checkpoints)


def merge_rerun_records(online: list[dict[str, Any]], rerun: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Online and re-run records side by side; re-run rows keep ``source=rerun``."""
    out = [dict(r, source=r.get("source", "online")) for r in online]
    out += [dict(r, source="rerun") for r in rerun]
    return out


def forced_row_for(milestone_id: str) -> str | None:
    """``ADAMAS_FORCE_ROW=<milestone>=<row>`` (or just ``<row>``) for this milestone, else None."""
    raw = os.environ.get(FORCE_ROW_ENV, "").strip()
    if not raw:
        return None
    if "=" in raw:
        mid, _, row = raw.partition("=")
        return row if mid == milestone_id else None
    return raw


def forced_f_for(milestone_id: str) -> str | None:
    raw = os.environ.get(FORCE_F_ENV, "").strip()
    if not raw:
        return None
    if "=" in raw:
        mid, _, entry = raw.partition("=")
        return entry if mid == milestone_id else None
    return raw


__all__ = [
    "FORCE_F_ENV", "FORCE_ROW_ENV", "FVerdict", "RERUN_ROOT", "RerunJob", "RerunPlan", "cli_command",
    "f_entry_verdict", "forced_f_for", "forced_row_for", "launch", "ledger_records_of", "merge_rerun_records",
    "pair_row_records", "paired_case_stats", "plan_f_reruns", "plan_row_reruns",
]
