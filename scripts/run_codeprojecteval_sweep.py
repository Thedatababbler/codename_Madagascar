#!/usr/bin/env python3
"""Run a CodeProjectEval trial matrix concurrently and record one row per trial.

Trials are independent processes over disjoint output directories, so the only
reason the shell driver ran them one at a time is that it was a shell loop. At
roughly sixteen minutes each, a three-repository three-arm sweep took the best
part of a day serially, which is long enough that nobody repeats it and every
comparison ends up resting on n=3.

Two things are deliberately *not* parallel:

* Repetitions of the same trial still run concurrently with each other -- there
  is no shared state between them -- but the concurrency limit is a single
  number rather than per-task, because the binding constraint is the model
  endpoint, not the machine.
* Scoring runs inside the same slot as the run that produced it. It is cheap
  next to the agent, and keeping them together means a finished slot has a
  complete row rather than a repository waiting to be scored later.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestra.control.backend_usage import derive_cost_usd  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Trial:
    task_id: str
    arm: str
    seed: int
    run_id: str
    plan_file: Path

    @property
    def label(self) -> str:
        return f"{self.task_id}/{self.arm}/r{self.seed}"


@dataclass
class TrialResult:
    """One row of the multi-objective record a tuning loop reads."""

    task_id: str
    arm: str
    seed: int
    run_id: str
    status: str
    pass_rate: float | None = None
    pass_rate_reachable: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    estimated_cost_usd: float | None = None
    cost_quality: str = "unavailable"
    wall_clock_seconds: float = 0.0
    agent_turns_planned: int = 0
    agent_turns_run: int = 0
    milestones: int = 0
    committed: int = 0
    gates_passed: int = 0
    gates_failed: int = 0
    errors: list[str] = field(default_factory=list)


def isolated_codex_home(run_id: str, root: Path) -> Path | None:
    """A private Codex state directory for one trial.

    The Codex CLI keeps its state, logs, memories and goals in four SQLite
    databases under ``CODEX_HOME``, and concurrent processes sharing that
    directory lose the race: the loser dies at startup with ``failed to
    initialize state runtime ... database is locked``. Credentials and config
    are copied in; the databases are created fresh and thrown away with the run.
    """
    source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if not source.is_dir():
        return None
    home = root / ".codex_homes" / run_id
    home.mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "config.toml"):
        origin = source / name
        if origin.is_file():
            shutil.copy2(origin, home / name)
    return home


def trial_env(codex_home: Path | None = None) -> dict[str, str]:
    """The environment a trial runs under.

    Handed to the subprocess rather than set on this process: mutating the
    parent's environment leaks these flags into anything else sharing the
    interpreter, and the untrusted-harness flag in particular disables a
    safety check.
    """
    env = dict(os.environ)
    env.setdefault("ADAMAS_CODEX_SANDBOX_OVERRIDE", "full_access")
    env.setdefault("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", "1")
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env


def _run(
    cmd: list[str], *, log: Path, timeout: float, codex_home: Path | None = None
) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as stream:
        stream.write(f"\n$ {' '.join(cmd)}\n".encode())
        stream.flush()
        try:
            proc = subprocess.run(
                cmd, cwd=REPO_ROOT, stdout=stream, stderr=subprocess.STDOUT,
                timeout=timeout, check=False, env=trial_env(codex_home),
            )
        except subprocess.TimeoutExpired:
            stream.write(b"\n[sweep] timed out\n")
            return 124
    return proc.returncode


def _events(batch_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(batch_dir.glob("*/logs/*/events.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def collect_result(trial: Trial, batch_dir: Path, *, status: str) -> TrialResult:
    """Join the run, its scoring and its usage into a single row.

    These live in three files today, and every consumer -- the A/B summary, the
    ceiling report, anything a tuning loop would want -- has had to rediscover
    how to join them. A search loop cannot afford that.
    """
    result = TrialResult(
        task_id=trial.task_id, arm=trial.arm, seed=trial.seed,
        run_id=trial.run_id, status=status,
    )
    summary_path = batch_dir / "batch_summary.json"
    if summary_path.is_file():
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        for row in payload.get("results") or []:
            if row.get("task_id") != trial.task_id:
                continue
            result.milestones = int(row.get("milestone_count") or 0)
            result.agent_turns_planned = int(row.get("agent_turns") or 0)
            result.committed = len(row.get("committed") or [])
            result.wall_clock_seconds = float(row.get("latency_ms") or 0.0) / 1000.0
            if row.get("error"):
                result.errors.append(str(row["error"]))

    hidden_path = batch_dir / "hidden_eval.json"
    if hidden_path.is_file():
        payload = json.loads(hidden_path.read_text(encoding="utf-8"))
        for row in payload.get("results") or []:
            if row.get("task_id") != trial.task_id:
                continue
            result.pass_rate = row.get("pass_rate")
            result.pass_rate_reachable = row.get("pass_rate_reachable")
            if row.get("status") in {"timeout", "no_environment", "no_workspace"}:
                # Scored zero is not the same as never measured, and a tuning
                # loop that cannot tell them apart will chase the difference.
                result.status = str(row["status"])

    model_name: str | None = None
    priced_nodes = 0
    for event in _events(batch_dir):
        kind = event.get("event_type")
        node = str(event.get("node_id") or "")
        if kind == "NODE_COMPLETED" and node.startswith("agent_"):
            result.agent_turns_run += 1
            result.prompt_tokens += int(event.get("prompt_tokens") or 0)
            result.completion_tokens += int(event.get("completion_tokens") or 0)
            result.cached_tokens += int(event.get("cached_tokens") or 0)
            model_name = model_name or event.get("model_name")
            cost = event.get("estimated_cost_usd")
            if cost is not None:
                result.estimated_cost_usd = (result.estimated_cost_usd or 0.0) + float(cost)
                priced_nodes += 1
        elif kind == "CONDITIONAL_EDGE_ACTIVATED":
            edge = str((event.get("metadata") or {}).get("edge_id") or "")
            if edge.endswith("_pass_to_freeze"):
                result.gates_passed += 1
            elif edge.startswith("gate_fail_to"):
                result.gates_failed += 1

    if priced_nodes and priced_nodes == result.agent_turns_run:
        result.cost_quality = "derived" if result.cached_tokens else "upper_bound"
    elif result.prompt_tokens or result.completion_tokens:
        # Runs recorded before the backend stamped a model name still have their
        # tokens, and a priced upper bound beats no cost axis at all -- as long
        # as it is labelled, because a cache-heavy Codex session pays well under
        # list price on input.
        derived, _ = derive_cost_usd(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached_tokens=result.cached_tokens or None,
            model_name=model_name or os.environ.get("CODEX_MODEL") or "gpt-5.4",
            provider_cost_usd=None,
        )
        result.estimated_cost_usd = derived
        result.cost_quality = (
            "unavailable" if derived is None
            else ("derived" if result.cached_tokens else "upper_bound")
        )
    return result


def execute(trial: Trial, args: argparse.Namespace) -> TrialResult:
    batch_dir = args.output_root / trial.run_id
    log = batch_dir / "sweep.log"
    codex_home = isolated_codex_home(trial.run_id, args.output_root)
    started = time.monotonic()
    code = _run(
        [
            "uv", "run", "python", "-m", "orchestra.cli.run_codeprojecteval_decomp",
            "--task-id", trial.task_id,
            "--plan-file", str(trial.plan_file),
            "--arm", trial.arm,
            "--output-root", str(args.output_root),
            "--run-id", trial.run_id,
        ],
        log=log,
        timeout=args.run_timeout,
        codex_home=codex_home,
    )
    status = "ok" if code == 0 else ("timeout" if code == 124 else "run_failed")
    if code == 0:
        score = _run(
            [
                "uv", "run", "python", "scripts/eval_codeprojecteval.py", str(batch_dir),
                # int, not float: the scorer's argparse rejects "5.0".
                "--per-test-timeout", str(int(args.per_test_timeout)),
                "--timeout", str(int(args.eval_timeout)),
            ],
            log=log,
            timeout=args.eval_timeout + 300,
        )
        if score != 0:
            status = "eval_failed"

    result = collect_result(trial, batch_dir, status=status)
    if not result.wall_clock_seconds:
        result.wall_clock_seconds = time.monotonic() - started
    return result


def build_trials(args: argparse.Namespace, stamp: str) -> list[Trial]:
    trials: list[Trial] = []
    for task_id in args.tasks:
        for arm in args.arms:
            plan = args.plans / f"{task_id}.{arm}.json"
            if not plan.is_file():
                raise SystemExit(f"missing frozen plan: {plan}")
            for seed in range(1, args.repeats + 1):
                trials.append(
                    Trial(
                        task_id=task_id, arm=arm, seed=seed,
                        run_id=f"ab-{task_id}-{arm}-r{seed}-{stamp}",
                        plan_file=plan,
                    )
                )
    return trials


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", required=True, type=lambda s: [x for x in s.split(",") if x])
    ap.add_argument(
        "--arms",
        default="solo,single,multi",
        type=lambda s: [x for x in s.split(",") if x],
    )
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Trials in flight at once. The model endpoint is the limit, not the host.",
    )
    ap.add_argument("--plans", type=Path, default=Path("outputs/cpe_ab/plans"))
    ap.add_argument("--output-root", type=Path, default=Path("outputs/cpe_ab"))
    ap.add_argument("--run-timeout", type=float, default=5400.0)
    ap.add_argument("--eval-timeout", type=float, default=1200.0)
    ap.add_argument("--per-test-timeout", type=float, default=5.0)
    ap.add_argument("--out", type=Path, default=None, help="Trial record (JSONL).")
    args = ap.parse_args()

    if args.repeats > 5:
        raise SystemExit("repeats above 5 is more compute than this experiment buys")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    trials = build_trials(args, stamp)
    out = args.out or args.output_root / f"trials-{stamp}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"{len(trials)} trials, {args.concurrency} at a time "
        f"({args.tasks} x {args.arms} x {args.repeats})"
    )
    started = time.monotonic()
    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(execute, trial, args): trial for trial in trials}
        for future in as_completed(futures):
            trial = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad trial must not end the sweep
                result = TrialResult(
                    task_id=trial.task_id, arm=trial.arm, seed=trial.seed,
                    run_id=trial.run_id, status="crashed", errors=[repr(exc)],
                )
            done += 1
            with open(out, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
            print(
                f"[{done}/{len(trials)}] {trial.label:28s} {result.status:11s} "
                f"pass={result.pass_rate if result.pass_rate is not None else '--':>6} "
                f"tok={result.prompt_tokens + result.completion_tokens:>9,} "
                f"{result.wall_clock_seconds / 60:5.1f}min",
                flush=True,
            )

    print(f"\n{len(trials)} trials in {(time.monotonic() - started) / 60:.1f} min")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
