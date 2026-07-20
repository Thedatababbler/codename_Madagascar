"""Bounded deterministic, mutation-only generator for global Pareto search."""

from __future__ import annotations

from itertools import combinations
from typing import Protocol

from orchestra.control.pareto.archive import ParetoArchive
from orchestra.control.pareto.candidate import build_pareto_candidate
from orchestra.control.pareto.schemas import (
    ParetoConfig,
    ParetoDecisionContext,
    ParetoOrchestraCandidate,
)
from orchestra.control.slow_loop.agent_node_resolver import FutureAgentNodeResolver
from orchestra.control.slow_loop.edits import apply_global_edits
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    GlobalCandidate,
    GlobalCandidateValidationStatus,
    GlobalDiagnosis,
    GlobalDiagnosisReason,
    GlobalEdit,
    GlobalObservation,
    PendingBackendAssignmentEdit,
    PendingPriorityEdit,
    RemovePayloadContractEdit,
    SchedulingConcurrencyEdit,
    SerializationGroupEdit,
    TaskSchedulingPolicy,
    UpsertPayloadContractEdit,
)
from orchestra.control.task_state import TaskExecutionState


class CandidateProposer(Protocol):
    def propose(self, **kwargs) -> list[GlobalEdit]: ...


class ParetoCandidateGenerator:
    def __init__(
        self, config: ParetoConfig | None = None, archive: ParetoArchive | None = None
    ) -> None:
        self.config = config or ParetoConfig()
        self.archive = archive
        self.resolver = FutureAgentNodeResolver()

    def generate(
        self,
        *,
        task_plan,
        communication_plan,
        scheduling_policy: TaskSchedulingPolicy,
        state: TaskExecutionState,
        observation: GlobalObservation,
        diagnosis: GlobalDiagnosis,
        eligible: set[str],
        context: ParetoDecisionContext,
    ) -> list[ParetoOrchestraCandidate]:
        del observation
        edits: list[list[GlobalEdit]] = []
        ids = sorted(eligible)
        reasons = set(diagnosis.reasons)
        # Always offer a small scheduling neighbourhood.
        for concurrency in sorted(
            {
                1,
                scheduling_policy.max_concurrent_subtasks,
                max(1, scheduling_policy.max_concurrent_subtasks - 1),
                scheduling_policy.max_concurrent_subtasks + 1,
            }
        ):
            if concurrency != scheduling_policy.max_concurrent_subtasks:
                edits.append([SchedulingConcurrencyEdit(max_concurrent_subtasks=concurrency)])
        if len(ids) >= 2 and (
            GlobalDiagnosisReason.SCHEDULING_CONTENTION in reasons
            or GlobalDiagnosisReason.CANONICAL_CONFLICT_RISK in reasons
        ):
            edits.append([SerializationGroupEdit(subtask_ids=ids[:2])])
        for sid in ids[:2]:
            current = next((s for s in task_plan.subtasks if s.subtask_id == sid), None)
            if current and (
                GlobalDiagnosisReason.SCHEDULING_CONTENTION in reasons
                or GlobalDiagnosisReason.BUDGET_PRESSURE in reasons
            ):
                edits.append([PendingPriorityEdit(subtask_id=sid, priority=current.priority + 1)])
        if GlobalDiagnosisReason.CONTEXT_PRESSURE in reasons:
            for sid in ids[:2]:
                budget = communication_plan.context_budgets.get(sid)
                if budget and budget > 64:
                    edits.append(
                        [ContextBudgetEdit(target_subtask_id=sid, max_tokens=max(64, budget // 2))]
                    )
                optional = next(
                    (
                        c
                        for c in communication_plan.payload_contracts
                        if c.target_subtask_id == sid and not c.is_required()
                    ),
                    None,
                )
                if optional is not None:
                    shrunk = optional.model_copy(
                        update={"max_tokens": max(64, optional.max_tokens // 2)}
                    )
                    edits.append([UpsertPayloadContractEdit(contract=shrunk)])
                    edits.append([RemovePayloadContractEdit(payload_id=optional.payload_id)])
        if (
            GlobalDiagnosisReason.BACKEND_INSTABILITY in reasons
            or GlobalDiagnosisReason.BUDGET_PRESSURE in reasons
        ):
            allowed = sorted({b for values in self._allowed(state).values() for b in values})
            by_id = {s.subtask_id: s for s in task_plan.subtasks}
            for sid in ids[:1]:
                resolution = self.resolver.resolve(
                    subtask=by_id[sid],
                    purpose="pareto_adaptation",
                    allowed_backend_ids=set(allowed),
                )
                alternatives = [b for b in allowed if b != resolution.current_backend_id]
                if resolution.eligible and resolution.node_id and alternatives:
                    edits.append(
                        [
                            PendingBackendAssignmentEdit(
                                subtask_id=sid,
                                node_id=resolution.node_id,
                                backend_id=alternatives[0],
                            )
                        ]
                    )
        # Archive-guided replay is only a declared edit list, never a genetic operation.
        if self.archive:
            for archived in self.archive.entries(context.context_id, kind=self._estimated_kind())[
                :1
            ]:
                if archived.edits:
                    edits.append(list(archived.edits[:1]))
        if self.config.allow_two_edit_pairs:
            singles = list(edits)
            for left, right in combinations(singles, 2):
                merged = left + right
                if self._compatible(merged):
                    edits.append(merged)
        candidates: list[ParetoOrchestraCandidate] = []
        seen: set[str] = set()
        for edit_list in edits:
            built = self._build(
                edit_list,
                task_plan,
                communication_plan,
                scheduling_policy,
                diagnosis,
                eligible,
                context,
            )
            if built.content_hash not in seen:
                seen.add(built.content_hash)
                candidates.append(built)
            if len(candidates) >= self.config.max_candidates:
                break
        return candidates

    @staticmethod
    def _allowed(state: TaskExecutionState) -> dict[str, list[str]]:
        return dict((state.task_plan.metadata or {}).get("allowed_backend_assignments") or {})

    @staticmethod
    def _estimated_kind():
        from orchestra.control.pareto.schemas import ParetoEvaluationKind

        return ParetoEvaluationKind.ESTIMATED

    @staticmethod
    def _compatible(edits: list[GlobalEdit]) -> bool:
        targets = [(getattr(e, "target_subtask_id", None), e.type) for e in edits]
        return len(targets) == len(set(targets)) or all(t[0] is None for t in targets)

    def _build(self, edits, task_plan, communication_plan, policy, diagnosis, eligible, context):
        new_plan, new_comm, new_policy, rejected = apply_global_edits(
            task_plan=task_plan,
            communication_plan=communication_plan,
            scheduling_policy=policy,
            edits=edits,
            eligible_subtask_ids=eligible,
        )
        global_candidate = GlobalCandidate(
            candidate_id=f"pareto-{len(edits)}-{len(context.context_id)}",
            diagnosis=diagnosis,
            edits=edits,
            proposed_task_plan=new_plan,
            proposed_communication_plan=new_comm,
            proposed_scheduling_policy=new_policy,
            validation_status=GlobalCandidateValidationStatus.INVALID
            if rejected
            else GlobalCandidateValidationStatus.VALID,
            rejection_reason="rejected ineligible edits" if rejected else None,
        )
        return build_pareto_candidate(
            global_candidate=global_candidate,
            context=context,
            communication_overhead=float(
                sum(
                    getattr(e, "contract", None).max_tokens if hasattr(e, "contract") else 0
                    for e in edits
                )
            ),
        )
