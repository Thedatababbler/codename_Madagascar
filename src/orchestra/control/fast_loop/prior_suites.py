"""Run committed predecessors' frozen suites on a candidate workspace, for diagnosis only.

A later milestone can break what an earlier one delivered, and nothing in
the earlier milestone's gate runs again once it is frozen. This module
re-runs each committed predecessor's frozen suite (and its contracts) on a
candidate's repository through the same gate script the milestone itself
uses -- the harness is untouched, only its ``--spec-tests`` / ``--contracts``
/ ``--level`` arguments point at the predecessor -- and compares the outcome
with what that predecessor recorded when it committed
(``<milestone>.committed_cases.json``). Nothing here is scored; the result is
a list of regressed case ids per predecessor, which the unified acceptance
rule refuses and the routing diagnosis labels ``regression_by_current``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestra.control.fast_loop.committed_cases import read_committed_cases
from orchestra.control.fast_loop.persistence import failure_key
from orchestra.harness.command_runner import run_authoritative_harness_command
from orchestra.harness.progress import behaviour_failures, behaviour_passed, parse_progress
from orchestra.ir.graph import OrchestraGraph, load_graph
from orchestra.ir.nodes import NodeKind


@dataclass(frozen=True)
class PriorSuite:
    milestone_id: str
    spec_dir: str
    contracts: str
    level: str
    committed_passed: frozenset[str]
    committed_failed: frozenset[str]


def _arg(command: list[str], flag: str) -> str | None:
    if flag in command:
        i = command.index(flag)
        if i + 1 < len(command):
            return command[i + 1]
    return None


def gate_command_of(graph: OrchestraGraph) -> tuple[list[str], float] | None:
    for node in graph.nodes:
        if node.node_kind is NodeKind.HARNESS:
            cmd = list(getattr(node, "command", None) or [])
            if "--spec-tests" in cmd or "--manifest" in cmd:
                return cmd, float(getattr(node, "timeout_seconds", None) or 900.0)
    return None


def prior_suites_for(state: Any, subtask_id: str) -> list[PriorSuite]:
    """Committed transitive dependencies of ``subtask_id`` that froze a suite and a case record."""
    subs = getattr(state, "subtasks", {}) or {}
    seen: set[str] = set()
    order: list[str] = []

    def walk(sid: str) -> None:
        sub = subs.get(sid)
        if sub is None:
            return
        for dep in list(getattr(sub.spec, "dependencies", None) or []):
            if dep in seen:
                continue
            seen.add(dep)
            walk(dep)
            order.append(dep)

    walk(subtask_id)
    out: list[PriorSuite] = []
    for dep in order:
        sub = subs.get(dep)
        if sub is None or str(getattr(sub.status, "value", sub.status)) != "committed":
            continue
        try:
            graph = load_graph(sub.spec.local_graph_template)
        except Exception:  # noqa: BLE001 -- a predecessor whose graph is gone is skipped, not fatal
            continue
        found = gate_command_of(graph)
        if found is None:
            continue
        cmd, _ = found
        spec_dir = _arg(cmd, "--spec-tests")
        if not spec_dir or not Path(spec_dir).is_dir():
            continue
        record = read_committed_cases(spec_dir)
        if record is None:
            continue
        out.append(
            PriorSuite(
                milestone_id=dep,
                spec_dir=spec_dir,
                contracts=_arg(cmd, "--contracts") or "",
                level=_arg(cmd, "--level") or "integration",
                committed_passed=frozenset(record["passed"]),
                committed_failed=frozenset(record["failed"]),
            )
        )
    return out


def rewrite_command(command: list[str], prior: PriorSuite) -> list[str]:
    """The current gate command, pointed at the predecessor's suite, without custody."""
    out: list[str] = []
    skip = False
    for i, tok in enumerate(command):
        if skip:
            skip = False
            continue
        if tok == "--take-custody":
            continue
        if tok in ("--spec-tests", "--contracts", "--level"):
            value = {"--spec-tests": prior.spec_dir, "--contracts": prior.contracts, "--level": prior.level}[tok]
            if value:
                out.extend([tok, value])
            skip = True
            continue
        out.append(tok)
    if "--spec-tests" not in out:
        out.extend(["--spec-tests", prior.spec_dir])
    if prior.contracts and "--contracts" not in out:
        out.extend(["--contracts", prior.contracts])
    return out


async def run_prior_suite(
    *, workspace_path: str, command: list[str], prior: PriorSuite, timeout_seconds: float
) -> dict[str, Any]:
    """``{"regressions": [...], "failed": [...], "passed_count": n, "ran": bool}`` for one predecessor."""
    cmd = rewrite_command(command, prior)
    _passed, _code, stdout, _stderr = await run_authoritative_harness_command(
        cwd=workspace_path, command=cmd, timeout_seconds=timeout_seconds
    )
    score, stages, _furthest = parse_progress(stdout)
    if score is None and not stages:
        return {"regressions": [], "failed": [], "passed_count": 0, "ran": False}
    failed = {failure_key(n) for n in behaviour_failures(stages)}
    passed = {failure_key(n) for n in behaviour_passed(stages)}
    regressions = sorted(k for k in prior.committed_passed if k in failed)
    return {
        "regressions": regressions,
        "failed": sorted(failed),
        "passed_count": len(passed),
        "ran": True,
    }


async def prior_suite_report(
    *, workspace_path: str, command: list[str], priors: list[PriorSuite], timeout_seconds: float
) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for prior in priors:
        try:
            report[prior.milestone_id] = await run_prior_suite(
                workspace_path=workspace_path, command=command, prior=prior, timeout_seconds=timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 -- diagnosis must never fail a search
            report[prior.milestone_id] = {"regressions": [], "failed": [], "passed_count": 0, "ran": False, "error": str(exc)[:200]}
    return report


__all__ = [
    "PriorSuite",
    "gate_command_of",
    "prior_suite_report",
    "prior_suites_for",
    "rewrite_command",
    "run_prior_suite",
]
