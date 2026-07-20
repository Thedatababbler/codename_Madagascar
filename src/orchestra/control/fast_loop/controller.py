"""Backend-agnostic Fast Loop controller (M4-A: FRESH candidates only)."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from pathlib import Path

from orchestra.backends.base import ArtifactRef
from orchestra.backends.capabilities import BackendCapabilities
from orchestra.backends.catalog import capabilities_for
from orchestra.cli.validate_graph import build_compiler
from orchestra.control.failure import classify_subtask_outcome
from orchestra.control.fast_loop.budget import FastLoopBudgetTracker, spent_from_state
from orchestra.control.fast_loop.candidate_generator import (
    RuleBasedLocalCandidateGenerator,
)
from orchestra.control.fast_loop.capability import validate_candidate_against_capabilities
from orchestra.control.fast_loop.diagnosis import diagnose_subtask_failure
from orchestra.control.fast_loop.schemas import (
    BackendModelPool,
    CandidateRecord,
    CandidateRejectionReason,
    CandidateStatus,
    CostRecord,
    FastLoopBudget,
    FastLoopState,
    LocalCandidate,
    StabilityIncident,
)
from orchestra.control.fast_loop.selector import DeterministicCandidateSelector
from orchestra.control.fast_loop.workspace import (
    CandidateWorkspaceError,
    GitCandidateWorkspaceManager,
)
from orchestra.control.task_state import (
    BackendSessionRecord,
    LocalUpdateRecord,
    SubtaskFailureReason,
    SubtaskStatus,
    TaskExecutionState,
)
from orchestra.harness.command_runner import run_authoritative_harness_command
from orchestra.ir.artifacts import ArtifactBundle
from orchestra.ir.graph import OrchestraGraph, load_graph
from orchestra.ir.nodes import NodeKind
from orchestra.runtime.backend import RunContext
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.runtime.state import GraphExecutionResult
from orchestra.runtime.task_checkpoint import TaskCheckpointStore
from orchestra.storage.artifacts import ArtifactStore
from orchestra.workspaces.base import WorkspaceRef

logger = logging.getLogger(__name__)


class FastLoopController:
    """Diagnose → generate ≤K candidates → isolate → evaluate → select → commit."""

    def __init__(
        self,
        *,
        runtime: NativeAsyncRuntime,
        artifact_store: ArtifactStore,
        task_checkpoint_store: TaskCheckpointStore,
        contracts_dir: str = "configs/contracts",
        workspace_manager: GitCandidateWorkspaceManager | None = None,
        generator: RuleBasedLocalCandidateGenerator | None = None,
        selector: DeterministicCandidateSelector | None = None,
        budget: FastLoopBudget | None = None,
        capabilities: Mapping[str, BackendCapabilities] | None = None,
        model_pools: Mapping[str, BackendModelPool] | None = None,
        clock=None,
        persist_checkpoints: bool = True,
    ) -> None:
        self.runtime = runtime
        self.artifact_store = artifact_store
        self.task_checkpoint_store = task_checkpoint_store
        self.contracts_dir = contracts_dir
        self.workspace_manager = workspace_manager or GitCandidateWorkspaceManager()
        self.compiler = build_compiler(contracts_dir)
        self.generator = generator or RuleBasedLocalCandidateGenerator(
            compiler=self.compiler,
            model_pools=model_pools,
        )
        self.selector = selector or DeterministicCandidateSelector()
        self.budget = budget or FastLoopBudget()
        self.budget_tracker = FastLoopBudgetTracker(self.budget, clock=clock)
        self.capabilities = dict(capabilities or {})
        # When False, scheduler coordinator owns shared task checkpoints.
        self.persist_checkpoints = persist_checkpoints

    async def _save_checkpoint(self, state: TaskExecutionState) -> None:
        if self.persist_checkpoints:
            await self.task_checkpoint_store.save(state)

    def _caps_for_graph(self, graph: OrchestraGraph) -> dict[str, BackendCapabilities]:
        caps = dict(self.capabilities)
        for node in graph.nodes:
            if getattr(node, "node_kind", None) and node.node_kind.value == "agent":
                backend_id = str(node.resolved_backend().type)
                if backend_id not in caps:
                    known = capabilities_for(backend_id)
                    if known is not None:
                        caps[backend_id] = known
        return caps

    def _harness_command(self, graph: OrchestraGraph) -> tuple[list[str], float]:
        for node in graph.nodes:
            if node.node_kind is NodeKind.HARNESS:
                command = list(getattr(node, "command", None) or ["python", "-m", "pytest", "-q"])
                timeout = float(getattr(node, "timeout_seconds", None) or 60.0)
                return command, timeout
        return ["python", "-m", "pytest", "-q"], 60.0

    def _record_from_local(
        self,
        cand: LocalCandidate,
        *,
        attempt_id: int,
    ) -> CandidateRecord:
        status = (
            CandidateStatus.REJECTED
            if cand.compatibility_rejected
            else CandidateStatus.PENDING
        )
        return CandidateRecord(
            candidate_id=cand.candidate_id,
            attempt_id=attempt_id,
            graph_hash=cand.graph.content_hash,
            parent_graph_hash=cand.parent_graph_hash,
            edits=list(cand.edits),
            status=status,
            session_policy=cand.session_policy,
            rejection_reason=cand.rejection_reason,
            rejection_message=cand.rejection_message,
            failure_message=cand.rejection_message,
            metadata={
                "generation_reason": cand.generation_reason,
                "graph": cand.graph.model_dump(mode="json"),
            },
        )

    async def run(
        self,
        *,
        state: TaskExecutionState,
        subtask_id: str,
        context: RunContext,
        initial_artifacts: ArtifactBundle,
        source_repo: str | None = None,
        graph: OrchestraGraph | None = None,
        graph_result: GraphExecutionResult | None = None,
        initial_execution_cost: CostRecord | None = None,
        base_workspace: WorkspaceRef | None = None,
    ) -> TaskExecutionState:
        sub = state.subtasks[subtask_id]
        if sub.status is SubtaskStatus.COMMITTED:
            return state

        base_graph = graph or load_graph(sub.spec.local_graph_template)
        diagnosis = diagnose_subtask_failure(
            subtask_state=sub,
            graph=base_graph,
            graph_result=graph_result,
        )

        fl_state = state.fast_loop_states.get(subtask_id)
        if fl_state is None:
            fl_state = FastLoopState(
                subtask_id=subtask_id,
                base_attempt_id=len(sub.attempts),
                base_graph_hash=base_graph.content_hash,
                diagnosis=diagnosis,
                initial_execution_cost=initial_execution_cost or CostRecord(),
            )
            state.fast_loop_states[subtask_id] = fl_state
        else:
            for cand in fl_state.candidates:
                if cand.status is CandidateStatus.RUNNING:
                    cand.status = CandidateStatus.PENDING
                    cand.failure_message = "recovered from interrupted candidate run"
            if fl_state.selected_candidate_id:
                winner = next(
                    (
                        c
                        for c in fl_state.candidates
                        if c.candidate_id == fl_state.selected_candidate_id
                    ),
                    None,
                )
                if winner and winner.status is CandidateStatus.COMMITTED:
                    return state

        self.budget_tracker.mark_started(fl_state)

        if diagnosis.infrastructure_related:
            await self._infra_retry_once(
                state=state,
                fl_state=fl_state,
                subtask_id=subtask_id,
                base_graph=base_graph,
                context=context,
                initial_artifacts=initial_artifacts,
                source_repo=source_repo,
                base_workspace=base_workspace,
            )
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        if not diagnosis.retryable:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        if not fl_state.candidates:
            ok, reason, code = self.budget_tracker.can_generate_candidate(fl_state)
            if not ok:
                fl_state.exhausted = True
                sub.status = SubtaskStatus.FAILED
                sub.failure_message = reason
                state.state_version += 1
                await self._save_checkpoint(state)
                return state
            caps = self._caps_for_graph(base_graph)
            generated = self.generator.generate(
                graph=base_graph,
                diagnosis=diagnosis,
                budget=self.budget,
                capabilities=caps,
            )
            for cand in generated:
                fl_state.candidates.append(
                    self._record_from_local(
                        cand, attempt_id=fl_state.base_attempt_id + 1
                    )
                )
            state.state_version += 1
            await self._save_checkpoint(state)

        if not fl_state.candidates:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        base_ws = base_workspace
        repo = source_repo
        if base_ws is None and repo:
            base_ws = await self.workspace_manager.prepare_base_snapshot(
                source_repo=repo,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
            )
            sub.workspace_ref = base_ws.path

        for record in fl_state.candidates:
            if record.status is not CandidateStatus.PENDING:
                continue

            ok, reason, code = self.budget_tracker.can_start_candidate(fl_state)
            if not ok:
                fl_state.exhausted = True
                record.status = CandidateStatus.REJECTED
                record.rejection_reason = code or CandidateRejectionReason.BUDGET_EXCEEDED
                record.rejection_message = reason
                record.failure_message = reason
                # Reject remaining pending without launching.
                for other in fl_state.candidates:
                    if other.status is CandidateStatus.PENDING and other is not record:
                        other.status = CandidateStatus.REJECTED
                        other.rejection_reason = CandidateRejectionReason.BUDGET_EXCEEDED
                        other.rejection_message = reason
                state.state_version += 1
                await self._save_checkpoint(state)
                break

            graph_payload = record.metadata.get("graph")
            if not graph_payload:
                record.status = CandidateStatus.REJECTED
                record.rejection_reason = CandidateRejectionReason.MISSING_GRAPH
                record.rejection_message = "missing candidate graph payload"
                continue
            candidate_graph = OrchestraGraph.model_validate(graph_payload)
            local_candidate = LocalCandidate(
                candidate_id=record.candidate_id,
                parent_graph_hash=record.parent_graph_hash,
                edits=list(record.edits),
                graph=candidate_graph,
                session_policy=record.session_policy,
                generation_reason=str(record.metadata.get("generation_reason") or ""),
            )
            caps = self._caps_for_graph(candidate_graph)
            compat = validate_candidate_against_capabilities(local_candidate, caps)
            if not compat.compatible:
                record.status = CandidateStatus.REJECTED
                record.rejection_reason = (
                    compat.rejection_reason or CandidateRejectionReason.OTHER
                )
                record.rejection_message = compat.reason
                record.failure_message = compat.reason
                state.state_version += 1
                await self._save_checkpoint(state)
                continue

            await self._evaluate_candidate(
                state=state,
                fl_state=fl_state,
                record=record,
                candidate_graph=candidate_graph,
                context=context,
                initial_artifacts=initial_artifacts,
                base_ws=base_ws,
            )
            state.state_version += 1
            await self._save_checkpoint(state)

        winner = self.selector.select(fl_state.candidates, self.budget)
        if winner is None:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = diagnosis.reason
            sub.failure_message = (
                diagnosis.concise_feedback or "fast loop exhausted without valid winner"
            )
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        ok, reason, code = self.budget_tracker.can_start_candidate(
            fl_state, reserved_backend_calls=0
        )
        # Wall-time still checked before commit.
        if fl_state.started_monotonic is not None:
            ok2, reason2, code2 = self.budget_tracker.can_start_candidate(fl_state)
            if not ok2 and "wall_time" in (reason2 or ""):
                fl_state.exhausted = True
                winner.status = CandidateStatus.REJECTED
                winner.rejection_reason = code2
                winner.rejection_message = reason2
                sub.status = SubtaskStatus.FAILED
                sub.failure_message = reason2
                state.state_version += 1
                await self._save_checkpoint(state)
                return state

        if base_ws is None:
            await self._commit_non_repo_winner(state, fl_state, winner, subtask_id)
            state.state_version += 1
            await self._save_checkpoint(state)
            return state

        await self._commit_winner(
            state=state,
            fl_state=fl_state,
            winner=winner,
            base_ws=base_ws,
            subtask_id=subtask_id,
            base_graph=base_graph,
        )
        state.state_version += 1
        await self._save_checkpoint(state)
        return state

    async def _infra_retry_once(
        self,
        *,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        subtask_id: str,
        base_graph: OrchestraGraph,
        context: RunContext,
        initial_artifacts: ArtifactBundle,
        source_repo: str | None,
        base_workspace: WorkspaceRef | None,
    ) -> None:
        sub = state.subtasks[subtask_id]
        if fl_state.infra_retries_used >= 1:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            return
        ok, reason, code = self.budget_tracker.can_start_candidate(fl_state)
        if not ok:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            sub.failure_message = reason
            return
        fl_state.infra_retries_used += 1
        fl_state.control_plane_cost = CostRecord(
            backend_calls=fl_state.control_plane_cost.backend_calls
        )
        infra_record = CandidateRecord(
            candidate_id="infra_retry_0",
            attempt_id=fl_state.base_attempt_id + 1,
            graph_hash=base_graph.content_hash,
            parent_graph_hash=base_graph.content_hash,
            edits=[],
            status=CandidateStatus.RUNNING,
            failure_reason=SubtaskFailureReason.INFRA,
            stability_incidents=[
                StabilityIncident(
                    kind="infrastructure",
                    message=sub.failure_message or "infra failure",
                )
            ],
            metadata={"infrastructure_related": True, "no_graph_edit": True},
        )
        fl_state.candidates.append(infra_record)

        workspace_ref = (
            (base_workspace.path if base_workspace else None)
            or sub.workspace_ref
            or context.workspace_ref
        )
        if source_repo and not workspace_ref:
            base = await self.workspace_manager.prepare_base_snapshot(
                source_repo=source_repo,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
            )
            workspace_ref = base.path
            sub.workspace_ref = workspace_ref
        run_context = RunContext(
            run_id=f"{context.run_id}:infra_retry",
            task_id=f"{context.task_id}__infra_retry",
            run_dir=context.run_dir,
            limits=context.limits,
            semaphores=context.semaphores,
            contract_hash=context.contract_hash,
            allow_config_drift=False,
            subtask_id=subtask_id,
            workspace_ref=workspace_ref,
        )
        compiled = self.compiler.compile(base_graph)
        try:
            result = await self.runtime.execute(
                graph=compiled,
                initial_artifacts=initial_artifacts,
                context=run_context,
            )
        except Exception as exc:  # noqa: BLE001
            infra_record.status = CandidateStatus.BACKEND_FAILED
            infra_record.cost = CostRecord(backend_calls=1)
            infra_record.failure_message = f"{type(exc).__name__}: {exc}"
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.INFRA
            sub.failure_message = infra_record.failure_message
            fl_state.exhausted = True
            return
        infra_record.cost = _cost_from_result(result)
        status, reason, message = await classify_subtask_outcome(
            result=result,
            artifact_store=self.artifact_store,
            error=None,
        )
        if status is SubtaskStatus.COMMITTED:
            sub.status = SubtaskStatus.COMMITTED
            sub.failure_reason = None
            sub.failure_message = None
            final_id = result.state.final_output_artifact_id
            sub.final_output_artifact_id = final_id
            state.frozen = all(
                s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
            )
            fl_state.selected_candidate_id = "infra_retry_0"
            fl_state.selected_execution_cost = infra_record.cost
            infra_record.status = CandidateStatus.COMMITTED
            state.mark_ready_from_dependencies()
        else:
            infra_record.status = CandidateStatus.BACKEND_FAILED
            infra_record.failure_message = message
            sub.status = status
            sub.failure_reason = reason
            sub.failure_message = message
            fl_state.exhausted = True

    async def _evaluate_candidate(
        self,
        *,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        record: CandidateRecord,
        candidate_graph: OrchestraGraph,
        context: RunContext,
        initial_artifacts: ArtifactBundle,
        base_ws: WorkspaceRef | None,
    ) -> None:
        subtask_id = fl_state.subtask_id
        record.status = CandidateStatus.RUNNING
        started = time.perf_counter()
        cand_ws: WorkspaceRef | None = None
        if base_ws is not None:
            cand_ws = await self.workspace_manager.fork_candidate_workspace(
                base=base_ws,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
                candidate_id=record.candidate_id,
            )
            record.workspace_ref = cand_ws

        run_context = RunContext(
            run_id=f"{context.run_id}:{record.candidate_id}",
            task_id=f"{context.task_id}__candidate__{record.candidate_id}",
            run_dir=context.run_dir,
            limits=context.limits,
            semaphores=context.semaphores,
            contract_hash=context.contract_hash,
            allow_config_drift=False,
            subtask_id=subtask_id,
            workspace_ref=cand_ws.path if cand_ws else context.workspace_ref,
        )
        compiled = self.compiler.compile(candidate_graph)
        try:
            result = await self.runtime.execute(
                graph=compiled,
                initial_artifacts=initial_artifacts,
                context=run_context,
            )
        except Exception as exc:  # noqa: BLE001
            from datetime import UTC, datetime

            from orchestra.control.backend_usage import (
                append_usage_records,
                exception_usage_record,
            )

            record.status = CandidateStatus.BACKEND_FAILED
            record.failure_reason = SubtaskFailureReason.INFRA
            record.failure_message = f"{type(exc).__name__}: {exc}"
            record.latency_ms = int((time.perf_counter() - started) * 1000)
            record.cost = CostRecord(backend_calls=1)
            record.stability_incidents.append(
                StabilityIncident(kind="exception", message=record.failure_message)
            )
            finished_at = datetime.now(UTC)
            started_at = datetime.fromtimestamp(
                finished_at.timestamp() - (record.latency_ms or 0) / 1000.0,
                tz=UTC,
            )
            state.backend_usage_records = append_usage_records(
                list(state.backend_usage_records or []),
                [
                    exception_usage_record(
                        task_id=state.task_id,
                        subtask_id=subtask_id,
                        attempt_id=record.attempt_id,
                        candidate_id=record.candidate_id,
                        started_at=started_at,
                        finished_at=finished_at,
                        status="exception",
                        accounting_source="fast_loop_candidate_exception",
                    )
                ],
            )
            return

        record.latency_ms = int((time.perf_counter() - started) * 1000)
        record.backend_sessions = _collect_sessions(
            result=result,
            attempt_id=record.attempt_id,
            candidate_id=record.candidate_id,
        )
        from datetime import UTC, datetime

        from orchestra.control.backend_usage import (
            append_usage_records,
            collect_usage_from_graph_result,
        )

        finished_at = datetime.now(UTC)
        started_at = datetime.fromtimestamp(
            finished_at.timestamp() - (record.latency_ms or 0) / 1000.0,
            tz=UTC,
        )
        state.backend_usage_records = append_usage_records(
            list(state.backend_usage_records or []),
            collect_usage_from_graph_result(
                task_id=state.task_id,
                subtask_id=subtask_id,
                attempt_id=record.attempt_id,
                result=result,
                candidate_id=record.candidate_id,
                started_at=started_at,
                finished_at=finished_at,
                status=str(getattr(record.status, "value", record.status)),
                accounting_source="fast_loop_candidate",
            ),
        )
        # Single source of truth: CandidateRecord.cost only (search_cost is derived).
        record.cost = _cost_from_result(result)

        if cand_ws is not None:
            change_set = await self.workspace_manager.collect_changeset(cand_ws)
            record.change_set = change_set
            patch, changed, digest = await self.workspace_manager.collect_patch(cand_ws)
            record.patch = patch
            record.changed_files = changed
            record.patch_hash = digest or None

        status, reason, message = await classify_subtask_outcome(
            result=result,
            artifact_store=self.artifact_store,
            error=None,
        )
        if status is SubtaskStatus.COMMITTED:
            record.status = CandidateStatus.VALID
            record.quality_score = 1.0
            record.failure_reason = None
            record.failure_message = None
            if result.state.final_output_artifact_id:
                record.output_artifact_ids = [result.state.final_output_artifact_id]
        elif status is SubtaskStatus.HARNESS_FAILED:
            record.status = CandidateStatus.HARNESS_FAILED
            record.quality_score = 0.0
            record.failure_reason = reason
            record.failure_message = message
        else:
            record.status = CandidateStatus.BACKEND_FAILED
            record.quality_score = 0.0
            record.failure_reason = reason
            record.failure_message = message

        if cand_ws is not None:
            summary_path = Path(cand_ws.path).parent / "candidate_result.json"
            summary_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")

    async def _commit_winner(
        self,
        *,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        winner: CandidateRecord,
        base_ws: WorkspaceRef,
        subtask_id: str,
        base_graph: OrchestraGraph,
    ) -> None:
        if winner.status is CandidateStatus.COMMITTED:
            return
        if winner.workspace_ref is None:
            raise CandidateWorkspaceError("winner missing workspace_ref")
        if winner.status is not CandidateStatus.VALID:
            raise CandidateWorkspaceError("refusing to commit non-valid winner")

        expected_rev = base_ws.base_revision
        change_set = winner.change_set or await self.workspace_manager.collect_changeset(
            winner.workspace_ref
        )

        # PREPARE: apply without finalize, then re-run authoritative harness.
        try:
            await self.workspace_manager.apply_changeset(
                base=base_ws,
                winner=winner.workspace_ref,
                change_set=change_set,
                expected_base_revision=expected_rev,
            )
        except CandidateWorkspaceError as exc:
            winner.status = CandidateStatus.COMMIT_VALIDATION_FAILED
            winner.failure_message = str(exc)
            sub = state.subtasks[subtask_id]
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.INFRA
            sub.failure_message = str(exc)
            fl_state.exhausted = True
            return

        command, timeout = self._harness_command(base_graph)
        passed, code, stdout, stderr = await run_authoritative_harness_command(
            cwd=base_ws.path,
            command=command,
            timeout_seconds=timeout,
        )
        if not passed:
            if expected_rev:
                await self.workspace_manager.rollback_to_revision(base_ws, expected_rev)
            winner.status = CandidateStatus.COMMIT_VALIDATION_FAILED
            winner.failure_message = (
                f"post-apply canonical harness failed exit={code}: "
                f"{(stderr or stdout)[-500:]}"
            )
            sub = state.subtasks[subtask_id]
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.HARNESS
            sub.failure_message = winner.failure_message
            fl_state.exhausted = True
            return

        # COMMIT: finalize git commit on canonical/base.
        new_rev = await self.workspace_manager.finalize_git_commit(
            base_ws, f"commit winner from {winner.candidate_id}"
        )
        committed = WorkspaceRef(
            workspace_id=base_ws.workspace_id,
            path=base_ws.path,
            kind=base_ws.kind,
            task_id=base_ws.task_id,
            subtask_id=base_ws.subtask_id,
            base_revision=new_rev,
        )

        sub = state.subtasks[subtask_id]
        sub.status = SubtaskStatus.COMMITTED
        sub.failure_reason = None
        sub.failure_message = None
        sub.workspace_ref = committed.path
        sub.current_graph_hash = winner.graph_hash
        sub.local_revision += 1
        if winner.output_artifact_ids:
            sub.final_output_artifact_id = winner.output_artifact_ids[0]
            sub.committed_artifacts = [
                ArtifactRef(
                    slot="final",
                    artifact_id=winner.output_artifact_ids[0],
                    artifact_type="RepositoryChangeArtifact",
                )
            ]
        sub.backend_sessions.extend(winner.backend_sessions)

        fl_state.selected_candidate_id = winner.candidate_id
        fl_state.selected_execution_cost = winner.cost
        winner.status = CandidateStatus.COMMITTED
        for cand in fl_state.candidates:
            if cand.candidate_id == winner.candidate_id:
                continue
            if cand.status not in {
                CandidateStatus.REJECTED,
                CandidateStatus.COMMIT_VALIDATION_FAILED,
            }:
                cand.status = CandidateStatus.DISCARDED
            if cand.workspace_ref is not None:
                await self.workspace_manager.discard_candidate(cand.workspace_ref)

        state.fast_loop_history.append(
            LocalUpdateRecord(
                record_id=f"{subtask_id}:{winner.candidate_id}",
                subtask_id=subtask_id,
                revision=sub.local_revision,
                summary=f"committed fast-loop winner {winner.candidate_id}",
                metadata={
                    "parent_graph_hash": winner.parent_graph_hash,
                    "graph_hash": winner.graph_hash,
                    "candidate_search_cost": fl_state.search_cost.model_dump(mode="json"),
                    "selected_execution_cost": winner.cost.model_dump(mode="json"),
                    "initial_execution_cost": fl_state.initial_execution_cost.model_dump(
                        mode="json"
                    ),
                    "total_method_cost": fl_state.total_method_cost.model_dump(mode="json"),
                    "edit_types": [e.type for e in winner.edits],
                    "spent_check": spent_from_state(fl_state).model_dump(mode="json"),
                },
            )
        )
        state.frozen = all(
            s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
        )
        state.mark_ready_from_dependencies()
        logger.info(
            "fast_loop committed winner=%s subtask=%s search_cost=%s selected_cost=%s",
            winner.candidate_id,
            subtask_id,
            fl_state.search_cost.model_dump(),
            winner.cost.model_dump(),
        )

    async def _commit_non_repo_winner(
        self,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        winner: CandidateRecord,
        subtask_id: str,
    ) -> None:
        if winner.status is CandidateStatus.COMMITTED:
            return
        sub = state.subtasks[subtask_id]
        sub.status = SubtaskStatus.COMMITTED
        sub.failure_reason = None
        sub.failure_message = None
        sub.current_graph_hash = winner.graph_hash
        sub.local_revision += 1
        if winner.output_artifact_ids:
            sub.final_output_artifact_id = winner.output_artifact_ids[0]
        fl_state.selected_candidate_id = winner.candidate_id
        fl_state.selected_execution_cost = winner.cost
        winner.status = CandidateStatus.COMMITTED
        for cand in fl_state.candidates:
            if (
                cand.candidate_id != winner.candidate_id
                and cand.status is not CandidateStatus.REJECTED
            ):
                cand.status = CandidateStatus.DISCARDED
        state.frozen = all(
            s.status is SubtaskStatus.COMMITTED for s in state.subtasks.values()
        )
        state.mark_ready_from_dependencies()


def _collect_sessions(
    *,
    result: GraphExecutionResult,
    attempt_id: int,
    candidate_id: str,
) -> list[BackendSessionRecord]:
    from orchestra.backends.base import BackendSessionRef

    records: list[BackendSessionRecord] = []
    for node_id, meta in result.state.node_backend_metadata.items():
        raw = meta.get("session_ref")
        if not raw:
            continue
        session_ref = BackendSessionRef.model_validate(raw)
        backend_id = str(meta.get("backend_id") or session_ref.backend_id)
        records.append(
            BackendSessionRecord(
                node_id=node_id,
                backend_id=backend_id,
                attempt_id=attempt_id,
                session_ref=session_ref,
                candidate_id=candidate_id,
            )
        )
    return records


def _cost_from_result(result: GraphExecutionResult) -> CostRecord:
    prompt = 0
    completion = 0
    calls = 0
    for meta in result.state.node_backend_metadata.values():
        usage = meta.get("usage") or {}
        if isinstance(usage, dict):
            prompt += int(usage.get("prompt_tokens") or 0)
            completion += int(usage.get("completion_tokens") or 0)
        if meta.get("backend_status") or meta.get("session_ref"):
            calls += 1
    if calls == 0:
        calls = 1
    estimated = (prompt * 0.15 + completion * 0.60) / 1_000_000
    return CostRecord(
        prompt_tokens=prompt,
        completion_tokens=completion,
        estimated_cost_usd=estimated,
        backend_calls=calls,
    )
