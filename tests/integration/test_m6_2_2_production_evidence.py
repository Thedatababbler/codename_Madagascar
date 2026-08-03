"""M6.2.2 — production evidence closure (cost, held-out, attempts, reports, ownership)."""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from orchestra.cli.stage2_pareto_experiments import cmd_freeze_calibration
from orchestra.control.pareto.controller import ParetoGlobalCandidatePolicy
from orchestra.control.pareto.schemas import ParetoConfig, PreferenceProfile
from orchestra.control.run_ownership import RunOwnershipError
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.experiments.control_plane import load_control_plane_mapping
from orchestra.experiments.stage2_fixture import run_stage2_fixture, stage2_fixture_plan
from orchestra.experiments.stage2_pareto import (
    HELDOUT_REQUIRED_IDENTITY_FIELDS,
    CalibrationArtifact,
    CalibrationMismatchError,
    assert_calibration_matches,
    collect_run_records,
    selection_identity_from_manifest,
    write_stage2_report,
)
from orchestra.ir.artifacts import ArtifactBundle

REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "configs/experiments/stage2/m6_balanced_knee.yaml"


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _run_production_dev(tmp_path: Path, run_id: str) -> Path:
    from orchestra.cli import run_m6_orchestra as mod

    args = SimpleNamespace(
        config=str(CFG),
        plan=None,
        run_id=run_id,
        output_root=str(tmp_path),
        source_repo=None,
        mock_backends=True,
        mock_llm=False,
        allow_config_drift=False,
        skip_healthcheck=True,
    )
    code = await mod._run(args)
    assert code == 0
    run_dir = tmp_path / run_id
    assert (run_dir / "run_manifest.json").exists()
    summary = json.loads(
        (run_dir / "m6_orchestra_summary.json").read_text(encoding="utf-8")
    )
    assert set(summary.get("committed") or []) >= {"s1", "s2", "s3", "s4"}, summary
    assert int(summary.get("usage_record_count") or 0) == 12, summary
    return run_dir


def _heldout_from_calibration(payload: dict, *, run_id: str = "held-1") -> dict:
    return {
        "split": "heldout",
        "run_id": run_id,
        "started_at": "2020-01-01T00:00:00+00:00",
        "schema_version": payload["schema_version"],
        "git_sha": payload["git_sha"],
        "git_commit": payload["git_sha"],
        "selection_config_hash": payload["selection_config_hash"],
        "control_plane_hash": payload["control_plane_hash"],
        "preference_hash": payload["preference_hash"],
        "objective_hash": payload["objective_hash"],
        "objectives": payload["objectives"],
        "objective_directions": payload["objective_directions"],
        "objective_required": payload["objective_required"],
        "preference_profile_id": payload["preference_profile_id"],
        "preference_profile": payload["preference_profile"],
        "pricing_version": payload["pricing_version"],
        "pricing_registry_hash": payload["pricing_registry_hash"],
        "candidate_catalog_hash": payload["candidate_catalog_hash"],
        "graph_catalog_hash": payload["graph_catalog_hash"],
        "resolved_graph_hash": payload["resolved_graph_hash"],
        "benchmark_manifest_hash": payload["benchmark_manifest_hash"],
        "private_data_policy": payload["private_data_policy"],
        "public_evaluator_id": payload["public_evaluator_id"],
        "public_evaluator_version": payload["public_evaluator_version"],
        "backend_kinds": payload["backend_kinds"],
        "model_identifiers": payload["model_identifiers"],
        "backend_model_settings": payload["backend_model_settings"],
        "seed_policy": payload["seed_policy"],
        "dataset_identity": payload.get("dataset_identity")
        or {"benchmark": "livecodebench"},
        "dataset_split_identity": {"split": "heldout", "run_id": run_id},
        "stage2": {"mode": "m6_balanced_knee", "seed": 42},
    }


@pytest.mark.asyncio
async def test_production_development_realized_cost_and_freeze(tmp_path: Path):
    run_dir = await _run_production_dev(tmp_path, "prod-dev-cost")
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["split"] == "development"
    # Do not rewrite split after persistence.
    original = manifest["split"]

    ckpt = json.loads(
        next((run_dir / "tasks").rglob("task_execution.json")).read_text(encoding="utf-8")
    )
    usage = ckpt["backend_usage_records"]
    usage_ids = sorted(u["usage_id"] for u in usage)
    assert len(usage_ids) == len(set(usage_ids)) == 12
    assert all(u.get("estimated_cost_usd") is not None for u in usage)
    assert all(u.get("run_id") for u in usage)

    decisions = [
        json.loads(line)
        for line in (run_dir / "pareto" / "decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    realized = [d for d in decisions if d.get("realization_status") == "realized"]
    assert len(realized) == 1
    cost = (
        (realized[0].get("selected_candidate_snapshot") or {})
        .get("objectives", {})
        .get("values", {})
        .get("cost")
        or {}
    )
    assert cost.get("available") is True
    assert cost.get("value") == pytest.approx(1.08e-05)
    attributed = (realized[0].get("realization_evidence") or {}).get("usage_ids") or []
    assert len(attributed) == 9
    assert all("s1:" not in uid for uid in attributed)

    for sid in ("s2", "s3", "s4"):
        att = (ckpt["subtasks"][sid].get("attempts") or [])[-1]
        assert att.get("wave_id")
        assert att.get("execution_plan_revision") == realized[0]["activated_revision_id"]
        assert att.get("scheduler_incarnation") is not None
        assert att.get("usage_ids")
    assert ckpt["subtasks"]["s2"]["attempts"][-1]["wave_id"] == realized[0]["affected_wave_id"]
    assert ckpt["subtasks"]["s4"]["attempts"][-1]["wave_id"] != realized[0]["affected_wave_id"]

    ns = SimpleNamespace(
        config=str(CFG),
        run_dir=[str(run_dir)],
        output=str(tmp_path / "prod_cal.json"),
    )
    assert cmd_freeze_calibration(ns) == 0
    cal = json.loads(Path(ns.output).read_text(encoding="utf-8"))
    assert cal["source_split"] == "development"
    assert json.loads((run_dir / "run_manifest.json").read_text())["split"] == original
    assert "cost" in cal["normalization"]
    assert cal["normalization"]["cost"]["min"] == pytest.approx(1.08e-05)
    assert cal["normalization_source_record_ids"]["cost"]
    assert "fallback_default" not in json.dumps(cal)


@pytest.mark.asyncio
async def test_minimal_heldout_manifest_rejected(tmp_path: Path):
    run_dir = await _run_production_dev(tmp_path, "prod-for-cal")
    cal_path = tmp_path / "cal.json"

    assert cmd_freeze_calibration(
        SimpleNamespace(config=str(CFG), run_dir=[str(run_dir)], output=str(cal_path))
    ) == 0
    payload = json.loads(cal_path.read_text(encoding="utf-8"))
    control = load_control_plane_mapping(
        yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    )
    cal = CalibrationArtifact.from_dict(payload)
    minimal = {
        "split": "heldout",
        "run_id": "held-minimal",
        "started_at": "2020-01-01T00:00:00Z",
    }
    with pytest.raises(CalibrationMismatchError):
        assert_calibration_matches(
            cal, control, require_held_out_split=True, run_manifest=minimal
        )


@pytest.mark.asyncio
async def test_heldout_missing_or_mutated_identity_rejected(tmp_path: Path):
    run_dir = await _run_production_dev(tmp_path, "prod-h-matrix")
    cal_path = tmp_path / "cal.json"

    assert cmd_freeze_calibration(
        SimpleNamespace(config=str(CFG), run_dir=[str(run_dir)], output=str(cal_path))
    ) == 0
    payload = json.loads(cal_path.read_text(encoding="utf-8"))
    control = load_control_plane_mapping(
        yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    )
    cal = CalibrationArtifact.from_dict(payload)
    base = _heldout_from_calibration(payload)
    # selection_config_hash is presence-checked only (embeds source_run_id).
    mutate_fields = [
        f for f in HELDOUT_REQUIRED_IDENTITY_FIELDS if f != "selection_config_hash"
    ]
    for field in mutate_fields:
        deleted = dict(base)
        deleted.pop(field, None)
        if field == "git_sha":
            deleted.pop("git_commit", None)
        with pytest.raises(CalibrationMismatchError, match=".") as exc_del:
            assert_calibration_matches(
                cal, control, require_held_out_split=True, run_manifest=deleted
            )
        assert exc_del.value is not None, f"delete {field}"

        nulled = dict(base)
        nulled[field] = None
        if field == "git_sha":
            nulled["git_commit"] = None
        try:
            assert_calibration_matches(
                cal, control, require_held_out_split=True, run_manifest=nulled
            )
        except CalibrationMismatchError:
            pass
        else:
            raise AssertionError(f"null {field} did not raise")

        mutated = dict(base)
        if field == "dataset_split_identity":
            mutated[field] = {**base[field], "split": "development"}
        elif field == "dataset_identity":
            mutated[field] = {**base[field], "benchmark": "mutated-benchmark"}
        elif field == "git_sha":
            mutated[field] = "0" * 40
            mutated["git_commit"] = "0" * 40
        elif isinstance(base[field], dict):
            # Change a canonical nested value rather than only adding keys.
            first_key = next(iter(base[field]))
            mutated[field] = {
                **base[field],
                first_key: f"mutated-{first_key}",
            }
        elif isinstance(base[field], list):
            mutated[field] = list(base[field]) + ["mutated"]
        else:
            mutated[field] = f"mutated-{field}"
        try:
            assert_calibration_matches(
                cal, control, require_held_out_split=True, run_manifest=mutated
            )
        except CalibrationMismatchError:
            pass
        else:
            raise AssertionError(f"mutate {field} did not raise")


@pytest.mark.asyncio
async def test_matching_production_calibration_and_heldout_accepted(tmp_path: Path):
    run_dir = await _run_production_dev(tmp_path, "prod-match")
    cal_path = tmp_path / "cal.json"

    assert cmd_freeze_calibration(
        SimpleNamespace(config=str(CFG), run_dir=[str(run_dir)], output=str(cal_path))
    ) == 0
    payload = json.loads(cal_path.read_text(encoding="utf-8"))
    control = load_control_plane_mapping(
        yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    )
    held = tmp_path / "held"
    held.mkdir()
    held_manifest = _heldout_from_calibration(payload)
    (held / "run_manifest.json").write_text(
        json.dumps(held_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (held / "pareto").mkdir()
    (held / "pareto" / "decisions.jsonl").write_text("", encoding="utf-8")
    (held / "m6_orchestra_summary.json").write_text(
        json.dumps({"mode": "m6_balanced_knee", "committed": [], "task_id": "held"}),
        encoding="utf-8",
    )
    cal = CalibrationArtifact.from_dict(payload)
    assert_calibration_matches(
        cal, control, require_held_out_split=True, run_manifest=held_manifest
    )
    # git_commit alias normalizes to git_sha
    only_commit = dict(held_manifest)
    only_commit.pop("git_sha")
    selection_identity_from_manifest(only_commit, role="heldout_target")


@pytest.mark.asyncio
async def test_attempt_identity_mutations_block_realization(tmp_path: Path):
    run_dir = await _run_production_dev(tmp_path, "prod-att")
    ckpt_path = next((run_dir / "tasks").rglob("task_execution.json"))
    ckpt = json.loads(ckpt_path.read_text(encoding="utf-8"))
    state = TaskExecutionState.model_validate(ckpt)
    # Re-pending a realized decision for negative checks.
    hist = list(state.pareto_state.decision_history or [])
    assert hist
    pending = hist[-1].model_copy(
        update={"realization_status": "pending", "realization_id": None}
    )
    state.pareto_state.pending_decision = pending
    state.pareto_state.decision_history = []
    policy = ParetoGlobalCandidatePolicy(
        config=ParetoConfig(enabled=True),
        preference_profile=PreferenceProfile(profile_id="balanced_knee"),
        run_dir=str(run_dir),
    )

    def _ready_after(mutate):
        local = state.model_copy(deep=True)
        mutate(local)
        return policy._behavioral_realization_ready(
            local, local.pareto_state.pending_decision
        )

    ready, _ = _ready_after(lambda s: None)
    assert ready is True

    def _patch(sid: str, **updates):
        def _mut(s):
            att = s.subtasks[sid].attempts[-1]
            s.subtasks[sid].attempts[-1] = att.model_copy(update=updates)

        return _mut

    ready, ev = _ready_after(_patch("s2", wave_id=None))
    assert ready is False
    assert ev["reason"] == "attempt_wave_id_missing"

    ready, ev = _ready_after(_patch("s3", execution_plan_revision="wrong-rev"))
    assert ready is False
    assert "revision" in ev["reason"]

    ready, ev = _ready_after(_patch("s4", scheduler_incarnation=None))
    assert ready is False
    assert ev["reason"] == "attempt_scheduler_incarnation_missing"

    ready, ev = _ready_after(_patch("s2", usage_ids=[]))
    assert ready is False
    assert ev["reason"] == "attempt_usage_ids_missing"


@pytest.mark.asyncio
async def test_report_ignores_tampered_summary_and_counts_traces(tmp_path: Path):
    import csv

    run_dir = await _run_production_dev(tmp_path, "prod-rep")
    summary_path = run_dir / "m6_orchestra_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert set(summary.get("committed") or []) >= {"s1", "s2", "s3", "s4"}
    assert int(summary.get("usage_record_count") or 0) == 12
    traces = [
        json.loads(line)
        for line in (run_dir / "pareto" / "search_traces.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert len(traces) >= 1
    expected_generated = len(
        {
            t.get("candidate_content_hash")
            for t in traces
            if t.get("candidate_content_hash")
            and str(t.get("pareto_status") or "") != "realized"
        }
    )
    summary["total_cost_usd"] = 999
    summary["m5_revision_count"] = 999
    summary["generated_candidates"] = 999
    summary["restart_recovery_counts"] = 999
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    a = tmp_path / "r1"
    b = tmp_path / "r2"
    write_stage2_report([run_dir], output_dir=a, seed=42)
    write_stage2_report([run_dir], output_dir=b, seed=42)
    with (a / "stage2_main_results.csv").open(encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert float(row["total_cost_usd"]) == pytest.approx(1.44e-05)
    assert int(row["m5_revision_count"]) == 1
    assert int(row["restart_recovery_counts"]) == 0
    assert int(row["generated_candidates"]) == expected_generated
    assert int(row["generated_candidates"]) != 999
    rec = collect_run_records(run_dir)
    assert rec["summary"]["total_cost_usd"] == pytest.approx(1.44e-05)
    assert rec["summary"]["m5_revision_count"] == 1
    assert rec["summary"]["restart_recovery_counts"] == 0
    for name in sorted(p.name for p in a.iterdir()):
        assert _file_hash(a / name) == _file_hash(b / name)


@pytest.mark.asyncio
async def test_fixture_usage_and_failpoint_recovery_counts(tmp_path: Path):
    clean = await run_stage2_fixture(CFG, output_root=tmp_path, run_id="fx-clean")
    assert clean["restart_recovery_counts"] == 0
    assert len(clean["usage_ids"]) == 6
    assert any("hist-codex" in u for u in clean["usage_ids"])

    for fp, rid, expected in (
        ("after_activation_checkpoint", "fa", 1),
        ("after_future_wave_started", "fw", 1),
        ("after_realization_persisted", "fr", 0),
    ):
        with pytest.raises(RuntimeError, match=fp):
            await run_stage2_fixture(
                CFG, output_root=tmp_path, run_id=rid, failpoint=fp
            )
        summary = await run_stage2_fixture(
            CFG, output_root=tmp_path, run_id=rid, failpoint=None
        )
        assert summary["restart_recovery_counts"] == expected, fp


def _ckpt_path(run_dir: Path, task_id: str) -> Path:
    return run_dir / "tasks" / task_id / "task_execution.json"


def _ownership_scheduler_bundle(run_dir: Path, *, runtime_cap: int = 1):
    """Build a real ReadySubtaskScheduler for ownership A/B/C processes."""
    from unittest.mock import AsyncMock

    from orchestra.control.input_assembler import SubtaskInputAssembler
    from orchestra.control.pareto.runtime_factory import (
        build_slow_loop_controller,
        resolve_from_mapping,
    )
    from orchestra.control.ready_scheduler import ReadySubtaskScheduler
    from orchestra.runtime.backend import RunContext
    from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
    from orchestra.runtime.task_checkpoint import TaskCheckpointStore
    from orchestra.storage.artifacts import FileArtifactStore

    raw = yaml.safe_load(CFG.read_text(encoding="utf-8")) or {}
    resolved = resolve_from_mapping(raw, repo_root=REPO)
    resolved.candidate_catalog.serialization_groups = []
    resolved.candidate_catalog.context_budget_alternatives = {}
    resolved.candidate_catalog.graph_templates = []
    plan = stage2_fixture_plan(task_id="own_abc")
    store = FileArtifactStore(run_dir)
    ckpt = TaskCheckpointStore(run_dir)
    slow_loop = build_slow_loop_controller(
        resolved,
        run_dir=run_dir,
        contracts_dir="configs/contracts",
        repo_root=REPO,
        checkpoint_store=ckpt,
        fail_closed=True,
        runtime_concurrency_cap=runtime_cap,
    )
    slow_loop.config.budget.max_updates_per_task = 0
    runtime = AsyncMock()
    runtime.artifact_store = store
    sched = ReadySubtaskScheduler(
        runtime=runtime,
        artifact_store=store,
        task_checkpoint_store=ckpt,
        contracts_dir="configs/contracts",
        slow_loop=slow_loop,
        slow_loop_config=resolved.slow_loop_config,
        max_concurrent_subtasks=runtime_cap,
        allow_concurrent_subtasks=False,
    )
    sched.input_assembler = SubtaskInputAssembler(store)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
    )
    context = RunContext(
        run_id=run_dir.name,
        task_id=plan.task_id,
        run_dir=run_dir,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="own-abc",
    )
    return plan, store, ckpt, sched, context


async def _ownership_seed_state(run_dir: Path) -> None:
    from orchestra.control.pareto.schemas import ParetoSearchState
    from orchestra.control.slow_loop.schemas import TaskSchedulingPolicy
    from orchestra.ir.artifacts import create_artifact
    from orchestra.runtime.task_checkpoint import TaskCheckpointStore
    from orchestra.schemas.artifacts import FinalAnswerArtifact
    from orchestra.storage.artifacts import FileArtifactStore

    plan = stage2_fixture_plan(task_id="own_abc")
    store = FileArtifactStore(run_dir)
    ckpt = TaskCheckpointStore(run_dir)
    state = TaskExecutionState.from_plan(
        plan, artifact_store_ref=str(run_dir / "artifacts")
    )
    state.communication_plan = plan.communication_plan
    state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=1)
    state.pareto_state = ParetoSearchState(enabled=True)
    art = create_artifact(
        FinalAnswerArtifact(answer="seed", source_node="s1"),
        producer_node_id="s1",
        task_id=plan.task_id,
    )
    await store.put(art)
    state.subtasks["s1"].status = SubtaskStatus.COMMITTED
    state.subtasks["s1"].final_output_artifact_id = art.artifact_id
    state.subtasks["s2"].status = SubtaskStatus.READY
    state.subtasks["s3"].status = SubtaskStatus.PENDING
    state.subtasks["s4"].status = SubtaskStatus.PENDING
    state.committed_subtask_count = 1
    await ckpt.save(state)


def _proc_a_hold_live_lease(run_dir: str, ready_q, hold_q) -> None:
    import asyncio

    async def _main() -> None:
        rd = Path(run_dir)
        plan, _store, ckpt, sched, context = _ownership_scheduler_bundle(rd)
        state = await ckpt.load(
            plan.task_id,
            plan_version=plan.plan_version,
            plan_content_hash=plan.content_hash(),
            allow_config_drift=True,
        )
        assert state is not None
        signaled = {"done": False}

        async def _stub(**kwargs):  # noqa: ANN003
            sid = kwargs["subtask_id"]
            snapshot = kwargs["state"]
            # Live leased wave already persisted by run_task before stub entry.
            ckpt_file = _ckpt_path(rd, plan.task_id)
            before_hash = hashlib.sha256(ckpt_file.read_bytes()).hexdigest()
            lease_id = snapshot.subtasks[sid].lease_id
            if not signaled["done"]:
                signaled["done"] = True
                ready_q.put(
                    {
                        "pid": os.getpid(),
                        "hash": before_hash,
                        "path": str(ckpt_file),
                        "lease_id": lease_id,
                        "incarnation": snapshot.scheduler_incarnation,
                        "subtask_id": sid,
                    }
                )
                hold_q.get()  # hold until killed
                raise RuntimeError("proc-a interrupted")
            raise RuntimeError("proc-a stub should not resume")

        sched._run_subtask_isolated = _stub  # type: ignore[method-assign]
        await sched.run_task(
            plan, state, initial_artifacts=ArtifactBundle(), context=context
        )

    asyncio.run(_main())


def _proc_b_compete(run_dir: str, result_q, _expected_hash: str) -> None:
    import asyncio

    async def _main() -> None:
        rd = Path(run_dir)
        plan, _store, ckpt, sched, context = _ownership_scheduler_bundle(rd)
        state = await ckpt.load(
            plan.task_id,
            plan_version=plan.plan_version,
            plan_content_hash=plan.content_hash(),
            allow_config_drift=True,
        )
        assert state is not None
        before_ids = sorted(state.subtasks.keys())
        before_lease = state.subtasks["s2"].lease_id
        before_inc = state.scheduler_incarnation
        ckpt_file = _ckpt_path(rd, plan.task_id)
        err_name = None
        try:
            await sched.run_task(
                plan, state, initial_artifacts=ArtifactBundle(), context=context
            )
        except RunOwnershipError as exc:
            err_name = type(exc).__name__
        after = await ckpt.load(
            plan.task_id,
            plan_version=plan.plan_version,
            plan_content_hash=plan.content_hash(),
            allow_config_drift=True,
        )
        assert after is not None
        result_q.put(
            {
                "error": err_name,
                "hash": hashlib.sha256(ckpt_file.read_bytes()).hexdigest(),
                "lease_id": after.subtasks["s2"].lease_id,
                "before_lease": before_lease,
                "incarnation": after.scheduler_incarnation,
                "before_inc": before_inc,
                "ids": sorted(after.subtasks.keys()),
                "before_ids": before_ids,
            }
        )

    asyncio.run(_main())


def _proc_c_recover(run_dir: str, result_q) -> None:
    import asyncio
    import traceback

    from orchestra.control.ready_scheduler import (
        SubtaskExecutionResult,
        SubtaskExecutionStatus,
    )
    from orchestra.ir.artifacts import create_artifact
    from orchestra.schemas.artifacts import FinalAnswerArtifact

    async def _main() -> None:
        rd = Path(run_dir)
        plan, store, ckpt, sched, context = _ownership_scheduler_bundle(rd)
        state = await ckpt.load(
            plan.task_id,
            plan_version=plan.plan_version,
            plan_content_hash=plan.content_hash(),
            allow_config_drift=True,
        )
        assert state is not None
        before_inc = state.scheduler_incarnation

        async def _stub(**kwargs):  # noqa: ANN003
            sid = kwargs["subtask_id"]
            snapshot = kwargs["state"]
            art = create_artifact(
                FinalAnswerArtifact(answer=f"out-{sid}", source_node=sid),
                producer_node_id=sid,
                task_id=plan.task_id,
            )
            await store.put(art)
            live = snapshot.subtasks[sid].model_copy(deep=True)
            live.status = SubtaskStatus.AWAITING_CANONICAL_COMMIT
            live.final_output_artifact_id = art.artifact_id
            live.communication_block_reason = None
            return SubtaskExecutionResult(
                subtask_id=sid,
                expected_state_version=kwargs.get("expected_state_version", 0),
                local_subtask_state=live,
                produced_artifacts=[art],
                execution_status=SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT,
                candidate_harness_passed=True,
            )

        sched._run_subtask_isolated = _stub  # type: ignore[method-assign]
        out = await sched.run_task(
            plan, state, initial_artifacts=ArtifactBundle(), context=context
        )
        recovery_ids = [
            e.get("recovery_id")
            for e in (out.scheduler_recovery_events or [])
            if e.get("recovery_id")
        ]
        result_q.put(
            {
                "ok": True,
                "committed": sorted(
                    sid
                    for sid, sub in out.subtasks.items()
                    if sub.status is SubtaskStatus.COMMITTED
                ),
                "recovery_ids": recovery_ids,
                "incarnation": out.scheduler_incarnation,
                "before_inc": before_inc,
                "s2_lease": out.subtasks["s2"].lease_status,
            }
        )

    try:
        asyncio.run(_main())
    except Exception as exc:  # pragma: no cover - surfaced via queue
        result_q.put({"ok": False, "error": f"{exc}\n{traceback.format_exc()}"})


def test_real_scheduler_ownership_abc_processes(tmp_path: Path):
    """Process A holds a live run_task lease; B fails closed; C recovers once."""
    import asyncio

    run_dir = tmp_path / "own-abc"
    run_dir.mkdir()
    # Seed before forking; avoid an active event loop across Process.start (fork).
    asyncio.run(_ownership_seed_state(run_dir))

    # fork is fine here: seed used asyncio.run (loop closed) before Process.start.
    ctx = mp.get_context("fork")
    ready_q: mp.Queue = ctx.Queue()
    hold_q: mp.Queue = ctx.Queue()
    result_b: mp.Queue = ctx.Queue()
    result_c: mp.Queue = ctx.Queue()

    proc_a = ctx.Process(
        target=_proc_a_hold_live_lease, args=(str(run_dir), ready_q, hold_q)
    )
    proc_a.start()
    info = ready_q.get(timeout=60)
    assert info["pid"] == proc_a.pid
    assert info["lease_id"]
    ckpt = Path(info["path"])
    before_hash = info["hash"]
    assert hashlib.sha256(ckpt.read_bytes()).hexdigest() == before_hash

    proc_b = ctx.Process(
        target=_proc_b_compete, args=(str(run_dir), result_b, before_hash)
    )
    proc_b.start()
    b_info = result_b.get(timeout=60)
    proc_b.join(timeout=30)
    assert proc_b.exitcode == 0
    assert b_info["error"] == "RunOwnershipError"
    assert b_info["hash"] == before_hash
    assert b_info["lease_id"] == info["lease_id"] == b_info["before_lease"]
    assert b_info["incarnation"] == b_info["before_inc"]
    assert b_info["ids"] == b_info["before_ids"]
    assert hashlib.sha256(ckpt.read_bytes()).hexdigest() == before_hash

    # Terminate A without releasing ownership (OS drops the flock).
    proc_a.kill()
    proc_a.join(timeout=30)
    assert proc_a.exitcode is not None

    proc_c = ctx.Process(target=_proc_c_recover, args=(str(run_dir), result_c))
    proc_c.start()
    c_info = result_c.get(timeout=120)
    proc_c.join(timeout=30)
    assert c_info.get("ok") is True, c_info
    assert proc_c.exitcode == 0
    assert set(c_info["committed"]) >= {"s1", "s2", "s3", "s4"}
    assert len(c_info["recovery_ids"]) == 1
    assert c_info["incarnation"] > c_info["before_inc"]
    assert c_info["s2_lease"] != "leased"
