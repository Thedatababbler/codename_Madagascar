#!/usr/bin/env python3
"""Does the authored-suite score rank designs the way the held-out suite does?

The acceptance gate cannot: EXP-20260810-03 measured nine imapclient candidates at
exactly 1.0 while their held-out rates ranged 0.307-0.375. `test_first` adds an
authored suite as a quality axis, but an axis that does not track held-out quality
would just be a more expensive way to be uninformative, and finding that out from a
tuning batch would cost several hundred dollars.

This buys the answer for the price of one agent call. `test_author` runs once to
produce the suite; the nine candidate patches from that experiment are already on
disk with their held-out rates, so scoring them against the frozen suite is free.

    uv run python scripts/probe_spec_axis_signal.py --author     # one API call
    uv run python scripts/probe_spec_axis_signal.py              # offline, reuses it
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics as st
import subprocess
import tempfile
from pathlib import Path

import yaml

from orchestra.backends.base import (
    AgentRequest,
    AgentRunStatus,
    AgentSessionPolicy,
    BackendExecutionContext,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.codeprojecteval import cpe_brief, load_task
from orchestra.codeprojecteval.dataset import DEFAULT_ENV_ROOT, build_agent_workspace

TASK_ID = "imapclient"
CANDIDATE_ROOT = Path("outputs/cpe_tuning/design")
HIDDEN_PER_CANDIDATE = Path("outputs/cpe_tuning/hidden_per_candidate.json")
PLAN = Path("outputs/cpe_tuning/plans/imapclient.multi.json")
PROBE_DIR = Path("outputs/spec_axis_probe")


def _milestone_prompt(task, milestone: dict) -> str:
    """What test_author sees in-graph: the brief plus this milestone's scope."""
    criteria = "\n".join(f"- {c}" for c in (milestone.get("acceptance") or {}).get("criteria", []))
    focus = "\n".join(f"- {p}" for p in milestone.get("focus_paths") or [])
    return (
        f"{cpe_brief(task)}\n\n"
        f"## This milestone: {milestone.get('milestone_id')}\n\n"
        f"Why it is a milestone: {milestone.get('risk_rationale')}\n\n"
        f"### Acceptance criteria\n{criteria}\n\n"
        f"### Modules in scope\n{focus}\n"
    )


async def _author_suite(out_dir: Path) -> None:
    task = load_task(TASK_ID)
    milestone = json.loads(PLAN.read_text(encoding="utf-8"))["milestones"][0]
    role = yaml.safe_load(Path("configs/roles/test_author.yaml").read_text(encoding="utf-8"))

    workspace = Path(tempfile.mkdtemp(prefix="spec-axis-author-"))
    repo = build_agent_workspace(task, workspace / "repo")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=probe", "commit", "-qm", "base"],
        cwd=repo,
        check=False,
    )

    budget = role.get("budget") or {}
    request = AgentRequest(
        request_id="spec-axis-probe",
        task_id=TASK_ID,
        subtask_id=milestone["milestone_id"],
        node_id="agent_1_test_author",
        role="test_author",
        instruction="Write the test suite that decides whether this milestone succeeded.",
        messages=[
            {"role": "system", "content": role["prompt"]},
            {"role": "user", "content": _milestone_prompt(task, milestone)},
        ],
        rendered_context=_milestone_prompt(task, milestone),
        model=ModelSpec(name=os.getenv("ADAMAS_PROBE_MODEL", "gpt-5.4")),
        tools=[],
        max_steps=int(budget.get("max_steps", 12)),
        timeout_seconds=float(budget.get("timeout_seconds", 1200)),
        output_contract=OutputContract(
            parser_id="repository_change", output_schema="RepositoryChangeArtifact"
        ),
        backend_config={
            "type": "codex_sdk",
            "thread_policy": "fresh",
            "sandbox": "workspace_write",
            "approval_policy": "never",
            "require_git_diff": True,
        },
        session_policy=AgentSessionPolicy.FRESH,
    )
    backend = CodexSDKBackend()
    result = await backend.run(
        request,
        BackendExecutionContext(
            run_id="spec-axis-probe",
            task_id=TASK_ID,
            subtask_id=milestone["milestone_id"],
            node_id="agent_1_test_author",
            workspace_ref=str(repo),
        ),
    )
    if result.status is not AgentRunStatus.SUCCESS:
        raise SystemExit(f"test_author failed: {result.error}")

    authored = repo / "spec_tests"
    if not authored.is_dir():
        raise SystemExit("test_author wrote no spec_tests/ directory")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(authored, out_dir)
    usage = result.backend_metadata or {}
    (out_dir.parent / "author_usage.json").write_text(
        json.dumps(
            {
                "files": sorted(p.name for p in out_dir.rglob("test_*.py")),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "estimated_cost_usd": usage.get("estimated_cost_usd"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"authored {len(list(out_dir.rglob('test_*.py')))} file(s) -> {out_dir}")


def _run_suite(python: Path, repo: Path) -> tuple[int, int]:
    """(passed, collected) for the authored suite inside `repo`."""
    proc = subprocess.run(
        [
            str(python), "-m", "pytest", "spec_tests", "-q", "--no-header",
            "-p", "no:cacheprovider", "-o", "addopts=",
            "--continue-on-collection-errors", "--timeout=30", "--timeout-method=signal",
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
    tail = (proc.stdout or "")[-4000:]
    passed = failed = errors = 0
    import re

    for token, name in (("passed", "p"), ("failed", "f"), ("error", "e")):
        m = re.search(rf"(\d+) {token}", tail)
        value = int(m.group(1)) if m else 0
        if name == "p":
            passed = value
        elif name == "f":
            failed = value
        else:
            errors = value
    return passed, passed + failed + errors


def _candidate_repos() -> dict[str, Path]:
    """Rebuild each candidate's tree from its recorded freeze patch."""
    task = load_task(TASK_ID)
    repos: dict[str, Path] = {}
    for run_dir in sorted(CANDIDATE_ROOT.glob("ab-imapclient-multi-r*")):
        rep = run_dir.name.split("-multi-")[1].split("-")[0]
        tasks_dir = run_dir / TASK_ID / "tasks"
        for cand_dir in sorted(tasks_dir.glob("*__candidate__*")):
            cand = cand_dir.name.split("__candidate__")[1]
            freeze = next(cand_dir.glob("artifacts/*freeze_change*.json"), None)
            if freeze is None:
                continue
            patch = (json.loads(freeze.read_text(encoding="utf-8"))["payload"] or {}).get("patch")
            if not patch:
                continue
            target = Path(tempfile.mkdtemp(prefix=f"spec-axis-{rep}-{cand}-"))
            repo = build_agent_workspace(task, target / "repo")
            proc = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"],
                cwd=repo,
                input=patch,
                text=True,
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                print(f"  {rep}|{cand}: patch did not apply ({proc.stderr.strip()[:120]})")
                continue
            repos[f"{rep}|{cand}"] = repo
    return repos


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--author", action="store_true", help="spend one agent call")
    args = ap.parse_args()

    suite = PROBE_DIR / "spec_tests"
    if args.author:
        PROBE_DIR.mkdir(parents=True, exist_ok=True)
        asyncio.run(_author_suite(suite))
    if not suite.is_dir():
        raise SystemExit(f"no authored suite at {suite}; run once with --author")

    python = DEFAULT_ENV_ROOT / TASK_ID / "bin" / "python"
    task = load_task(TASK_ID)

    # Vacuity baseline: tests that pass with the implementation withheld measure
    # nothing, so they are excluded before any candidate is compared.
    pristine_root = Path(tempfile.mkdtemp(prefix="spec-axis-pristine-"))
    pristine = build_agent_workspace(task, pristine_root / "repo")
    shutil.copytree(suite, pristine / "spec_tests")
    vacuous, baseline_total = _run_suite(python, pristine)
    print(f"vacuity baseline: {vacuous}/{baseline_total} pass with no implementation\n")

    hidden = json.loads(HIDDEN_PER_CANDIDATE.read_text(encoding="utf-8"))
    rows = []
    for key, repo in _candidate_repos().items():
        shutil.copytree(suite, repo / "spec_tests", dirs_exist_ok=True)
        passed, total = _run_suite(python, repo)
        useful_total = max(total - vacuous, 0)
        useful_passed = max(passed - vacuous, 0)
        authored_score = useful_passed / useful_total if useful_total else None
        rows.append((key, authored_score, (hidden.get(key) or [None])[0], passed, total))

    print(f"{'候选':44s} {'authored':>9s} {'隐藏':>7s}")
    for key, authored, hid, passed, total in sorted(rows):
        a = "n/a" if authored is None else f"{authored:.3f}"
        h = "n/a" if hid is None else f"{hid:.3f}"
        print(f"{key:44s} {a:>9s} {h:>7s}   ({passed}/{total} authored raw)")

    pairs = [(a, h) for _, a, h, _, _ in rows if a is not None and h is not None]
    if len(pairs) >= 3:
        xs = [a for a, _ in pairs]
        ys = [h for _, h in pairs]
        spread = max(xs) - min(xs)
        print(f"\nauthored 分布: {min(xs):.3f}..{max(xs):.3f} (跨度 {spread:.3f})")
        print(f"隐藏分布:     {min(ys):.3f}..{max(ys):.3f} (跨度 {max(ys)-min(ys):.3f})")
        if spread < 1e-9:
            print("裁定: 退化 —— authored 轴对这些候选给出同一个分,和门一样没有信号")
        else:
            r = st.correlation(xs, ys) if hasattr(st, "correlation") else None
            print(f"皮尔逊相关 r = {r:.3f}" if r is not None else "")
            aligned = (r or 0) > 0.3
            print(
                "裁定: 轴有区分度"
                + ("且方向正确" if aligned else ",但方向与隐藏质量不一致,不能直接当质量轴用")
            )
    payload = [
        {"candidate": k, "authored": a, "hidden": h, "raw": f"{p}/{t}"}
        for k, a, h, p, t in rows
    ]
    (PROBE_DIR / "signal.json").write_text(
        json.dumps(
            {"vacuous": vacuous, "baseline_total": baseline_total, "rows": payload},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {PROBE_DIR / 'signal.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
