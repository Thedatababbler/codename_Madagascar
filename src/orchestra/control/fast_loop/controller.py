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
from orchestra.control.fast_loop.budget import can_launch_candidate, spent_from_state
from orchestra.control.fast_loop.candidate_generator import (
    RuleBasedLocalCandidateGenerator,
)
from orchestra.control.fast_loop.capability import validate_candidate_against_capabilities
from orchestra.control.fast_loop.diagnosis import diagnose_subtask_failure
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
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
from orchestra.ir.artifacts import ArtifactBundle
from orchestra.ir.graph import OrchestraGraph, load_graph
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
    ) -> None:
        self.runtime = runtime
        self.artifact_store = artifact_store
        self.task_checkpoint_store = task_checkpoint_store
        self.contracts_dir = contracts_dir
        self.workspace_manager = workspace_manager or GitCandidateWorkspaceManager()
        self.compiler = build_compiler(contracts_dir)
        self.generator = generator or RuleBasedLocalCandidateGenerator(
            compiler=self.compiler
        )
        self.selector = selector or DeterministicCandidateSelector()
        self.budget = budget or FastLoopBudget()
        self.capabilities = dict(capabilities or {})

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

    async def run(
        self,
        *,
        state: TaskExecutionState,
        subtask_id: str,
        context: RunContext,
        initial_artifacts: ArtifactBundle,
        source_repo: str | None = None,
        graph: OrchestraGraph | None = None,
    ) -> TaskExecutionState:
        sub = state.subtasks[subtask_id]
        if sub.status is SubtaskStatus.COMMITTED:
            return state

        base_graph = graph or load_graph(sub.spec.local_graph_template)
        diagnosis = diagnose_subtask_failure(subtask_state=sub, graph=base_graph)

        # Resume existing fast-loop state when present.
        fl_state = state.fast_loop_states.get(subtask_id)
        if fl_state is None:
            fl_state = FastLoopState(
                subtask_id=subtask_id,
                base_attempt_id=len(sub.attempts),
                base_graph_hash=base_graph.content_hash,
                diagnosis=diagnosis,
            )
            state.fast_loop_states[subtask_id] = fl_state
        else:
            # Crash recovery: running → pending for safe restart.
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

        if diagnosis.infrastructure_related:
            await self._infra_retry_once(
                state=state,
                fl_state=fl_state,
                subtask_id=subtask_id,
                base_graph=base_graph,
                context=context,
                initial_artifacts=initial_artifacts,
                source_repo=source_repo,
            )
            await self.task_checkpoint_store.save(state)
            return state

        if not diagnosis.retryable:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            await self.task_checkpoint_store.save(state)
            return state

        if not fl_state.candidates:
            caps = self._caps_for_graph(base_graph)
            generated = self.generator.generate(
                graph=base_graph,
                diagnosis=diagnosis,
                budget=self.budget,
                capabilities=caps,
            )
            for cand in generated:
                fl_state.candidates.append(
                    CandidateRecord(
                        candidate_id=cand.candidate_id,
                        attempt_id=fl_state.base_attempt_id + 1,
                        graph_hash=cand.graph.content_hash,
                        parent_graph_hash=cand.parent_graph_hash,
                        edits=list(cand.edits),
                        status=CandidateStatus.PENDING,
                        session_policy=cand.session_policy,
                        metadata={
                            "generation_reason": cand.generation_reason,
                            "graph": cand.graph.model_dump(mode="json"),
                        },
                    )
                )
            await self.task_checkpoint_store.save(state)

        if not fl_state.candidates:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            await self.task_checkpoint_store.save(state)
            return state

        base_ws: WorkspaceRef | None = None
        repo = source_repo
        if repo:
            base_ws = await self.workspace_manager.prepare_base_snapshot(
                source_repo=repo,
                run_dir=str(context.run_dir),
                task_id=state.task_id,
                subtask_id=subtask_id,
            )
            sub.workspace_ref = base_ws.path

        for record in fl_state.candidates:
            if record.status not in {
                CandidateStatus.PENDING,
            }:
                continue
            ok, reason = can_launch_candidate(self.budget, fl_state)
            if not ok:
                fl_state.exhausted = True
                record.status = CandidateStatus.REJECTED
                record.failure_message = reason
                await self.task_checkpoint_store.save(state)
                break

            graph_payload = record.metadata.get("graph")
            if not graph_payload:
                record.status = CandidateStatus.REJECTED
                record.failure_message = "missing candidate graph payload"
                continue
            candidate_graph = OrchestraGraph.model_validate(graph_payload)
            local_candidate = LocalCandidate(
                candidate_id=record.candidate_id,
                parent_graph_hash=record.parent_graph_hash,
                edits=list(record.edits),
                graph=candidate_graph,
                session_policy=record.session_policy,
                generation_reason=str(
                    record.metadata.get("generation_reason") or ""
                ),
            )
            caps = self._caps_for_graph(candidate_graph)
            compat = validate_candidate_against_capabilities(local_candidate, caps)
            if not compat.compatible:
                record.status = CandidateStatus.REJECTED
                record.failure_message = compat.reason
                # Compatibility rejects do not count as model failures.
                await self.task_checkpoint_store.save(state)
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
            await self.task_checkpoint_store.save(state)

        winner = self.selector.select(fl_state.candidates, self.budget)
        if winner is None:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = diagnosis.reason
            sub.failure_message = (
                diagnosis.concise_feedback or "fast loop exhausted without valid winner"
            )
            await self.task_checkpoint_store.save(state)
            return state

        if base_ws is None:
            # Non-repo tasks: mark committed from winner artifacts only.
            await self._commit_non_repo_winner(state, fl_state, winner, subtask_id)
            await self.task_checkpoint_store.save(state)
            return state

        await self._commit_winner(
            state=state,
            fl_state=fl_state,
            winner=winner,
            base_ws=base_ws,
            subtask_id=subtask_id,
        )
        await self.task_checkpoint_store.save(state)
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
    ) -> None:
        """Allow one infrastructure retry without graph edits."""
        sub = state.subtasks[subtask_id]
        if fl_state.infra_retries_used >= 1:
            fl_state.exhausted = True
            sub.status = SubtaskStatus.FAILED
            return
        fl_state.infra_retries_used += 1
        fl_state.search_cost = CostRecord(
            backend_calls=fl_state.search_cost.backend_calls + 1,
            estimated_cost_usd=fl_state.search_cost.estimated_cost_usd,
        )
        # Record incident but do not generate prompt edits.
        fl_state.candidates.append(
            CandidateRecord(
                candidate_id="infra_retry_0",
                attempt_id=fl_state.base_attempt_id + 1,
                graph_hash=base_graph.content_hash,
                parent_graph_hash=base_graph.content_hash,
                edits=[],
                status=CandidateStatus.REJECTED,
                failure_reason=SubtaskFailureReason.INFRA,
                failure_message="infrastructure retry slot reserved (no graph edit)",
                stability_incidents=[
                    StabilityIncident(
                        kind="infrastructure",
                        message=sub.failure_message or "infra failure",
                    )
                ],
                metadata={"infrastructure_related": True, "no_graph_edit": True},
            )
        )
        # Re-run base graph once in base/canonical workspace if available.
        workspace_ref = sub.workspace_ref or context.workspace_ref
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
            sub.status = SubtaskStatus.FAILED
            sub.failure_reason = SubtaskFailureReason.INFRA
            sub.failure_message = f"{type(exc).__name__}: {exc}"
            fl_state.exhausted = True
            return
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
            for cand in fl_state.candidates:
                if cand.candidate_id == "infra_retry_0":
                    cand.status = CandidateStatus.COMMITTED
            state.mark_ready_from_dependencies()
        else:
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

        # Isolate graph checkpoints per candidate so edited graph hashes do not
        # collide with the base attempt checkpoint (CheckpointDriftError).
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
            record.status = CandidateStatus.BACKEND_FAILED
            record.failure_reason = SubtaskFailureReason.INFRA
            record.failure_message = f"{type(exc).__name__}: {exc}"
            record.latency_ms = int((time.perf_counter() - started) * 1000)
            record.cost = CostRecord(backend_calls=1)
            record.stability_incidents.append(
                StabilityIncident(kind="exception", message=record.failure_message)
            )
            return

        record.latency_ms = int((time.perf_counter() - started) * 1000)
        record.backend_sessions = _collect_sessions(
            result=result,
            attempt_id=record.attempt_id,
            candidate_id=record.candidate_id,
        )
        usage_cost = _cost_from_result(result)
        record.cost = usage_cost
        fl_state.search_cost = CostRecord(
            prompt_tokens=fl_state.search_cost.prompt_tokens + usage_cost.prompt_tokens,
            completion_tokens=(
                fl_state.search_cost.completion_tokens + usage_cost.completion_tokens
            ),
            estimated_cost_usd=(
                fl_state.search_cost.estimated_cost_usd + usage_cost.estimated_cost_usd
            ),
            backend_calls=fl_state.search_cost.backend_calls + usage_cost.backend_calls,
        )

        if cand_ws is not None:
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

        # Persist telemetry-friendly summary under candidate dir.
        if cand_ws is not None:
            summary_path = Path(cand_ws.path).parent / "candidate_result.json"
            summary_path.write_text(
                record.model_dump_json(indent=2),
                encoding="utf-8",
            )

    async def _commit_winner(
        self,
        *,
        state: TaskExecutionState,
        fl_state: FastLoopState,
        winner: CandidateRecord,
        base_ws: WorkspaceRef,
        subtask_id: str,
    ) -> None:
        if winner.status is CandidateStatus.COMMITTED:
            return  # idempotent
        if winner.workspace_ref is None:
            raise CandidateWorkspaceError("winner missing workspace_ref")
        # Re-validate winner harness result already recorded as VALID.
        if winner.status is not CandidateStatus.VALID:
            raise CandidateWorkspaceError("refusing to commit non-valid winner")

        committed = await self.workspace_manager.commit_winner(
            base=base_ws,
            winner=winner.workspace_ref,
            expected_base_revision=base_ws.base_revision,
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
            if cand.status is not CandidateStatus.REJECTED:
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
                    "search_cost": spent_from_state(fl_state).model_dump(mode="json"),
                    "selected_execution_cost": winner.cost.model_dump(mode="json"),
                    "edit_types": [e.type for e in winner.edits],
                },
            )
        )
        # Task frozen only when all subtasks committed.
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
            if cand.candidate_id != winner.candidate_id:
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
    # Rough default pricing placeholder for telemetry only.
    estimated = (prompt * 0.15 + completion * 0.60) / 1_000_000
    return CostRecord(
        prompt_tokens=prompt,
        completion_tokens=completion,
        estimated_cost_usd=estimated,
        backend_calls=calls,
    )
