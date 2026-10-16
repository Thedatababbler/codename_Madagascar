#!/usr/bin/env python3
"""Single-session arm of the NL2Repo hard-level comparison (user decisions 2026-10-16).

One Codex session gets the design documents and builds the whole repository in an empty workspace: no split, no
author, no gate, no repair. It runs through AdaMAS's own CodexSDKBackend -- the same sandboxed session config,
self-check, sandbox manifest, prompt capture, turn audit and breach log as every node of the full arm -- but the
prompt is only the template below (the full arm's node contracts carry AdaMAS guidance, which this arm must not
see, spec §5.6). The prompt asks for no self-testing and forbids none: Codex may write and run tests on its own.

The wall clock is the only per-session limit besides the shared token cap: ``--timeout`` (default 6 h) is far above
the 2400 s per-node ceiling of the full arm, which is sized for one milestone. The token cap is enforced from outside
by the driver (scripts/nl2_hard_pilot.py) on the quota window. The workspace is committed as the run's canonical
repository whatever state it is in, so the scorer grades what the session left.

    uv run python scripts/nl2_single_session.py --task nl2_dbutils --run-id <id> [--output-root outputs/nl2repo_hard]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PROMPT = """You are implementing a complete Python project from its design documents.
The documents are in docs/: PRD.md, architecture_design.md and directory_tree.txt.
Implement the whole project in this repository so that everything the documents
describe works, at the module paths and with the public interfaces they name.
When you finish, the repository should contain the complete implementation."""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


async def run(task_id: str, run_id: str, output_root: Path, timeout: float, model: str) -> dict:
    from orchestra.backends.base import AgentRequest, BackendExecutionContext, ModelSpec, OutputContract
    from orchestra.backends.codex_sdk import CodexSDKBackend
    from orchestra.cli import run_codeprojecteval_decomp as R
    from orchestra.codeprojecteval.dataset import build_agent_workspace, load_task
    from orchestra.codeprojecteval.harness import materialize_check_harness
    from orchestra.sandbox.breach import scan_run
    from orchestra.sandbox.guard import require_sandbox

    require_sandbox(f"single session {task_id}")
    dataset_root = Path(os.environ["CPE_DATASET_ROOT"])
    task = load_task(task_id, dataset_root=dataset_root)
    batch = output_root / run_id
    run_dir = batch / task_id
    harness_dir = run_dir / "harness"
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest = materialize_check_harness(task, harness_dir=harness_dir)
    purged = R.purge_task_packages(Path(manifest.env_python), list(manifest.top_level_packages))
    workspace = build_agent_workspace(task, batch / "workspaces" / task_id)
    if not (workspace / ".git").exists():
        _git(workspace, "init", "-q")
        _git(workspace, "add", "-A")
        _git(workspace, "-c", "user.email=s@adamas", "-c", "user.name=single", "commit", "-qm", "dataset inputs only")
    # the same process environment the run entry sets for every Codex node
    os.environ.setdefault("ADAMAS_CODEX_SANDBOX_OVERRIDE", "full_access")
    os.environ.setdefault("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", "1")
    R._start_sandbox({}, run_dir=run_dir, harness_dir=harness_dir, task_id=task_id, workspace=workspace)
    rb = f"rb_{task_id}"
    trace = run_dir / "tasks" / rb / "backend_traces" / "single_session"
    trace.mkdir(parents=True, exist_ok=True)
    request = AgentRequest(
        request_id=str(uuid.uuid4()), task_id=rb, subtask_id="single_session", node_id="single_session",
        role="implementer", instruction=PROMPT, rendered_context="",
        model=ModelSpec(provider="openai_compatible", name=model, temperature=0.0, max_tokens=8192),
        timeout_seconds=timeout,
        output_contract=OutputContract(parser_id="repository_change", output_schema="RepositoryChangeArtifact"),
        backend_config={"type": "codex_sdk", "thread_policy": "fresh", "sandbox": "workspace_write",
                        "approval_policy": "never", "require_git_diff": True, "max_steps": 1},
        messages=[{"role": "user", "content": PROMPT}],
    )
    context = BackendExecutionContext(run_id=run_id, task_id=rb, subtask_id="single_session", node_id="single_session",
                                      workspace_ref=str(workspace), trace_dir=str(trace))
    t0 = time.time()
    started = datetime.now(UTC).isoformat()
    result = await CodexSDKBackend().run(request, context)
    wall = time.time() - t0
    usage = result.usage.model_dump() if hasattr(result.usage, "model_dump") else dict(result.usage)
    post = run_dir / "logs" / "03_post_run"
    post.mkdir(parents=True, exist_ok=True)
    (post / "backend_usage_records.json").write_text(json.dumps([{"node_id": "single_session", "usage": usage}], indent=1))
    # the session's final repository state is the run's canonical repository, whatever the outcome
    canon = run_dir / "tasks" / rb / "canonical" / "repo"
    canon.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(workspace, canon, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    breach = scan_run(run_dir, run_id=f"{run_id}/{task_id}")
    # as the run entry does after every task: an install the session made of its own package leaves the env
    purged_after = R.purge_task_packages(Path(manifest.env_python), list(manifest.top_level_packages))
    summary = {
        "arm": "single_session", "task_id": task_id, "run_id": run_id, "model": model, "started": started,
        "wall_seconds": round(wall, 1), "timeout_seconds": timeout, "status": str(getattr(result.status, "value", result.status)),
        "error": (result.error.model_dump() if getattr(result, "error", None) is not None and hasattr(result.error, "model_dump") else None),
        "usage": usage, "backend_calls": 1, "purged_packages": purged, "purged_after": purged_after, "prompt": PROMPT,
        "sandbox_breach": {k: v for k, v in breach.items() if k != "critical_events"},
        "sandbox_void": bool(breach.get("void")),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "nl2repo_hard")
    ap.add_argument("--timeout", type=float, default=21600.0)
    ap.add_argument("--model", default=os.environ.get("CODEX_MODEL") or "gpt-5.6-luna")
    a = ap.parse_args()
    s = asyncio.run(run(a.task, a.run_id, a.output_root, a.timeout, a.model))
    print(json.dumps({k: s[k] for k in ("task_id", "status", "wall_seconds", "usage", "sandbox_breach")}, default=str))
    return 0 if s["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
