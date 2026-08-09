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
import re
import resource
import shutil
import subprocess
import tempfile
from pathlib import Path

from orchestra.codeprojecteval.ceiling import analyze_ceiling, collected_counts
from orchestra.codeprojecteval.dataset import (
    DEFAULT_DATASET_ROOT,
    DEFAULT_ENV_ROOT,
    load_task,
)

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
    """

    def apply() -> None:
        resource.setrlimit(
            resource.RLIMIT_AS, (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024)
        )
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 30))

    return apply


def resolve_canonical_repo(batch_dir: Path, task_id: str) -> Path:
    """The committed repository, not the pristine workspace the run started from.

    Milestones execute in per-subtask forks and merge into the task's canonical
    repository; the batch-level workspace stays at the dataset inputs.
    """
    task_dir = batch_dir / task_id / "tasks"
    if task_dir.is_dir():
        for plan_dir in sorted(task_dir.iterdir()):
            repo = plan_dir / "canonical" / "repo"
            if repo.is_dir():
                return repo
    return batch_dir / "workspaces" / task_id


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


def score_task(
    task_id: str,
    *,
    workspace: Path,
    dataset_root: Path,
    env_root: Path,
    timeout: int,
    per_test_timeout: int = 60,
    memory_mb: int = 2048,
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
                    "PYTHONPATH": str(repo),
                    "PATH": f"{python.parent}:/usr/bin:/bin:/usr/local/bin",
                    "HOME": str(repo),
                },
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                preexec_fn=_resource_caps(memory_mb, timeout),  # noqa: PLW1509
            )
        except subprocess.TimeoutExpired:
            ceiling = analyze_ceiling(
                task,
                collected=collected_counts(
                    task,
                    python=python,
                    cache_path=Path("outputs/cpe_collect_cache.json"),
                ),
            )
            return {
                "task_id": task_id,
                "status": "timeout",
                "shipped_top_level": shipped,
                "passed": 0,
                "failed": 0,
                "error": 0,
                "pass_rate": 0.0,
                "pass_rate_reachable": 0.0,
                "reachable_ceiling": ceiling.reachable_ceiling,
                "tests_total": ceiling.tests_total,
                "tests_reachable": ceiling.tests_reachable,
            }

    tail = (proc.stdout or "")[-6000:]
    counts = _parse_counts(tail)
    observed = counts["passed"] + counts["failed"] + counts["error"]
    ceiling = analyze_ceiling(
        task,
        collected=collected_counts(
            task, python=python, cache_path=Path("outputs/cpe_collect_cache.json")
        ),
    )
    return {
        "task_id": task_id,
        "status": "ok" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "source_dir_present": source_present,
        "shipped_top_level": shipped,
        **counts,
        # Raw uses the dataset's own suite size, so a module that never imported
        # still costs its tests rather than shrinking the denominator.
        "pass_rate": round(counts["passed"] / ceiling.tests_total, 4)
        if ceiling.tests_total
        else 0.0,
        "pass_rate_observed": round(counts["passed"] / observed, 4) if observed else 0.0,
        # Normalised by what the design documents can specify at all.
        "pass_rate_reachable": round(counts["passed"] / ceiling.tests_reachable, 4)
        if ceiling.tests_reachable
        else 0.0,
        "reachable_ceiling": ceiling.reachable_ceiling,
        "tests_total": ceiling.tests_total,
        "tests_reachable": ceiling.tests_reachable,
        "blocked_modules": ceiling.blocked_modules,
        "tail": tail[-1200:],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("batch_dir", type=Path)
    ap.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    ap.add_argument("--env-root", type=Path, default=DEFAULT_ENV_ROOT)
    ap.add_argument("--timeout", type=int, default=1200)
    # 15s x a few hundred hanging tests overruns any sane wall clock; these are
    # unit tests, so a test still running after 5s is a hang, not slow work.
    ap.add_argument("--per-test-timeout", type=int, default=5)
    ap.add_argument("--memory-mb", type=int, default=2048)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    workspaces = args.batch_dir / "workspaces"
    if not workspaces.is_dir():
        raise SystemExit(f"no workspaces under {args.batch_dir}")

    results = []
    for ws in sorted(p for p in workspaces.iterdir() if p.is_dir()):
        result = score_task(
            ws.name,
            workspace=resolve_canonical_repo(args.batch_dir, ws.name),
            dataset_root=args.dataset_root,
            env_root=args.env_root,
            timeout=args.timeout,
            per_test_timeout=args.per_test_timeout,
            memory_mb=args.memory_mb,
        )
        results.append(result)
        print(
            f"{result['task_id']:24s} {result['status']:12s} "
            f"pass={result.get('passed', 0):4d} fail={result.get('failed', 0):3d} "
            f"err={result.get('error', 0):3d} "
            f"raw={result.get('pass_rate', 0):.3f} "
            f"reachable={result.get('pass_rate_reachable', 0):.3f} "
            f"(ceiling {result.get('reachable_ceiling', 0):.2f})",
            flush=True,
        )

    scored = [r for r in results if "pass_rate" in r]
    summary = {
        "batch_dir": str(args.batch_dir),
        "n_tasks": len(results),
        "mean_pass_rate": round(sum(r["pass_rate"] for r in scored) / len(scored), 4)
        if scored
        else 0.0,
        "mean_pass_rate_reachable": round(
            sum(r["pass_rate_reachable"] for r in scored) / len(scored), 4
        )
        if scored
        else 0.0,
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
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
