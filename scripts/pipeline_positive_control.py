#!/usr/bin/env python3
"""Whole-pipeline positive control (user decision 2026-10-15): a tiny synthetic task with planted errors.

No model, no quota. A script stands in for the agents and makes fixed changes; every link the sandbox baseline found
silently broken is then checked on the real components, under the real sandbox:

  gate        the real check script (materialised like a run's), confined, finds exactly the planted errors of the
              current milestone, and its result travels in the structured results file;
  regression  FastLoopController._route_and_classify -- the diagnosis entry the controller calls -- runs the
              predecessor suites on the incumbent's code (not the run's initial workspace) and finds the predecessor
              functions the implementer broke on purpose, including in a large suite;
  large suite a predecessor suite of 120 cases (score line far beyond 4000 characters) is read in full;
  evidence    a row candidate (id with a colon, as the playbook rows have) gets an evidence directory with every
              suite's failures, which passes the delivery check (exists, not empty, collected, readable by the
              repairer's sandbox) and whose runs really fail on the incumbent;
  repair      the scripted repairer's fix clears the gate and every predecessor suite.

Synthetic package ``pcalc``: m1 core arithmetic (5 cases), m2 number formatting (120 cases), m3 statistics (6 cases).
The scripted m3 implementer plants two bugs in m3, breaks core.sub (m1) and negative formatting (m2, 20 cases).

    uv run python scripts/pipeline_positive_control.py [--keep]

Exit 0 only when every check passes; the JSON report names each check. Every pipeline change must pass this first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "outputs" / "pipeline_positive_control"
ENV_PYTHON = Path("/root/codex-benchmarks/cpe_envs/zxcvbn/bin/python")
TASK = "rb_pcalc"
NEG_CASES = 20

# --------------------------------------------------------------------------- the synthetic task

CORE_OK = """def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b


def div(a, b):
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b
"""
FMT_OK = """def fmt_number(x, places=2):
    sign = "-" if x < 0 else ""
    return sign + format(abs(x), "." + str(places) + "f")
"""
STATS_OK = """from pcalc.core import add, div, sub, mul


def mean(xs):
    total = 0
    for x in xs:
        total = add(total, x)
    return div(total, len(xs))


def median(xs):
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return div(add(s[mid - 1], s[mid]), 2)


def variance(xs):
    m = mean(xs)
    return div(sum(mul(sub(x, m), sub(x, m)) for x in xs), len(xs) - 1)
"""
# the scripted implementer of m3: two planted bugs in m3, one regression in m1, one in m2 (negative numbers)
CORE_BROKEN = CORE_OK.replace("    return a - b", "    return b - a")
FMT_BROKEN = """def fmt_number(x, places=2):
    return format(abs(x), "." + str(places) + "f")
"""
STATS_BROKEN = STATS_OK.replace("    return s[mid]\n    return div(add(s[mid - 1], s[mid]), 2)",
                                "    return s[mid]\n    return s[mid]").replace("len(xs) - 1)", "len(xs))")

SUITE_M1 = """from spec_tests.helpers import pair


def test_add():
    from pcalc.core import add
    assert add(*pair) == 5


def test_sub():
    from pcalc.core import sub
    assert sub(*pair) == -1


def test_mul():
    from pcalc.core import mul
    assert mul(*pair) == 6


def test_div():
    from pcalc.core import div
    assert div(6, 3) == 2


def test_div_zero():
    import pytest
    from pcalc.core import div
    with pytest.raises(ZeroDivisionError):
        div(1, 0)
"""
SUITE_M2 = f"""import pytest

POSITIVE = [(i * 1.25, 2, format(i * 1.25, ".2f")) for i in range(100)]
NEGATIVE = [(-(i + 1) * 0.5, 1, "-" + format((i + 1) * 0.5, ".1f")) for i in range({NEG_CASES})]


@pytest.mark.parametrize("value,places,expected", POSITIVE,
                         ids=["formats_positive_value_with_requested_places_case_%03d" % i for i in range(len(POSITIVE))])
def test_fmt_number_positive(value, places, expected):
    from pcalc.fmt import fmt_number
    assert fmt_number(value, places) == expected


@pytest.mark.parametrize("value,places,expected", NEGATIVE,
                         ids=["formats_negative_value_keeps_its_sign_case_%03d" % i for i in range(len(NEGATIVE))])
def test_fmt_number_negative(value, places, expected):
    from pcalc.fmt import fmt_number
    assert fmt_number(value, places) == expected
"""
SUITE_M3 = """def test_mean():
    from pcalc.stats import mean
    assert mean([1, 2, 3, 4]) == 2.5


def test_median_odd():
    from pcalc.stats import median
    assert median([3, 1, 2]) == 2


def test_median_even():
    from pcalc.stats import median
    assert median([4, 1, 3, 2]) == 2.5


def test_variance_is_the_sample_variance():
    from pcalc.stats import variance
    assert variance([1, 2, 3, 4]) == 5 / 3


def test_mean_single():
    from pcalc.stats import mean
    assert mean([7]) == 7


def test_median_single():
    from pcalc.stats import median
    assert median([7]) == 7
"""
PLANTED_M3 = {"test_median_even", "test_variance_is_the_sample_variance"}
PLANTED_M1 = {"test_sub"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def _commit_tree(repo: Path, files: dict[str, str], message: str) -> None:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=pc@adamas", "-c", "user.name=pc", "commit", "-q", "-m", message)


def build(root: Path) -> dict:
    from orchestra.codeprojecteval.harness import _check_script_source, check_command, install_testkit

    harness = root / "harness"
    harness.mkdir(parents=True)
    (harness / "adamas_cpe_check.py").write_text(_check_script_source())
    install_testkit(harness)
    pristine = harness / "pristine_repo"
    (pristine / "docs").mkdir(parents=True)
    (pristine / "docs" / "README.md").write_text("pcalc: arithmetic, formatting, statistics.\n")
    (harness / "adamas_cpe_harness.json").write_text(json.dumps({
        "expected_modules": ["pcalc", "pcalc.core", "pcalc.fmt", "pcalc.stats"], "top_level_packages": ["pcalc"],
        "check_tests_dir": "check_tests", "source_dir": "pcalc", "spec_tests_dir": "spec_tests",
        "pristine_repo": str(pristine), "env_python": str(ENV_PYTHON)}))
    suites = {"m1": SUITE_M1, "m2": SUITE_M2, "m3": SUITE_M3}
    graphs = {}
    for mid, src in suites.items():
        frozen = harness / f"{mid}.spec_tests"
        frozen.mkdir()
        (frozen / "__init__.py").write_text("")
        (frozen / f"test_{mid}.py").write_text(src)
        if mid == "m1":
            (frozen / "helpers.py").write_text("pair = (2, 3)\n")
        cmd = check_command(harness_dir=harness, level="implementation", env_python=ENV_PYTHON, spec_tests_path=frozen)
        g = root / "generated" / "graphs" / f"{mid}.yaml"
        g.parent.mkdir(parents=True, exist_ok=True)
        g.write_text(json.dumps({
            "graph_id": f"pc_{mid}", "version": "1.0",
            "initial_artifact_slots": {"repository_change": "RepositoryChangeArtifact"}, "final_output_slot": "result",
            "nodes": [{"node_id": "gate", "node_kind": "harness", "harness_id": "repository_test_harness",
                       "visibility": "public", "command": cmd, "timeout_seconds": 300,
                       "input_slots": {"repository_change": "RepositoryChangeArtifact"},
                       "output_slots": {"result": "RepositoryHarnessResultArtifact"}}], "edges": []}))
        graphs[mid] = g
    # the run's initial workspace (dataset inputs only): the tree the 10-12 diagnosis wrongly ran in
    initial = root / "workspaces" / "pcalc"
    initial.mkdir(parents=True)
    _git(initial, "init", "-q")
    _commit_tree(initial, {"docs/README.md": "pcalc: arithmetic, formatting, statistics.\n"}, "dataset inputs only")
    # canonical after m1 and m2 committed (correct code)
    canonical = root / "canonical" / "repo"
    shutil.copytree(initial, canonical)
    _commit_tree(canonical, {"pcalc/__init__.py": "", "pcalc/core.py": CORE_OK}, "canonical commit m1")
    _commit_tree(canonical, {"pcalc/fmt.py": FMT_OK}, "canonical commit m2")
    return {"harness": harness, "graphs": graphs, "initial": initial, "canonical": canonical}


# --------------------------------------------------------------------------- checks


class _Store:
    def __init__(self):
        self.items: dict[str, str] = {}

    async def get(self, artifact_id):
        return SimpleNamespace(payload={"patch": self.items[artifact_id]})


def _key(node: str) -> str:
    from orchestra.control.fast_loop.persistence import failure_key

    return failure_key(node)


async def run_checks(root: Path) -> list[dict]:
    from orchestra.control.fast_loop.committed_cases import write_committed_cases
    from orchestra.control.fast_loop.controller import DIAGNOSIS_WORKSPACE, FastLoopController
    from orchestra.control.fast_loop.evolution_config import EvolutionConfig
    from orchestra.control.fast_loop.prior_suites import gate_command_of, prior_suite_report, prior_suites_for
    from orchestra.control.fast_loop.repair_evidence import MANIFEST
    from orchestra.control.fast_loop.schemas import CandidateRecord, FailureDiagnosis, FastLoopState
    from orchestra.control.fast_loop.workspace import GitCandidateWorkspaceManager
    from orchestra.control.task_state import SubtaskFailureReason
    from orchestra.harness.command_runner import run_harness
    from orchestra.harness.progress import behaviour_failures, behaviour_passed, progress_from_payload
    from orchestra.ir.graph import load_graph

    checks: list[dict] = []

    def check(name: str, ok: bool, **detail) -> bool:
        checks.append({"check": name, "ok": bool(ok), **detail})
        return bool(ok)

    t = build(root)
    cmd = {m: gate_command_of(load_graph(g))[0] for m, g in t["graphs"].items()}

    async def gate(repo: Path, mid: str):
        run = await run_harness(cwd=repo, command=cmd[mid], timeout_seconds=300)
        score, stages, _ = progress_from_payload(run.payload)
        return run, stages

    # predecessors committed: their committed-case records, from the real gate on the canonical tree
    for mid in ("m1", "m2"):
        run, stages = await gate(t["canonical"], mid)
        passed = sorted(behaviour_passed(stages))
        spec = next((s for s in stages if s.stage == "spec_tests"), None)
        expect = 5 if mid == "m1" else 100 + NEG_CASES
        check(f"gate/{mid}: committed tree passes its whole suite", spec is not None and spec.passed_units == expect == spec.total_units,
              passed=spec.passed_units if spec else None, total=spec.total_units if spec else None)
        if mid == "m2":
            check("large suite: 120-case result read in full from the results file",
                  run.payload is not None and len(passed) == expect and len(json.dumps(run.payload)) > 4000,
                  payload_chars=len(json.dumps(run.payload or {})), stdout_tail_chars=len(run.stdout))
        write_committed_cases(str(t["harness"] / f"{mid}.spec_tests"), passed=passed, failed=[], source="positive_control",
                              milestone_id=mid)

    # the scripted m3 implementer: first-attempt workspace = canonical + its change
    wsm = GitCandidateWorkspaceManager()
    first = root / "tasks" / TASK / "workspaces" / "m3" / "repo"
    shutil.copytree(t["canonical"], first)
    (first / "pcalc" / "stats.py").write_text(STATS_BROKEN)
    (first / "pcalc" / "core.py").write_text(CORE_BROKEN)
    (first / "pcalc" / "fmt.py").write_text(FMT_BROKEN)
    _git(first, "add", "-A")
    patch = _git(first, "diff", "--cached", "--binary", "HEAD")
    store = _Store()
    store.items["art-m3"] = patch

    # 1. the gate finds exactly the planted m3 errors
    run, stages = await gate(first, "m3")
    failed = {_key(n).split("::", 1)[-1] for n in behaviour_failures(stages)}
    check("gate/m3: finds exactly the planted errors", failed == PLANTED_M3, found=sorted(failed), planted=sorted(PLANTED_M3),
          structured=run.payload is not None)
    gate_failures = list(behaviour_failures(stages))

    # 2. the diagnosis: predecessor suites on the incumbent's code, through the controller's own entry
    base_ws = await wsm.prepare_base_snapshot(source_repo=str(t["canonical"]), run_dir=str(root), task_id=TASK, subtask_id="m3")
    spec = lambda deps, g: SimpleNamespace(dependencies=deps, local_graph_template=str(g), metadata={"focus_paths": []})  # noqa: E731
    state = SimpleNamespace(task_id=TASK, subtasks={
        "m1": SimpleNamespace(status="committed", spec=spec([], t["graphs"]["m1"])),
        "m2": SimpleNamespace(status="committed", spec=spec(["m1"], t["graphs"]["m2"])),
        "m3": SimpleNamespace(status="running", spec=spec(["m1", "m2"], t["graphs"]["m3"])),
    })
    check("diagnosis: both predecessors are found", [p.milestone_id for p in prior_suites_for(state, "m3")] == ["m1", "m2"])
    ctl = FastLoopController.__new__(FastLoopController)
    ctl.workspace_manager = wsm
    ctl.artifact_store = store
    ctl.evolution = EvolutionConfig(enabled=True)
    fl = FastLoopState(subtask_id="m3", base_attempt_id=1, base_graph_hash="pc",
                       diagnosis=FailureDiagnosis(reason=SubtaskFailureReason.HARNESS, retryable=True, concise_feedback=""))
    fl.routing["reference_filter"] = {"applied": False, "removed": []}      # synthetic task: no reference
    summary = SimpleNamespace(persistent=gate_failures, flaky=[], samples=2)
    ctx = SimpleNamespace(run_dir=str(root))
    repair, _classes = await ctl._route_and_classify(
        state=state, fl_state=fl, sub=state.subtasks["m3"], subtask_id="m3", base_graph=load_graph(t["graphs"]["m3"]),
        summary=summary, source_repo=str(t["initial"]), base_ws=base_ws, context=ctx, incumbent_artifact="art-m3")
    diag = fl.routing.get("diagnosis_workspace", "")
    check("diagnosis: runs on the incumbent's code, not the initial workspace",
          diag.endswith(f"/{DIAGNOSIS_WORKSPACE}/repo") and str(t["initial"]) not in diag, workspace=diag)
    report = fl.routing.get("incumbent_prior_suites") or {}
    r1 = {_key(k).split("::", 1)[-1] for k in (report.get("m1") or {}).get("regressions", [])}
    r2 = (report.get("m2") or {}).get("regressions", [])
    check("regression: finds the broken m1 function", r1 == PLANTED_M1, found=sorted(r1))
    check("regression: finds the broken m2 behaviour in the 120-case suite",
          (report.get("m2") or {}).get("ran") is True and len(r2) == NEG_CASES and all("negative" in k for k in r2),
          found=len(r2), ran=(report.get("m2") or {}).get("ran"), passed=(report.get("m2") or {}).get("passed_count"))
    expected_repair = len(PLANTED_M3) + len(PLANTED_M1) + NEG_CASES
    check("diagnosis: the repair list holds own failures and every regression", len(repair) == expected_repair,
          repair=len(repair), expected=expected_repair)
    # the 10-12 tree: the same check run on the initial workspace yields no fake regression
    initial_report = await prior_suite_report(workspace_path=str(t["initial"]), command=cmd["m3"],
                                              priors=prior_suites_for(state, "m3"), timeout_seconds=300)
    check("regression: the initial workspace produces no fake regression",
          all(not e.get("regressions") for e in initial_report.values()),
          initial={m: (len(e.get("regressions", [])), e.get("error")) for m, e in initial_report.items()})

    # 3. evidence for a table row (candidate id with a colon) and the delivery check
    cand_id = "cand_v2:E3-T1"
    cand_ws = await wsm.fork_candidate_workspace(base=base_ws, run_dir=str(root), task_id=TASK, subtask_id="m3",
                                                 candidate_id=cand_id)
    await wsm.apply_patch(cand_ws, patch)
    check("colon: the candidate directory name holds no colon", ":" not in cand_ws.path and os.pathsep not in cand_ws.path,
          path=cand_ws.path)
    record = CandidateRecord(candidate_id=cand_id, attempt_id=2, graph_hash="pc", parent_graph_hash="pc", edits=[],
                             metadata={"persistent_failures": list(repair)})
    delivered = await ctl._stage_repair_evidence(record, cand_ws, ctx)
    delivery = record.metadata.get("evidence_delivery") or {}
    check("evidence: passes the delivery check (exists, not empty, collected, readable by the repairer)",
          delivered and delivery.get("ok") and delivery.get("readable") is True, delivery=delivery)
    ev = Path(record.metadata.get("repair_evidence") or "/nonexistent")
    manifest = json.loads((ev / MANIFEST).read_text()) if (ev / MANIFEST).is_file() else {}
    suites = {s["suite"]: s for s in manifest.get("suites") or []}
    check("evidence: every suite's failures are staged", set(suites) == {"m1", "m2", "m3"}
          and manifest.get("staged") == expected_repair and manifest.get("truncated") == 0,
          suites={k: v["staged"] for k, v in suites.items()}, staged=manifest.get("staged"))
    check("evidence: every staged case really fails on the incumbent",
          all((s.get("counts") or {}).get("failed") == s["staged"] for s in suites.values()),
          counts={k: v.get("counts") for k, v in suites.items()})
    log = root / "repair_evidence_delivery.jsonl"
    check("evidence: the hand-over is recorded for the delivery audit", log.is_file() and cand_id in log.read_text())

    # 4. the scripted repairer fixes everything; the gate and every predecessor suite pass
    repo = Path(cand_ws.path)
    (repo / "pcalc" / "stats.py").write_text(STATS_OK)
    (repo / "pcalc" / "core.py").write_text(CORE_OK)
    (repo / "pcalc" / "fmt.py").write_text(FMT_OK)
    run, stages = await gate(repo, "m3")
    check("repair: the fixed candidate passes the m3 gate", not behaviour_failures(stages) and bool(behaviour_passed(stages)))
    after = await prior_suite_report(workspace_path=str(repo), command=cmd["m3"], priors=prior_suites_for(state, "m3"),
                                     timeout_seconds=300)
    check("repair: no predecessor regression remains", all(e.get("ran") and not e.get("regressions") for e in after.values()),
          after={m: (e.get("ran"), len(e.get("regressions", []))) for m, e in after.items()})
    return checks


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the run directory even when every check passes")
    a = ap.parse_args()
    from orchestra.sandbox.guard import require_sandbox

    require_sandbox("pipeline positive control")
    os.environ.setdefault("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", "1")    # as the run entry sets it
    root = OUT / ("run-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    root.mkdir(parents=True)
    try:
        checks = asyncio.run(run_checks(root))
    except Exception as exc:  # noqa: BLE001 -- a crash is a failed control, reported as such
        import traceback

        checks = [{"check": "control ran to the end", "ok": False, "error": f"{type(exc).__name__}: {exc}",
                   "traceback": traceback.format_exc()[-3000:]}]
    ok = all(c["ok"] for c in checks)
    report = {"ok": ok, "passed": sum(c["ok"] for c in checks), "total": len(checks), "run_dir": str(root), "checks": checks}
    (OUT / "last.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str))
    if ok and not a.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
