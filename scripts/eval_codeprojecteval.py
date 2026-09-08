#!/usr/bin/env python3
"""Score a CodeProjectEval batch on the held-out unit_tests suite.

Runs offline and outside the agent's reach: the generated repository is copied
to a scratch directory, the dataset's ``unit_tests`` are overlaid there, and the
suite runs under the repository's own virtualenv. Nothing the agent produced can
influence which tests run — the visible ``check_tests`` are replaced by the
dataset's copies too, so a rewritten suite cannot survive into scoring.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import shutil
import subprocess
import tempfile
from pathlib import Path

from orchestra.codeprojecteval.ceiling import (
    analyze_ceiling,
    collected_counts,
    denominator_faults,
    reachable_is_unsound,
)
from orchestra.codeprojecteval.dataset import (
    DEFAULT_DATASET_ROOT,
    DEFAULT_ENV_ROOT,
    load_task,
)
from orchestra.codeprojecteval.suite_sizes import load_pinned_suite_sizes

# Files AdaMAS or the dataset owns; never counted as agent output.
SKIP_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "check_tests",
    "unit_tests",
    "docs",
}


def _resource_caps(memory_mb: int, cpu_seconds: int):  # noqa: ANN202
    """Hard limits for the scoring subprocess.

    A per-test timeout uses SIGALRM, which a tight allocating loop never gets to
    handle: one generated implementation held the harness for 20 minutes while
    growing to 6.7 GB. Address-space and CPU limits kill such a run immediately
    instead of letting it decide how long scoring takes.

    CPU seconds are not the session wall clock. A large suite can wait on I/O
    for hours and still be a real exam; a busy loop must not inherit that budget.

    The address-space ceiling has to clear what an honest suite reserves, not
    what it uses: importing pandas alone maps well past 2 GB, and at 2048 the
    interpreter died before pytest printed a line, which the caller then read as
    an empty repository (EXP-20260815 official csvs-to-sqlite, really 17/25).
    """

    def apply() -> None:
        resource.setrlimit(
            resource.RLIMIT_AS, (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024)
        )
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 30))

    return apply


def _source_py_count(repo: Path) -> int:
    return sum(
        1
        for p in repo.rglob("*.py")
        if p.is_file()
        and not any(part in SKIP_NAMES or part == "__pycache__" for part in p.parts)
    )


def resolve_canonical_repo(
    batch_dir: Path, task_id: str, *, prefer_agent_workspace: bool = False
) -> Path:
    """The committed repository, not the pristine workspace the run started from.

    Milestones execute in per-subtask forks and merge into the task's canonical
    repository; the batch-level workspace stays at the dataset inputs.

    ``prefer_agent_workspace`` is for a baseline that must be scored even when
    the gate refused to commit: pick the fork with the most agent-written
    Python rather than an empty canonical tree.
    """
    task_dir = batch_dir / task_id / "tasks"
    committed: Path | None = None
    if task_dir.is_dir():
        for plan_dir in sorted(task_dir.iterdir()):
            repo = plan_dir / "canonical" / "repo"
            if repo.is_dir():
                committed = repo
                break
    fallback = batch_dir / "workspaces" / task_id
    if not prefer_agent_workspace:
        return committed or fallback
    candidates = [p for p in [committed, fallback] if p is not None and p.is_dir()]
    if task_dir.is_dir():
        candidates.extend(
            p
            for p in task_dir.glob("**/workspaces/*/repo")
            if p.is_dir()
        )
    if not candidates:
        return fallback
    return max(candidates, key=_source_py_count)


def _copy_generated(source: Path, dest: Path) -> list[str]:
    """Copy the agent's repository, dropping harness- and dataset-owned trees."""
    dest.mkdir(parents=True, exist_ok=True)
    shipped: list[str] = []
    for item in sorted(source.iterdir()):
        if item.name in SKIP_NAMES:
            continue
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(
                item,
                target,
                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
                dirs_exist_ok=True,
            )
        else:
            shutil.copy2(item, target)
        shipped.append(item.name)
    return shipped


def _parse_counts(text: str) -> dict[str, int]:
    counts = {}
    for token in ("passed", "failed", "error", "skipped"):
        match = re.search(rf"(\d+) {token}", text)
        counts[token] = int(match.group(1)) if match else 0
    return counts


def _suite_sizes(task_id: str, task, python: Path) -> dict[str, int] | None:
    """Collected case counts, preferring the pinned file over live collection.

    Live collection is a fallback for an unpinned task only. It is not equivalent:
    it varies with the environment's state, which is how the same suite came to be
    divided by two different numbers across runs.
    """
    pinned = load_pinned_suite_sizes().get(task_id)
    if pinned:
        return pinned
    return collected_counts(
        task, python=python, cache_path=Path("outputs/cpe_collect_cache.json")
    )


def score_task(
    task_id: str,
    *,
    workspace: Path,
    dataset_root: Path,
    env_root: Path,
    timeout: int,
    per_test_timeout: int = 30,
    memory_mb: int = 8192,
    cpu_seconds: int = 3600,
) -> dict:
    task = load_task(task_id, dataset_root=dataset_root)
    python = env_root / task_id / "bin" / "python"
    if not python.is_file():
        return {"task_id": task_id, "status": "no_environment", "python": str(python)}
    if not workspace.is_dir():
        return {"task_id": task_id, "status": "no_workspace"}

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / task_id
        shipped = _copy_generated(workspace, repo)
        # Dataset-owned suites always come from the dataset, never the workspace.
        for name in (task.unit_tests, task.check_tests):
            src = task.repo_root / name
            if src.is_dir():
                shutil.copytree(src, repo / name, dirs_exist_ok=True)

        source_present = bool(task.source_dir) and (repo / task.source_dir).exists()
        try:
            proc = subprocess.run(
                [
                    str(python), "-m", "pytest", task.unit_tests, "-q", "--no-header",
                    "-p", "no:cacheprovider", "-o", "addopts=",
                    # One module importing an unstated internal name would
                    # otherwise abort collection and zero the whole suite.
                    "--continue-on-collection-errors",
                    # Generated code can loop forever; charge that to the test
                    # that hangs instead of to the whole repository's score.
                    f"--timeout={per_test_timeout}",
                    # `thread` aborts the whole session on the first hang;
                    # `signal` raises inside the test so the rest still runs.
                    "--timeout-method=signal",
                ],
                cwd=repo,
                env={
                    "PYTHONPATH": os.pathsep.join(
                        p for p in [str(repo), str(repo / "src") if (repo / "src").is_dir() else ""] if p
                    ),
                    "PATH": f"{python.parent}:/usr/bin:/bin:/usr/local/bin",
                    "HOME": str(repo),
                },
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                preexec_fn=_resource_caps(memory_mb, cpu_seconds),  # noqa: PLW1509
            )
        except subprocess.TimeoutExpired:
            ceiling = analyze_ceiling(
                task, collected=_suite_sizes(task_id, task, python)
            )
            return {
                "task_id": task_id,
                "status": "timeout",
                "shipped_top_level": shipped,
                "passed": 0,
                "failed": 0,
                "error": 0,
                # Never a scored zero: the suite did not finish, so there is
                # no rate. Publishing 0.0 here made a hung eval look like an
                # empty repository (EXP-20260814 official bplustree solo).
                "pass_rate": None,
                "pass_rate_reachable": None,
                "reachable_ceiling": ceiling.reachable_ceiling,
                "tests_total": ceiling.tests_total,
                "tests_reachable": ceiling.tests_reachable,
            }

    tail = (proc.stdout or "")[-6000:]
    stderr_tail = (proc.stderr or "")[-6000:]
    counts = _parse_counts(tail)
    observed = counts["passed"] + counts["failed"] + counts["error"]
    ceiling = analyze_ceiling(task, collected=_suite_sizes(task_id, task, python))
    # SIGXCPU / SIGKILL with no summary is the same as a wall timeout: unmeasured.
    # So is a nonzero exit that said nothing at all on either stream: pytest
    # always reports its own errors, so silence means the interpreter died
    # before running the suite (a resource cap), which is not a verdict on the
    # code under test.
    died_silently = (
        proc.returncode != 0 and not tail.strip() and not stderr_tail.strip()
    )
    if observed == 0 and (proc.returncode not in {0, 1, 2, 3, 4, 5} or died_silently):
        return {
            "task_id": task_id,
            "status": "timeout",
            "returncode": proc.returncode,
            "shipped_top_level": shipped,
            "passed": 0,
            "failed": 0,
            "error": 0,
            "pass_rate": None,
            "pass_rate_reachable": None,
            "reachable_ceiling": ceiling.reachable_ceiling,
            "tests_total": ceiling.tests_total,
            "tests_reachable": ceiling.tests_reachable,
        }
    faults = denominator_faults(ceiling, passed=counts["passed"])
    reachable_unsound = reachable_is_unsound(ceiling, passed=counts["passed"])
    return {
        "task_id": task_id,
        "status": "ok" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "source_dir_present": source_present,
        "shipped_top_level": shipped,
        **counts,
        # Raw uses the dataset's own suite size, so a module that never imported
        # still costs its tests rather than shrinking the denominator.
        "pass_rate": None
        if faults
        else round(counts["passed"] / ceiling.tests_total, 4),
        "denominator_faults": faults,
        "pass_rate_observed": round(counts["passed"] / observed, 4) if observed else 0.0,
        # Normalised by what the design documents can specify at all.
        "pass_rate_reachable": None
        if (faults or reachable_unsound or not ceiling.tests_reachable)
        else round(counts["passed"] / ceiling.tests_reachable, 4),
        "reachable_unsound": reachable_unsound,
        "reachable_ceiling": ceiling.reachable_ceiling,
        "tests_total": ceiling.tests_total,
        "tests_reachable": ceiling.tests_reachable,
        "estimated_modules": ceiling.estimated_modules,
        "blocked_modules": ceiling.blocked_modules,
        "tail": tail[-1200:],
        # A conftest that fails to import writes only to stderr, so a record
        # carrying stdout alone showed an empty reason for a zeroed suite.
        "stderr_tail": stderr_tail[-1200:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("batch_dir", type=Path)
    ap.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    ap.add_argument("--env-root", type=Path, default=DEFAULT_ENV_ROOT)
    ap.add_argument("--timeout", type=int, default=21600)
    # Per-test hang protection stays on so one infinite loop cannot eat the
    # wall clock. 30s is slow work, not a hang. The session budget must cover
    # a large suite if every case uses that full allotment (bplustree ~356
    # cases × 30s > the old 20-minute wall, which then published 0.0).
    ap.add_argument("--per-test-timeout", type=int, default=30)
    ap.add_argument("--memory-mb", type=int, default=8192)
    ap.add_argument("--cpu-seconds", type=int, default=3600)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument(
        "--prefer-agent-workspace",
        action="store_true",
        help="Score the agent's working tree even if the gate did not commit.",
    )
    args = ap.parse_args()

    workspaces = args.batch_dir / "workspaces"
    if not workspaces.is_dir():
        raise SystemExit(f"no workspaces under {args.batch_dir}")

    results = []
    for ws in sorted(p for p in workspaces.iterdir() if p.is_dir()):
        result = score_task(
            ws.name,
            workspace=resolve_canonical_repo(
                args.batch_dir,
                ws.name,
                prefer_agent_workspace=args.prefer_agent_workspace,
            ),
            dataset_root=args.dataset_root,
            env_root=args.env_root,
            timeout=args.timeout,
            per_test_timeout=args.per_test_timeout,
            memory_mb=args.memory_mb,
            cpu_seconds=args.cpu_seconds,
        )
        results.append(result)

        def _rate(value: float | None) -> str:
            return "  n/a" if value is None else f"{value:.3f}"

        print(
            f"{result['task_id']:24s} {result['status']:12s} "
            f"pass={result.get('passed', 0):4d} fail={result.get('failed', 0):3d} "
            f"err={result.get('error', 0):3d} "
            f"raw={_rate(result.get('pass_rate'))} "
            f"reachable={_rate(result.get('pass_rate_reachable'))} "
            f"(ceiling {result.get('reachable_ceiling', 0):.2f})"
            + (
                "  UNSCORED: " + "; ".join(result["denominator_faults"])
                if result.get("denominator_faults")
                else ""
            ),
            flush=True,
        )

    scored = [r for r in results if r.get("pass_rate") is not None]
    reachable_scored = [r for r in results if r.get("pass_rate_reachable") is not None]
    unscored = [r["task_id"] for r in results if r.get("pass_rate") is None]
    summary = {
        "batch_dir": str(args.batch_dir),
        "n_tasks": len(results),
        "n_scored": len(scored),
        # Named so nobody averages a mean that silently dropped tasks.
        "unscored_tasks": unscored,
        "mean_pass_rate": round(sum(r["pass_rate"] for r in scored) / len(scored), 4)
        if scored
        else None,
        "mean_pass_rate_reachable": round(
            sum(r["pass_rate_reachable"] for r in reachable_scored)
            / len(reachable_scored),
            4,
        )
        if reachable_scored
        else None,
        "fully_passing": [r["task_id"] for r in scored if r["status"] == "ok"],
        "results": results,
    }
    out = args.out or args.batch_dir / "hidden_eval.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"\nmean_pass_rate={summary['mean_pass_rate']} "
        f"mean_pass_rate_reachable={summary['mean_pass_rate_reachable']} "
        f"fully_passing={len(summary['fully_passing'])}/{len(scored)}"
    )
    if unscored:
        print(f"UNSCORED ({len(unscored)}): {', '.join(unscored)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
