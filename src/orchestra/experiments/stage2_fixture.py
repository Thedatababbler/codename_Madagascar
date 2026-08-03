"""Deterministic fork/join Stage-2 fixture (no paid API).

DAG shape:
          ┌→ s2 ─┐
s1 ───────┤      ├→ s4
          └→ s3 ─┘

Starts at effective concurrency 1, adapts after s1 to concurrency 2,
then executes s2||s3 as one concurrent wave before s4.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import yaml

from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.backend_usage import BackendUsageRecord
from orchestra.control.input_assembler import SubtaskInputAssembler
from orchestra.control.pareto.runtime_factory import (
    build_slow_loop_controller,
    control_plane_manifest_fields,
    resolve_from_mapping,
)
from orchestra.control.pareto.schemas import (
    EvaluationVisibility,
    ParetoSearchState,
    PublicEvaluationRecord,
)
from orchestra.control.ready_scheduler import (
    ReadySubtaskScheduler,
    SubtaskExecutionResult,
    SubtaskExecutionStatus,
)
from orchestra.control.scheduling_effect import (
    concurrency_manifest_fields,
    effective_concurrency,
)
from orchestra.control.slow_loop.schemas import TaskSchedulingPolicy
from orchestra.control.task_state import SubtaskAttempt, SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan
from orchestra.experiments.metadata import git_commit_hash
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore

GRAPH = "configs/graphs/codex_single_implementer.yaml"

# Fixture-only duration labels (not real-model measurements).
FIXTURE_SUBTASK_DURATIONS = {
    "s1": 0.10,
    "s2": 0.20,
    "s3": 0.20,
    "s4": 0.15,
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def stage2_fixture_plan(task_id: str = "stage2_fixture") -> TaskPlan:
    return TaskPlan(
        task_id=task_id,
        plan_version=1,
        decomposition_rationale="stage2 fork/join concurrency fixture",
        subtasks=[
            SubtaskSpec(
                subtask_id="s1",
                title="s1",
                objective="seed",
                dependencies=[],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s2",
                title="s2",
                objective="fork-left",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s3",
                title="s3",
                objective="fork-right",
                dependencies=["s1"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
            SubtaskSpec(
                subtask_id="s4",
                title="s4",
                objective="join",
                dependencies=["s2", "s3"],
                keystone_harness_id="repository_test_harness",
                local_graph_template=GRAPH,
                budget=BudgetSpec(),
            ),
        ],
        communication_plan=CommunicationPlan(
            version=1,
            payload_contracts=[
                PayloadContract(
                    payload_id="p12",
                    source_subtask_id="s1",
                    target_subtask_id="s2",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=4096,
                    metadata={"slot": "comm:p12"},
                ),
                PayloadContract(
                    payload_id="p13",
                    source_subtask_id="s1",
                    target_subtask_id="s3",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=4096,
                    metadata={"slot": "comm:p13"},
                ),
                PayloadContract(
                    payload_id="p24",
                    source_subtask_id="s2",
                    target_subtask_id="s4",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=4096,
                    metadata={"slot": "comm:p24"},
                ),
                PayloadContract(
                    payload_id="p34",
                    source_subtask_id="s3",
                    target_subtask_id="s4",
                    artifact_type="FinalAnswerArtifact",
                    required=False,
                    max_tokens=4096,
                    metadata={"slot": "comm:p34"},
                ),
            ],
            delivery_schedule=[
                DeliveryRule(rule_id="r12", payload_id="p12", enabled=True),
                DeliveryRule(rule_id="r13", payload_id="p13", enabled=True),
                DeliveryRule(rule_id="r24", payload_id="p24", enabled=True),
                DeliveryRule(rule_id="r34", payload_id="p34", enabled=True),
            ],
            # Generous budgets so CONTEXT_PRESSURE does not drown out concurrency.
            context_budgets={"s2": 8000, "s3": 8000, "s4": 8000},
        ),
        metadata={
            "allowed_backend_assignments": {
                "coding": ["codex_sdk", "smolagents_code"],
                "s2": ["codex_sdk", "smolagents_code"],
                "s3": ["codex_sdk", "smolagents_code"],
                "s4": ["codex_sdk", "smolagents_code"],
            },
            "fixture_subtask_durations_seconds": dict(FIXTURE_SUBTASK_DURATIONS),
            "fixture_latency_label": "fixture_estimate_not_real_model",
        },
    )


def _runtime_cap_from_raw(raw: dict[str, Any], default: int = 2) -> int:
    runtime = raw.get("runtime") or {}
    return max(1, int(runtime.get("max_concurrent_subtasks", default)))


def _ctx(run_dir: Path, task_id: str, *, runtime_cap: int) -> RunContext:
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=2,
        max_parallel_llm_calls=2,
        max_parallel_sandboxes=2,
        max_concurrent_subtasks=runtime_cap,
    )
    return RunContext(
        run_id=task_id,
        task_id=task_id,
        run_dir=run_dir,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="stage2-fixture",
    )


async def run_stage2_fixture(
    config_path: str | Path,
    *,
    output_root: str | Path | None = None,
    run_id: str | None = None,
    failpoint: str | None = None,
    private_labels_path: str | Path | None = None,
) -> dict[str, Any]:
    repo_root = _repo_root()
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    resolved = resolve_from_mapping(raw, repo_root=repo_root)
    if not resolved.slow_loop_config.enabled:
        raise RuntimeError("stage2 fixture requires slow_loop.enabled=true")

    runtime_cap = _runtime_cap_from_raw(raw, default=2)
    started = datetime.now(UTC)
    mode = ((raw.get("stage2") or {}).get("mode")) or "fixture"
    experiment_name = ((raw.get("experiment") or {}).get("name")) or "stage2_fixture"
    root = Path(
        output_root
        or ((raw.get("stage2") or {}).get("output_root"))
        or ((raw.get("experiment") or {}).get("output_root"))
        or "outputs/stage2_pareto"
    )
    rid = run_id or f"fixture-{experiment_name}-{started.strftime('%Y%m%dT%H%M%SZ')}"
    run_dir = root / rid
    run_dir.mkdir(parents=True, exist_ok=True)

    plan = stage2_fixture_plan(task_id=f"stage2_{mode}")
    store = FileArtifactStore(run_dir)
    ckpt = TaskCheckpointStore(run_dir)

    # Resume path: if a checkpoint already exists, continue from it.
    loaded = await ckpt.load(
        plan.task_id,
        plan_version=plan.plan_version,
        plan_content_hash=plan.content_hash(),
        allow_config_drift=True,
    )
    if loaded is not None:
        state = loaded
        # Resume marker is diagnostic only; canonical recovery counts come from
        # scheduler_recovery_events with recovery_id (emitted only on reclaim).
    else:
        state = TaskExecutionState.from_plan(
            plan, artifact_store_ref=str(run_dir / "artifacts")
        )
        state.communication_plan = plan.communication_plan
        # Execute s1 via the stub so Slow Loop runs after s1 commit and before
        # the fork wave (s2||s3). Do not pre-commit s1.
        for sid in ("s1", "s2", "s3", "s4"):
            state.subtasks[sid].status = SubtaskStatus.PENDING
        state.committed_subtask_count = 0
        # Start with policy concurrency 1; runtime cap allows later increase.
        state.scheduling_policy = TaskSchedulingPolicy(max_concurrent_subtasks=1)
        if resolved.pareto_config.enabled:
            state.pareto_state = ParetoSearchState(enabled=True)
        now = datetime.now(UTC)
        # Prior public evidence only (not s1 execution); durations live in plan metadata.
        state.backend_usage_records = [
            BackendUsageRecord(
                usage_id="hist-codex",
                run_id=rid,
                task_id=plan.task_id,
                subtask_id="seed",
                node_id="n1",
                backend_id="codex_sdk",
                backend_kind="codex_sdk",
                attempt_id=1,
                started_at=now,
                finished_at=now,
                latency_seconds=FIXTURE_SUBTASK_DURATIONS["s1"],
                prompt_tokens=40,
                completion_tokens=12,
                total_tokens=52,
                estimated_cost_usd=0.03,
                cost_quality="exact",
                accounting_source="hist",
                phase="historical",
                provenance="fixture_seed_history",
                status="success",
                model_name="fake-test-model",
            ),
            BackendUsageRecord(
                usage_id="hist-smol",
                run_id=rid,
                task_id=plan.task_id,
                subtask_id="seed",
                node_id="n2",
                backend_id="smolagents_code",
                backend_kind="smolagents_code",
                attempt_id=1,
                started_at=now,
                finished_at=now,
                latency_seconds=0.08,
                prompt_tokens=20,
                completion_tokens=8,
                total_tokens=28,
                estimated_cost_usd=0.008,
                cost_quality="exact",
                accounting_source="hist",
                phase="historical",
                provenance="fixture_seed_history",
                status="success",
                model_name="fake-test-model",
            ),
        ]
        state.public_evaluation_records = [
            PublicEvaluationRecord(
                evaluation_id="pub1",
                task_id=plan.task_id,
                harness_id="repository_test_harness",
                visibility=EvaluationVisibility.PUBLIC,
                passed=True,
                normalized_score=0.92,
            ),
            PublicEvaluationRecord(
                evaluation_id="hidden1",
                task_id=plan.task_id,
                harness_id="private_harness",
                visibility=EvaluationVisibility.HIDDEN,
                passed=True,
                normalized_score=0.01,
            ),
        ]

    # Fixture focuses concurrency adaptation on the fork wave: keep catalog
    # concurrency alternatives; drop serialization / budget noise that drowns it.
    resolved.candidate_catalog.serialization_groups = []
    resolved.candidate_catalog.context_budget_alternatives = {}
    resolved.candidate_catalog.graph_templates = []
    if resolved.pareto_config.max_candidates < 16:
        resolved.pareto_config.max_candidates = 16

    slow_loop = build_slow_loop_controller(
        resolved,
        run_dir=run_dir,
        contracts_dir="configs/contracts",
        repo_root=repo_root,
        checkpoint_store=ckpt,
        fail_closed=True,
        runtime_concurrency_cap=runtime_cap,
    )
    slow_loop.config.budget.context_pressure_ratio = min(
        slow_loop.config.budget.context_pressure_ratio, 0.5
    )
    slow_loop.config.budget.min_commits_between_updates = 1
    # One adaptation after s1 is enough to prove the fork-wave concurrency change.
    slow_loop.config.budget.max_updates_per_task = min(
        slow_loop.config.budget.max_updates_per_task, 1
    )

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
        allow_concurrent_subtasks=runtime_cap > 1,
    )
    sched.input_assembler = SubtaskInputAssembler(store)

    wave_records: list[dict[str, Any]] = []
    produced: dict[str, object] = {}
    # Shared clock for deterministic overlapping lease windows.
    wave_clock = {"t": 0.0}

    async def _stub_run(**kwargs):  # noqa: ANN003
        sid = kwargs["subtask_id"]
        snapshot: TaskExecutionState = kwargs["state"]
        pol = 1
        if snapshot.scheduling_policy is not None:
            pol = int(snapshot.scheduling_policy.max_concurrent_subtasks)
        eff = effective_concurrency(
            runtime_concurrency_cap=runtime_cap, policy_concurrency=pol
        )
        lease_start = wave_clock["t"]
        duration = float(FIXTURE_SUBTASK_DURATIONS.get(sid, 0.1))
        # Concurrent wave members share the same lease_start; advance clock by
        # duration / effective so overlapping windows are observable.
        wave_clock["t"] = lease_start + (duration / max(1, eff))
        lease_end = lease_start + duration

        art = create_artifact(
            FinalAnswerArtifact(answer=f"out-{sid}", source_node=sid),
            producer_node_id=sid,
            task_id=plan.task_id,
        )
        await store.put(art)
        produced[sid] = art
        live = snapshot.subtasks[sid].model_copy(deep=True)
        live.status = SubtaskStatus.AWAITING_CANONICAL_COMMIT
        live.final_output_artifact_id = art.artifact_id
        live.communication_block_reason = None
        started_at = datetime.now(UTC)
        # Stable usage identity: lease-scoped so resume re-execution after reclaim
        # gets a new id, while exact-once merge still deduplicates identical ids.
        lease_token = live.lease_id or (
            f"inc{snapshot.scheduler_incarnation}-a{len(live.attempts) + 1}"
        )
        usage = BackendUsageRecord(
            usage_id=f"wave-{sid}-{lease_token}",
            run_id=rid,
            task_id=plan.task_id,
            subtask_id=sid,
            node_id=f"n-{sid}",
            backend_id="codex_sdk",
            backend_kind="codex_sdk",
            attempt_id=max(1, len(live.attempts) + 1),
            started_at=started_at,
            finished_at=started_at,
            latency_seconds=duration,
            prompt_tokens=8,
            completion_tokens=4,
            total_tokens=12,
            estimated_cost_usd=0.01,
            cost_quality="exact",
            accounting_source="wave",
            phase=(
                "post_activation"
                if snapshot.active_plan_revision_id
                else "pre_activation"
            ),
            plan_revision=snapshot.active_plan_revision_id,
            wave_id=snapshot.current_wave_id,
            scheduler_incarnation=snapshot.scheduler_incarnation,
            provenance="fixture_wave_stub",
            status="success",
            model_name="fake-test-model",
        )
        # Must return via backend_usage_append so the coordinator merges into
        # the canonical checkpoint (snapshot mutations alone are discarded).
        live.attempts.append(
            SubtaskAttempt(
                attempt_id=max(1, len(live.attempts) + 1),
                status=SubtaskStatus.AWAITING_CANONICAL_COMMIT,
                started_at=started_at,
                finished_at=started_at,
                lease_id=live.lease_id,
                wave_id=snapshot.current_wave_id,
                execution_plan_revision=snapshot.active_plan_revision_id,
                scheduler_incarnation=snapshot.scheduler_incarnation,
                usage_ids=[usage.usage_id],
            )
        )
        wave_records.append(
            {
                "subtask_id": sid,
                "lease_start": lease_start,
                "lease_end": lease_end,
                "duration_fixture_s": duration,
                "policy_concurrency": pol,
                "runtime_concurrency_cap": runtime_cap,
                "effective_concurrency": eff,
                "active_plan_revision_id": snapshot.active_plan_revision_id,
                "wave_id": snapshot.current_wave_id,
                "state_version": snapshot.state_version,
            }
        )
        if failpoint == "after_future_wave_started" and sid in {"s2", "s3"}:
            # Crash after the wave is leased. Do not retain usage for incomplete
            # work — resume reclaims the lease and records usage on completion.
            snap_sub = snapshot.subtasks[sid]
            snap_sub.attempts = list(live.attempts)
            await ckpt.save(snapshot)
            raise RuntimeError("FAILPOINT:after_future_wave_started")
        return SubtaskExecutionResult(
            subtask_id=sid,
            expected_state_version=kwargs.get("expected_state_version", 0),
            local_subtask_state=live,
            produced_artifacts=[art],
            execution_status=SubtaskExecutionStatus.SUCCESS_PENDING_COMMIT,
            candidate_harness_passed=True,
            backend_usage_append=[usage],
        )

    sched._run_subtask_isolated = _stub_run  # type: ignore[method-assign]

    # Hook activation checkpoint failpoint via Slow Loop save.
    if failpoint == "after_activation_checkpoint":
        original_maybe = slow_loop.maybe_update

        async def _wrapped_maybe(**kw):  # noqa: ANN003
            result = await original_maybe(**kw)
            if getattr(result, "updated", False):
                await ckpt.save(kw["state"])
                raise RuntimeError("FAILPOINT:after_activation_checkpoint")
            return result

        slow_loop.maybe_update = _wrapped_maybe  # type: ignore[method-assign]

    initial_conc = concurrency_manifest_fields(
        runtime_concurrency_cap=runtime_cap,
        policy=state.scheduling_policy,
    )
    manifest = {
        "runner": "stage2_fixture",
        "started_at": started.isoformat(),
        "git_commit": git_commit_hash(repo_root),
        "config_path": str(config_path),
        "split": "fixture",
        "stage2": raw.get("stage2") or {"mode": mode},
        "preference_profile_id": resolved.preference_profile.profile_id,
        "backend_override": "deterministic_mock",
        "mock_backends": True,
        "fixture_latency_label": "fixture_estimate_not_real_model",
        "failpoint": failpoint,
        **control_plane_manifest_fields(
            resolved,
            runtime_concurrency_cap=runtime_cap,
            policy_concurrency=initial_conc["policy_concurrency"],
        ),
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    try:
        out = await sched.run_task(
            plan,
            state,
            initial_artifacts=ArtifactBundle(),
            context=_ctx(run_dir, plan.task_id, runtime_cap=runtime_cap),
        )
    except RuntimeError as exc:
        if failpoint and str(exc).startswith("FAILPOINT:"):
            (run_dir / "failpoint.json").write_text(
                json.dumps(
                    {
                        "failpoint": failpoint,
                        "error": str(exc),
                        "wave_records": wave_records,
                        "recovery_events": list(
                            getattr(state, "scheduler_recovery_events", None) or []
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            raise
        raise

    # Finalize any pending realized decision after future commits.
    if (
        out.pareto_state is not None
        and out.pareto_state.pending_decision is not None
        and hasattr(slow_loop.candidate_policy, "finalize_realized")
    ):
        before_final = out.pareto_state.pending_decision.decision_id
        slow_loop.candidate_policy.finalize_realized(out)
        await ckpt.save(out)
        if failpoint == "after_realization_persisted":
            (run_dir / "failpoint.json").write_text(
                json.dumps(
                    {
                        "failpoint": failpoint,
                        "realization_decision_id": before_final,
                        "wave_records": wave_records,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            raise RuntimeError("FAILPOINT:after_realization_persisted")
    decisions = 0
    selected_hash = None
    pending = False
    activation_revision = None
    if out.pareto_state is not None:
        decisions = len(out.pareto_state.decision_history)
        pending = out.pareto_state.pending_decision is not None
        if out.pareto_state.decision_history:
            last = out.pareto_state.decision_history[-1]
            selected_hash = last.selected_content_hash
            activation_revision = last.activated_revision_id
        elif out.pareto_state.pending_decision is not None:
            selected_hash = out.pareto_state.pending_decision.selected_content_hash
            activation_revision = out.pareto_state.pending_decision.activated_revision_id
            decisions = max(decisions, 1)

    if failpoint == "after_realization_persisted" and not (
        out.pareto_state is not None and out.pareto_state.pending_decision is not None
    ):
        # Realization may already have been finalized inside the last Slow Loop
        # tick; still inject the crash after persistence for resume tests.
        (run_dir / "failpoint.json").write_text(
            json.dumps(
                {
                    "failpoint": failpoint,
                    "realization_decision_id": selected_hash,
                    "wave_records": wave_records,
                    "note": "already_finalized_in_scheduler",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        raise RuntimeError("FAILPOINT:after_realization_persisted")

    final_pol = 1
    if out.scheduling_policy is not None:
        final_pol = int(out.scheduling_policy.max_concurrent_subtasks)
    final_eff = effective_concurrency(
        runtime_concurrency_cap=runtime_cap, policy_concurrency=final_pol
    )

    # Detect concurrent fork wave: s2 and s3 overlapping lease windows under eff>=2.
    # Use wave-time effective concurrency (not the final post-join policy).
    fork_wave = [w for w in wave_records if w["subtask_id"] in {"s2", "s3"}]
    concurrent_fork = False
    fork_eff = 1
    activation_for_fork = None
    if len(fork_wave) >= 2:
        s2 = next((w for w in fork_wave if w["subtask_id"] == "s2"), None)
        s3 = next((w for w in fork_wave if w["subtask_id"] == "s3"), None)
        if s2 and s3:
            fork_eff = min(
                int(s2["effective_concurrency"]), int(s3["effective_concurrency"])
            )
            activation_for_fork = s2.get("active_plan_revision_id")
            concurrent_fork = (
                s2["lease_start"] < s3["lease_end"]
                and s3["lease_start"] < s2["lease_end"]
                and fork_eff >= 2
                and s2.get("active_plan_revision_id") == s3.get("active_plan_revision_id")
                and bool(s2.get("active_plan_revision_id"))
            )

    usage_cost = sum(
        float(r.estimated_cost_usd or 0.0)
        for r in out.backend_usage_records
        if r.estimated_cost_usd is not None
    )
    # Exclude oracle/hidden from online aggregates — cost from persisted usage only.
    wall = sum(float(FIXTURE_SUBTASK_DURATIONS[s]) for s in ("s1", "s2", "s3", "s4"))
    critical = (
        FIXTURE_SUBTASK_DURATIONS["s1"]
        + max(FIXTURE_SUBTASK_DURATIONS["s2"], FIXTURE_SUBTASK_DURATIONS["s3"])
        + FIXTURE_SUBTASK_DURATIONS["s4"]
        if concurrent_fork
        else wall
    )

    evidence = {
        "active_plan_revision_id": out.active_plan_revision_id,
        "activation_revision": activation_revision,
        "fork_activation_revision": activation_for_fork,
        "selected_candidate_hash": selected_hash,
        "runtime_concurrency_cap": runtime_cap,
        "policy_concurrency": final_pol,
        "effective_concurrency": final_eff,
        "fork_wave_effective_concurrency": fork_eff,
        "wave_records": wave_records,
        "concurrent_fork_wave": concurrent_fork,
        "recovery_events": list(out.scheduler_recovery_events or []),
        "scheduler_recovery_events": list(out.scheduler_recovery_events or []),
        "scheduler_wave_records": [
            w.model_dump(mode="json") if hasattr(w, "model_dump") else w
            for w in (out.scheduler_wave_records or [])
        ],
        "fixture_subtask_durations_seconds": FIXTURE_SUBTASK_DURATIONS,
        "fixture_latency_label": "fixture_estimate_not_real_model",
    }
    (run_dir / "concurrency_evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    recovery_ids = {
        e.get("recovery_id")
        for e in (out.scheduler_recovery_events or [])
        if isinstance(e, dict) and e.get("recovery_id")
    }
    if out.scheduler_recovery_events:
        (run_dir / "recovery_events.json").write_text(
            json.dumps(out.scheduler_recovery_events, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # Persist updated concurrency fields into manifest.
    manifest.update(
        concurrency_manifest_fields(
            runtime_concurrency_cap=runtime_cap,
            policy=out.scheduling_policy,
        )
    )
    manifest["concurrent_fork_wave"] = concurrent_fork
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    committed = [
        sid for sid, sub in out.subtasks.items() if sub.status is SubtaskStatus.COMMITTED
    ]
    # Task-level solving: one root benchmark task, not committed-subtask count.
    root_task_solved = set(committed) >= {"s1", "s2", "s3", "s4"}
    solved_task_count = 1 if root_task_solved else 0
    cost_per_solved_task = (
        usage_cost / solved_task_count if solved_task_count > 0 else None
    )
    summary = {
        "mode": mode,
        "run_dir": str(run_dir),
        "task_id": plan.task_id,
        "split": "fixture",
        "pareto_enabled": resolved.pareto_config.enabled,
        "preference_profile": resolved.preference_profile.profile_id,
        "committed": committed,
        "committed_subtask_count": len(committed),
        "solved_task_count": solved_task_count,
        "active_plan_revision_id": out.active_plan_revision_id,
        "activation_revision": activation_revision,
        "m5_revision_count": len(
            [
                r
                for r in out.plan_revision_history
                if str(r.status).endswith("APPLIED")
                or getattr(getattr(r, "status", None), "value", "") == "applied"
            ]
        ),
        "decisions": decisions,
        "pending_decision": pending,
        "selected_hash": selected_hash,
        "runtime_concurrency_cap": runtime_cap,
        "policy_concurrency": final_pol,
        "effective_concurrency": final_eff,
        "fork_wave_effective_concurrency": fork_eff,
        "fork_activation_revision": activation_for_fork,
        "concurrent_fork_wave": concurrent_fork,
        "execution_success_rate": (
            1.0 if root_task_solved else 0.0
        ),
        "avg_cost_usd": usage_cost / max(1, len(out.backend_usage_records)),
        "total_cost_usd": usage_cost,
        "cost_per_solved": cost_per_solved_task,
        "cost_per_solved_task": cost_per_solved_task,
        "wall_latency_s": wall,
        "critical_path_latency_s": critical,
        "communication_overhead": None,
        "difficulty": "fixture",
        "hidden_pass_at_1": "",
        "restart_recovery_counts": len(recovery_ids),
        "recovery_ids": sorted(recovery_ids),
        "control_plane_hash": resolved.control_plane_hash,
        "scheduler_path": "ReadySubtaskScheduler",
        "scheduler_incarnation": out.scheduler_incarnation,
        "fixture_latency_label": "fixture_estimate_not_real_model",
        "cost_provenance": "persisted_usage_estimated_cost_usd",
        "public_evaluation_count": len(out.public_evaluation_records or []),
        "usage_record_count": len(out.backend_usage_records or []),
        "usage_ids": sorted(
            {
                getattr(u, "usage_id", None) or u.get("usage_id")
                for u in (out.backend_usage_records or [])
                if getattr(u, "usage_id", None) or (isinstance(u, dict) and u.get("usage_id"))
            }
        ),
        "private_labels_path": str(private_labels_path) if private_labels_path else None,
        "offline_private_score": None,
    }
    # Offline private-label read happens only after online control completes.
    if private_labels_path is not None:
        from orchestra.experiments.private_labels import (
            load_private_labels,
            project_offline_private_score,
        )

        labels = load_private_labels(
            private_labels_path,
            purpose="offline_post_execution_report",
            allow_online=False,
        )
        summary["offline_private_score"] = project_offline_private_score(
            labels, task_id="stage2-fixture"
        )
        summary["private_label_artifact_id"] = labels.get("artifact_id")
    (run_dir / "stage2_fixture_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary
