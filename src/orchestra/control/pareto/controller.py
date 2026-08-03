"""Policies that let the M5 controller use deterministic Pareto search."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from orchestra.control.pareto.archive import ParetoArchive
from orchestra.control.pareto.candidate_generator import ParetoCandidateGenerator
from orchestra.control.pareto.context import build_decision_context
from orchestra.control.pareto.estimator import ParetoObjectiveEstimator
from orchestra.control.pareto.persistence import ParetoPersistence
from orchestra.control.pareto.schemas import (
    ParetoConfig,
    ParetoDecisionRecord,
    ParetoEvaluationKind,
    ParetoSearchState,
    ParetoSelectionProposal,
    ParetoSelectionStatus,
    PreferenceProfile,
    RealizationStatus,
)
from orchestra.control.pareto.selector import DeterministicParetoSelector
from orchestra.control.pareto.telemetry import realized_horizon_objectives
from orchestra.control.pareto.trace_export import ParetoTraceExporter
from orchestra.control.pareto.validation import ParetoCandidateValidator
from orchestra.control.slow_loop.candidate_generator import RuleBasedGlobalCandidateGenerator
from orchestra.control.slow_loop.selector import DeterministicGlobalCandidateSelector
from orchestra.control.task_state import SubtaskStatus


class GlobalCandidatePolicy(Protocol):
    def propose(self, **kwargs): ...
    def select(self, candidates, observation, **kwargs): ...


class RuleBasedGlobalCandidatePolicy:
    def __init__(self, generator=None, selector=None) -> None:
        self.generator = generator or RuleBasedGlobalCandidateGenerator()
        self.selector = selector or DeterministicGlobalCandidateSelector()

    def propose(self, **kwargs):
        return self.generator.generate(**kwargs)

    def select(self, candidates, observation, **kwargs):
        del kwargs
        return self.selector.select(candidates, observation)


class ParetoGlobalCandidatePolicy:
    def __init__(
        self,
        config: ParetoConfig | None = None,
        preference_profile: PreferenceProfile | None = None,
        run_dir: str | None = None,
        catalog=None,
        *,
        runtime_concurrency_cap: int = 1,
    ) -> None:
        self.config = config or ParetoConfig(enabled=True)
        self.profile = preference_profile or PreferenceProfile()
        self.archive = ParetoArchive(self.config)
        self.catalog = catalog
        self.runtime_concurrency_cap = max(1, int(runtime_concurrency_cap))
        self.generator = ParetoCandidateGenerator(
            self.config, self.archive, catalog=catalog
        )
        self.estimator = ParetoObjectiveEstimator(
            runtime_concurrency_cap=self.runtime_concurrency_cap
        )
        self.validator = ParetoCandidateValidator(
            runtime_concurrency_cap=self.runtime_concurrency_cap
        )
        self.selector = DeterministicParetoSelector()
        self._candidates_by_hash: dict[str, object] = {}
        self._contexts: dict[str, object] = {}
        self.persistence = ParetoPersistence(run_dir) if run_dir else None
        self.trace_exporter = ParetoTraceExporter(run_dir) if run_dir else None
        if self.persistence is not None:
            loaded = self.persistence.load_estimated_archive(self.config)
            self.archive.estimated_complete.update(loaded.estimated_complete)
            self.archive.estimated_partial.update(loaded.estimated_partial)
            realized = self.persistence.load_realized_archive(self.config)
            self.archive.realized_complete.update(realized.realized_complete)
            self.archive.realized_partial.update(realized.realized_partial)

    def propose(self, **kwargs):
        state = kwargs["state"]
        context = build_decision_context(
            parent_plan_hash=state.active_plan_hash or state.task_plan.content_hash(),
            parent_communication_hash=state.active_communication_hash or "",
            committed_prefix=[
                sid for sid, sub in state.subtasks.items() if sub.status.value == "committed"
            ],
            eligible_future_subtask_ids=kwargs["eligible"],
            triggers=kwargs.get("triggers", []),
            diagnosis=kwargs["diagnosis"],
            preference_profile=self.profile,
            backend_capabilities=state.task_plan.metadata.get("backend_capabilities", {}),
            state=state,
            objective_config=self.config.objectives,
            communication_plan=state.communication_plan,
        )
        generated = self.generator.generate(
            context=context, **{k: v for k, v in kwargs.items() if k != "triggers"}
        )
        self._contexts[context.context_id] = context
        for cand in generated:
            self._candidates_by_hash[cand.content_hash] = cand
        return generated

    def select(self, candidates, observation, **kwargs) -> ParetoSelectionProposal | None:
        """Propose a selection; never mutate live TaskExecutionState.pareto_state."""
        state = kwargs["state"]
        valid = []
        for candidate in candidates:
            if self.validator.validate(
                candidate, current_state=state, leased_subtask_ids=kwargs["leased_subtask_ids"]
            ).ok:
                estimate = self.estimator.estimate(
                    candidate,
                    observation=observation,
                    archive=self.archive,
                    public_evaluations=getattr(state, "public_evaluation_records", []),
                    state=state,
                    risk_coefficients=self.config.risk_coefficients,
                )
                candidate.objectives = estimate.objective_vector
                self.archive.insert(candidate)
                if self.persistence:
                    self.persistence.append_event(
                        {
                            "event_id": f"est:{candidate.content_hash}",
                            "kind": "estimated",
                            "content_hash": candidate.content_hash,
                            "context_id": candidate.context_id,
                        }
                    )
                valid.append(candidate)
        context_id = (
            valid[0].context_id
            if valid
            else (candidates[0].context_id if candidates else "")
        )
        context = self._contexts.get(context_id)
        if context is None and candidates:
            # Prefer the context captured during propose(); never invent empty hashes.
            raise RuntimeError(
                "ParetoDecisionContext missing for selection; propose() must run first"
            )
        if context is None:
            return None
        # With scalarize_without_pareto_filter, complete_frontier holds all complete
        # candidates (archive insert skips dominance filtering).
        frontier = self.archive.complete_frontier(
            context.context_id, ParetoEvaluationKind.ESTIMATED
        )
        status = ParetoSelectionStatus.NO_COMPARABLE_CANDIDATE
        selected = None
        if frontier:
            selected = self.selector.select(frontier, self.profile, self.config.objectives)
            status = (
                ParetoSelectionStatus.SELECTED_COMPLETE_FRONTIER if selected else status
            )
        elif (
            self.profile.allow_partial_objectives
            and self.profile.profile_id == "data_collection"
        ):
            selected = self.selector.select(
                self.archive.partial_candidates(
                    context.context_id, ParetoEvaluationKind.ESTIMATED
                ),
                self.profile,
                self.config.objectives,
            )
            status = (
                ParetoSelectionStatus.SELECTED_PARTIAL_FOR_DATA_COLLECTION
                if selected
                else status
            )
        elif self.config.fallback_to_rule_based:
            status = ParetoSelectionStatus.FALLBACK_RULE_BASED

        search = ParetoSearchState(enabled=self.config.enabled)
        if isinstance(state.pareto_state, ParetoSearchState):
            search = state.pareto_state.model_copy(deep=True)
            search.enabled = self.config.enabled
        elif state.pareto_state:
            search = ParetoSearchState.model_validate(state.pareto_state)
            search.enabled = self.config.enabled

        affected = list(getattr(context, "eligible_future_subtask_ids", None) or [])
        decision = ParetoDecisionRecord(
            decision_id=(
                f"decision-{selected.content_hash[:16]}"
                if selected
                else f"decision-{context.context_id[:16]}"
            ),
            context=context,
            selected_content_hash=selected.content_hash if selected else None,
            candidate_hashes=sorted(c.content_hash for c in valid),
            profile_id=self.profile.profile_id,
            selected_candidate_snapshot=selected.model_copy(deep=True) if selected else None,
            selection_status=status,
            baseline_usage_index=len(state.backend_usage_records or []),
            baseline_delivery_index=len(state.delivery_ledger or []),
            baseline_commit_index=len(state.workspace_commit_records or []),
            baseline_evidence_keys=list(
                getattr(getattr(state, "slow_loop_state", None), "handled_evidence_keys", [])
                or []
            ),
            baseline_public_evaluation_index=len(
                getattr(state, "public_evaluation_records", []) or []
            ),
            started_at=datetime.now(UTC),
            affected_subtask_ids=affected,
            realization_status=RealizationStatus.PENDING,
        )
        if selected:
            search.pending_candidate_hash = selected.content_hash
            search.pending_decision = decision
            search.horizon_usage_index = decision.baseline_usage_index
            search.horizon_delivery_index = decision.baseline_delivery_index
            search.horizon_commit_index = decision.baseline_commit_index
            search.decisions += 1
        proposal = ParetoSelectionProposal(
            selected_global_candidate=selected.global_candidate if selected else None,
            selected_pareto_candidate=selected,
            projected_pareto_state=search,
            decision_record=decision,
            selection_status=status,
            context=context,
        )
        if self.persistence:
            self.persistence.snapshot(self.archive)
            self.persistence.append_decision(decision)
        if self.trace_exporter:
            self.trace_exporter.emit_candidates(
                valid,
                decision_id=decision.decision_id,
                decision_context=context,
                preference_profile=self.profile,
                selected_hash=selected.content_hash if selected else None,
                status=status,
                activated_revision_id=None,
            )
        return proposal

    @staticmethod
    def _behavioral_realization_ready(state, decision: ParetoDecisionRecord) -> tuple[bool, dict]:
        """Require persisted wave binding + terminal execution under activation.

        Activation alone, wave start alone, or scheduler return alone are insufficient.
        """
        affected = list(decision.affected_subtask_ids or [])
        if not affected:
            affected = list(
                getattr(decision.context, "eligible_future_subtask_ids", None) or []
            )
        terminal = {
            SubtaskStatus.COMMITTED,
            SubtaskStatus.FAILED,
            SubtaskStatus.SKIPPED,
            SubtaskStatus.HARNESS_FAILED,
        }
        if not affected:
            return False, {
                "reason": "no_eligible_future_wave",
                "censored": True,
            }
        wave_id = getattr(decision, "affected_wave_id", None)
        evidence = {
            "decision_id": decision.decision_id,
            "candidate_hash": decision.selected_content_hash,
            "activation_revision": decision.activated_revision_id,
            "affected_wave_id": wave_id,
            "affected_subtask_ids": affected,
            "active_plan_revision_id": state.active_plan_revision_id,
        }
        if not wave_id:
            evidence["reason"] = "affected_wave_unbound"
            return False, evidence
        waves = list(getattr(state, "scheduler_wave_records", None) or [])
        bound = None
        for wave in waves:
            wid = getattr(wave, "wave_id", None) or (
                wave.get("wave_id") if isinstance(wave, dict) else None
            )
            if wid == wave_id:
                bound = wave
                break
        if bound is None:
            evidence["reason"] = "affected_wave_missing"
            return False, evidence
        plan_rev = getattr(bound, "plan_revision", None) or (
            bound.get("plan_revision") if isinstance(bound, dict) else None
        )
        term = getattr(bound, "terminal_state", None) or (
            bound.get("terminal_state") if isinstance(bound, dict) else None
        )
        evidence["wave_plan_revision"] = plan_rev
        evidence["wave_terminal_state"] = term
        evidence["effective_concurrency"] = getattr(
            bound, "effective_concurrency", None
        ) or (bound.get("effective_concurrency") if isinstance(bound, dict) else None)
        if plan_rev != decision.activated_revision_id:
            evidence["reason"] = "wave_revision_mismatch"
            return False, evidence
        if term != "terminal":
            evidence["reason"] = "affected_wave_incomplete"
            return False, evidence
        statuses: dict[str, str] = {}
        incomplete: list[str] = []
        for sid in affected:
            sub = state.subtasks.get(sid)
            if sub is None:
                incomplete.append(sid)
                statuses[sid] = "missing"
                continue
            statuses[sid] = str(
                sub.status.value if hasattr(sub.status, "value") else sub.status
            )
            if sub.status not in terminal:
                incomplete.append(sid)
        evidence["terminal_states"] = statuses
        evidence["incomplete_subtask_ids"] = incomplete
        if incomplete:
            evidence["reason"] = "affected_subtasks_incomplete"
            return False, evidence
        if decision.activated_revision_id != state.active_plan_revision_id:
            evidence["reason"] = "activation_revision_mismatch"
            return False, evidence
        evidence["reason"] = "affected_wave_terminal"
        evidence["binding_status"] = "realized"
        return True, evidence

    def finalize_realized(self, state) -> None:
        search = state.pareto_state
        if not isinstance(search, ParetoSearchState) or not search.pending_decision:
            return
        pending = search.pending_decision
        if pending.activated_revision_id != state.active_plan_revision_id:
            return
        # Exact-once: already realized in history (post-realization crash resume).
        if any(
            getattr(d, "decision_id", None) == pending.decision_id
            and getattr(d, "realization_status", None) == RealizationStatus.REALIZED
            for d in (search.decision_history or [])
        ):
            search.pending_decision = None
            search.pending_candidate_hash = None
            return
        ready, evidence = self._behavioral_realization_ready(state, pending)
        if not ready:
            if evidence.get("censored"):
                censored = pending.model_copy(
                    update={
                        "realization_status": RealizationStatus.CENSORED_NO_ELIGIBLE_WAVE,
                        "realization_evidence": evidence,
                        "reason": "censored_no_eligible_wave",
                        "completed_at": datetime.now(UTC),
                        "completed_state_version": state.state_version,
                    }
                )
                search.decision_history.append(censored)
                search.pending_decision = None
                search.pending_candidate_hash = None
                if self.persistence:
                    self.persistence.append_decision(censored)
            # Incomplete wave: keep pending (do not finalize on scheduler return).
            return
        completed_at = datetime.now(UTC)
        vector, failures = realized_horizon_objectives(
            state,
            usage_start=pending.baseline_usage_index,
            delivery_start=pending.baseline_delivery_index,
            commit_start=pending.baseline_commit_index,
            config=self.config,
            started_at=pending.started_at,
            completed_at=completed_at,
            public_evaluation_start=pending.baseline_public_evaluation_index,
        )
        candidate = pending.selected_candidate_snapshot
        realization_id = f"real:{pending.decision_id}:{pending.activated_revision_id}"
        if candidate is not None:
            candidate.objectives = vector
            candidate.raw_failure_counts = failures
            self.archive.insert(candidate, ParetoEvaluationKind.REALIZED)
            if self.persistence:
                self.persistence.append_event(
                    {
                        "event_id": realization_id,
                        "kind": "realized",
                        "content_hash": candidate.content_hash,
                        "context_id": candidate.context_id,
                        "decision_id": pending.decision_id,
                    }
                )
        policy = getattr(state, "scheduling_policy", None)
        evidence.update(
            {
                "policy_concurrency": getattr(policy, "max_concurrent_subtasks", None),
                "wave_completion": True,
            }
        )
        finalized = pending.model_copy(
            update={
                "evaluation_kind": ParetoEvaluationKind.REALIZED,
                "reason": f"risk={failures.total}",
                "completed_at": completed_at,
                "completed_state_version": state.state_version,
                "selected_candidate_snapshot": candidate,
                "realization_status": RealizationStatus.REALIZED,
                "realization_id": realization_id,
                "realization_evidence": evidence,
                "affected_subtask_ids": list(evidence.get("affected_subtask_ids") or []),
            }
        )
        search.decision_history.append(finalized)
        search.pending_decision = None
        search.pending_candidate_hash = None
        if self.persistence:
            self.persistence.snapshot(self.archive)
            self.persistence.append_decision(finalized)
        if self.trace_exporter and candidate is not None:
            self.trace_exporter.emit_realization(
                candidate=candidate,
                decision=finalized,
                preference_profile=self.profile,
                status=finalized.selection_status,
            )
