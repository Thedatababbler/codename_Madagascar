#!/usr/bin/env python3
"""Hidden-test evaluation for RealBench AdaMAS Codex decomp baseline outputs.

Reuses the same overlay+pytest protocol as realbench_codex_vanilla
(`evaluate_hidden`), evaluating frozen canonical repos against private
`proj_with_test` fixtures. Does not modify agent workspaces.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import venv
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ADAMAS_ROOT = Path(__file__).resolve().parents[1]
VANILLA_ROOT = Path(
    os.environ.get(
        "REALBENCH_CODEX_VANILLA_ROOT",
        "/root/codex-benchmarks/realbench_codex_vanilla",
    )
).resolve()


def _import_vanilla() -> Any:
    scripts = VANILLA_ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import common  # type: ignore

    return common


# Agent-facing AdaMAS markers must not pollute private evaluation fixtures.
SKIP_NAMES = {
    "TASK.md",
    "REQUIREMENTS.md",
    "README.md",
    "NOTES.md",
    ".pristine_snapshot.json",
    ".adamas_trusted_harness",
    "test_adamas_workspace_ok.py",
    # Runs no longer place these in the workspace; kept so an accidental
    # regression can never reach the hidden fixture.
    "MILESTONE.md",
    "ADAMAS_CHANGELOG.md",
    "ADAMAS_DECISIONS.md",
    "adamas_public_harness.json",
    "adamas_milestone_contracts.json",
    "adamas_public_check.py",
}
SKIP_PARTS = {".git", "public_design", "__pycache__", ".pytest_cache"}


def _is_agent_test_path(rel: Path) -> bool:
    """Skip agent-authored tests so private fixture tests remain authoritative."""
    parts = {p.lower() for p in rel.parts}
    name = rel.name.lower()
    if "tests" in parts or "test" in parts:
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return False


def parse_junit_counts(junit_path: Path) -> dict[str, Any] | None:
    if not junit_path.is_file():
        return None
    try:
        root = ET.parse(junit_path).getroot()
    except ET.ParseError:
        return None
    suites = list(root) if root.tag == "testsuites" else [root]
    tests = errors = failures = skipped = 0
    for s in suites:
        tests += int(s.attrib.get("tests") or 0)
        errors += int(s.attrib.get("errors") or 0)
        failures += int(s.attrib.get("failures") or 0)
        skipped += int(s.attrib.get("skipped") or 0)
    # Prefer suite attrs; fall back to counting nodes if attrs missing.
    if tests == 0:
        cases = root.findall(".//testcase")
        tests = len(cases)
        failures = len(root.findall(".//failure"))
        errors = len(root.findall(".//error"))
        skipped = len(root.findall(".//skipped"))
    passed = max(0, tests - errors - failures - skipped)
    return {
        "passed": passed,
        "failed": failures,
        "error": errors,
        "skipped": skipped,
        "collected": tests,
        "collection_failure": tests == 0,
        "parsed_ok": True,
        "source": "junit.xml",
    }


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def resolve_canonical_repo(batch_dir: Path, task_id: str) -> Path:
    task_dir = batch_dir / task_id
    plan_id = f"rb_{task_id}"
    repo = task_dir / "tasks" / plan_id / "canonical" / "repo"
    if not repo.is_dir():
        raise FileNotFoundError(f"canonical repo missing for {task_id}: {repo}")
    return repo


def evaluate_hidden(
    *,
    common: Any,
    task: dict[str, Any],
    workspace: Path,
    out_dir: Path,
    eval_tmp_root: Path,
) -> dict[str, Any]:
    """Mirror vanilla evaluate_hidden with AdaMAS-specific skip filters."""
    import subprocess

    t0 = time.monotonic()
    private = Path(task["private_fixture_dir"])
    fixture = private / "proj_with_test"
    if not fixture.is_dir():
        raise FileNotFoundError(f"missing private fixture: {fixture}")

    eval_tmp_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"eval_{task['task_id']}_", dir=str(eval_tmp_root)))
    out_dir.mkdir(parents=True, exist_ok=True)

    eval_root = tmp / "repo"
    shutil.copytree(fixture, eval_root)

    copied: list[str] = []
    for f in workspace.rglob("*"):
        if not f.is_file():
            continue
        if any(p in SKIP_PARTS for p in f.parts):
            continue
        rel = f.relative_to(workspace)
        if rel.name in SKIP_NAMES:
            continue
        if _is_agent_test_path(rel):
            continue
        parts = list(rel.parts)
        if parts and parts[0] == "proj_clean":
            rel = Path(*parts[1:]) if len(parts) > 1 else Path(".")
            if str(rel) == ".":
                continue
        dest = eval_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        copied.append(str(rel))

    private_venv = private / ".preflight_venv" / "bin" / "python"
    logs: list[str] = []
    if private_venv.exists():
        py = private_venv
        logs.append("using_preflight_venv")
    else:
        venv_dir = tmp / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        py = venv_dir / "bin" / "python"
        subprocess.run(
            [str(py), "-m", "pip", "install", "-q", "pytest", "pytest-timeout", "pytest-asyncio"],
            cwd=str(eval_root),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if (eval_root / "pyproject.toml").exists() or (eval_root / "setup.py").exists():
            r = subprocess.run(
                [str(py), "-m", "pip", "install", "-q", "-e", str(eval_root)],
                cwd=str(eval_root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            logs.append(r.stderr[-2000:] if r.stderr else "")
        elif (eval_root / "requirements.txt").exists():
            r = subprocess.run(
                [str(py), "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                cwd=str(eval_root),
                capture_output=True,
                text=True,
                timeout=300,
            )
            logs.append(r.stderr[-2000:] if r.stderr else "")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(eval_root) + (
        os.pathsep + str(eval_root / "src") if (eval_root / "src").exists() else ""
    )
    junit = out_dir / "junit.xml"
    cmd = [str(py), "-m", "pytest", "-q", "--tb=line", "--timeout=60", f"--junitxml={junit}"]
    try:
        proc = subprocess.run(
            cmd, cwd=str(eval_root), capture_output=True, text=True, timeout=180, env=env
        )
        timed_out = False
        rc = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = -1
        stdout = e.stdout if isinstance(e.stdout, str) else ""
        stderr = e.stderr if isinstance(e.stderr, str) else ""

    (out_dir / "evaluation.log").write_text(
        "COPIED_FILES\n"
        + "\n".join(copied)
        + "\n\nSTDOUT\n"
        + (stdout or "")
        + "\n\nSTDERR\n"
        + (stderr or "")
        + "\n\nINSTALL\n"
        + "\n".join(logs),
        encoding="utf-8",
    )
    # Prefer junit.xml (reliable under -q); fall back to stdout parsing.
    counts = parse_junit_counts(junit) or common.parse_pytest_counts(stdout or "", stderr or "", rc)
    # If stdout parser missed a quiet all-pass run, recover from progress line + rc.
    if (
        not counts.get("parsed_ok")
        or (
            counts.get("collected") in (0, None)
            and counts["passed"] + counts["failed"] + counts["error"] == 0
            and rc == 0
            and re.search(r"\[100%\]", stdout or "")
        )
    ):
        j2 = parse_junit_counts(junit)
        if j2:
            counts = j2

    evaluator_failure = False
    failure_kind = None
    if timed_out:
        failure_kind = "timeout"
        evaluator_failure = True
    elif counts.get("collection_failure"):
        failure_kind = "collection_failure"
        evaluator_failure = True
    elif counts.get("collected") in (0, None) and (
        counts["passed"] + counts["failed"] + counts["error"] == 0
    ):
        failure_kind = "collection_failure"
        evaluator_failure = True
    elif (
        "ERROR: " in (stderr or "")
        and counts["passed"] == 0
        and not counts.get("parsed_ok")
    ):
        failure_kind = "dependency/setup failure"
        evaluator_failure = True

    denom = counts["passed"] + counts["failed"] + counts["error"]
    if denom == 0 or (
        evaluator_failure
        and counts["passed"] == 0
        and counts["failed"] == 0
        and counts["error"] == 0
    ):
        test_pass_rate = None
    else:
        test_pass_rate = counts["passed"] / denom

    repo_success = (
        1
        if (
            counts["failed"] == 0
            and counts["error"] == 0
            and (counts.get("collected") or 0) >= 1
            and not evaluator_failure
        )
        else 0
    )
    if (counts.get("collected") or 0) < 1 and counts["passed"] < 1:
        repo_success = 0
        test_pass_rate = None
        evaluator_failure = True
        failure_kind = failure_kind or "collection_failure"

    return {
        "passed": counts["passed"],
        "failed": counts["failed"],
        "error": counts["error"],
        "skipped": counts["skipped"],
        "collected": counts.get("collected"),
        "test_pass_rate": test_pass_rate,
        "repo_success": repo_success,
        "evaluator_failure": evaluator_failure,
        "failure_kind": failure_kind,
        "timed_out": timed_out,
        "returncode": rc,
        "copied_file_count": len(copied),
        "evaluation_wall_seconds": time.monotonic() - t0,
        "eval_tmpdir": str(tmp),
        "canonical_repo": str(workspace),
        "private_fixture": str(fixture),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluable = [r for r in rows if not r.get("infra_failure")]
    valid_rates = [r["test_pass_rate"] for r in evaluable if r.get("test_pass_rate") is not None]
    sum_p = sum(r.get("passed") or 0 for r in evaluable)
    sum_den = sum(
        (r.get("passed") or 0) + (r.get("failed") or 0) + (r.get("error") or 0)
        for r in evaluable
    )
    micro = (sum_p / sum_den) if sum_den else None
    macro = statistics.mean(valid_rates) if valid_rates else None
    repo_ok = sum(1 for r in evaluable if r.get("repo_success") == 1)
    return {
        "n_runs": len(rows),
        "n_evaluable": len(evaluable),
        "micro_test_pass_rate": micro,
        "macro_test_pass_rate": macro,
        "repository_success_rate": (repo_ok / len(evaluable)) if evaluable else None,
        "repo_success_count": repo_ok,
        "evaluator_failure_count": sum(1 for r in rows if r.get("evaluator_failure")),
        "generated_at": utc_now(),
    }


def write_summary_md(summary: dict[str, Any], rows: list[dict[str, Any]], path: Path) -> None:
    lines = [
        "# RealBench AdaMAS Codex Decomp Baseline — Hidden Test Eval",
        "",
        f"- Generated: {summary['generated_at']}",
        f"- Batch: `{summary.get('batch_id')}`",
        "- Protocol: overlay agent **source** onto private `proj_with_test`, then pytest "
        "(agent `tests/` skipped; counts from junit.xml)",
        f"- Runs: {summary['n_runs']}",
        f"- Evaluable: {summary['n_evaluable']}",
        "",
        "## Correctness",
        "",
        f"- Micro Test Pass Rate: `{summary['micro_test_pass_rate']}`",
        f"- Macro Test Pass Rate: `{summary['macro_test_pass_rate']}`",
        f"- Repository Success Rate: `{summary['repository_success_rate']}` "
        f"({summary['repo_success_count']}/{summary['n_evaluable']})",
        "",
        f"- Evaluator failure count: `{summary['evaluator_failure_count']}`",
        "",
        "## Per-task results",
        "",
        (
            "| task_id | domain | pass_rate | repo_success | "
            "passed/failed/error | collected | eval_s | evaluator_fail |"
        ),
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        denom = f"{r.get('passed')}/{r.get('failed')}/{r.get('error')}"
        lines.append(
            f"| {r['task_id']} | {r.get('domain')} | {r.get('test_pass_rate')} | "
            f"{r.get('repo_success')} | {denom} | {r.get('collected')} | "
            f"{round(r.get('evaluation_wall_seconds') or 0, 1)} | {r.get('evaluator_failure')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--batch-dir",
        type=Path,
        default=ADAMAS_ROOT
        / "outputs"
        / "realbench_codex_decomp_baseline"
        / "rb-decomp-20260803T142700Z",
    )
    ap.add_argument(
        "--manifest",
        type=Path,
        default=VANILLA_ROOT / "selected_tasks" / "manifest.json",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Defaults to <batch-dir>/hidden_eval",
    )
    args = ap.parse_args()
    batch_dir = args.batch_dir.resolve()
    out_dir = (args.out_dir or (batch_dir / "hidden_eval")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    common = _import_vanilla()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    tasks = manifest["selected_tasks"]
    eval_tmp = out_dir / "_eval_tmp"

    rows: list[dict[str, Any]] = []
    for task in tasks:
        task_id = task["task_id"]
        task_out = out_dir / task_id
        task_out.mkdir(parents=True, exist_ok=True)
        print(f"===== EVAL {task_id} =====", flush=True)
        try:
            workspace = resolve_canonical_repo(batch_dir, task_id)
            ev = evaluate_hidden(
                common=common,
                task=task,
                workspace=workspace,
                out_dir=task_out,
                eval_tmp_root=eval_tmp,
            )
            row = {
                "task_id": task_id,
                "domain": task.get("domain"),
                "mode": "adamas_codex_decomp_baseline",
                "agent_status": "ok",
                "infra_failure": False,
                "latency_ms": None,
                **ev,
            }
        except Exception as exc:  # noqa: BLE001 — surface per-task eval errors
            row = {
                "task_id": task_id,
                "domain": task.get("domain"),
                "mode": "adamas_codex_decomp_baseline",
                "agent_status": "eval_error",
                "infra_failure": False,
                "evaluator_failure": True,
                "failure_kind": "eval_error",
                "error": str(exc),
                "test_pass_rate": None,
                "repo_success": 0,
                "passed": 0,
                "failed": 0,
                "error_count": 0,
            }
            print(f"ERROR {task_id}: {exc}", flush=True)
        write_json(task_out / "metrics.json", row)
        rows.append(row)
        print(
            f"  pass_rate={row.get('test_pass_rate')} repo_success={row.get('repo_success')} "
            f"p/f/e={row.get('passed')}/{row.get('failed')}/{row.get('error')}",
            flush=True,
        )

    summary = summarize(rows)
    summary["batch_id"] = batch_dir.name
    summary["batch_dir"] = str(batch_dir)
    summary["protocol"] = "vanilla_evaluate_hidden"
    summary["results"] = rows
    write_json(out_dir / "results.json", rows)
    write_json(out_dir / "summary.json", summary)
    write_summary_md(summary, rows, out_dir / "summary.md")
    # Also mirror under AdaMAS reports for discoverability.
    reports = ADAMAS_ROOT / "outputs" / "realbench_codex_decomp_baseline" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out_dir / "summary.md", reports / f"{batch_dir.name}_hidden_eval.md")
    print(json.dumps({k: summary[k] for k in summary if k != "results"}, indent=2), flush=True)
    print(f"wrote {out_dir / 'summary.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
