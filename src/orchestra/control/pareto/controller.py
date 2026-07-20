"""Policies that let the M5 controller use deterministic Pareto search."""

from __future__ import annotations

from typing import Protocol

from orchestra.control.pareto.archive import ParetoArchive
from orchestra.control.pareto.candidate_generator import ParetoCandidateGenerator
from orchestra.control.pareto.context import build_decision_context
from orchestra.control.pareto.estimator import ParetoObjectiveEstimator
from orchestra.control.pareto.schemas import (
    ParetoConfig,
    ParetoDecisionRecord,
    ParetoSearchState,
    PreferenceProfile,
)
from orchestra.control.pareto.selector import DeterministicParetoSelector
from orchestra.control.pareto.telemetry import realized_horizon_objectives
from orchestra.control.pareto.validation import ParetoCandidateValidator
from orchestra.control.slow_loop.candidate_generator import RuleBasedGlobalCandidateGenerator
from orchestra.control.slow_loop.selector import DeterministicGlobalCandidateSelector


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
    ) -> None:
        self.config = config or ParetoConfig(enabled=True)
        self.profile = preference_profile or PreferenceProfile()
        self.archive = ParetoArchive(self.config)
        self.generator = ParetoCandidateGenerator(self.config, self.archive)
        self.estimator = ParetoObjectiveEstimator()
        self.validator = ParetoCandidateValidator()
        self.selector = DeterministicParetoSelector()
        self._candidates_by_hash = {}

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
        )
        return self.generator.generate(
            context=context, **{k: v for k, v in kwargs.items() if k != "triggers"}
        )

    def select(self, candidates, observation, **kwargs):
        state = kwargs["state"]
        valid = []
        for candidate in candidates:
            if self.validator.validate(
                candidate, current_state=state, leased_subtask_ids=kwargs["leased_subtask_ids"]
            ).ok:
                self.estimator.estimate(candidate, observation=observation, archive=self.archive)
                self.archive.insert(candidate)
                valid.append(candidate)
        selected = self.selector.select(valid, self.profile, self.config.objectives)
        if selected is None:
            return None
        search = (
            state.pareto_state
            if isinstance(state.pareto_state, ParetoSearchState)
            else ParetoSearchState.model_validate(state.pareto_state or {})
        )
        search.enabled = self.config.enabled
        search.pending_candidate_hash = selected.content_hash
        self._candidates_by_hash[selected.content_hash] = selected.model_copy(deep=True)
        search.pending_decision = ParetoDecisionRecord(
            decision_id=f"decision-{selected.content_hash[:16]}",
            context=self._context_from_candidate(selected),
            selected_content_hash=selected.content_hash,
            candidate_hashes=sorted(c.content_hash for c in valid),
            profile_id=self.profile.profile_id,
        )
        search.horizon_usage_index = len(state.backend_usage_records or [])
        search.horizon_delivery_index = len(state.delivery_ledger or [])
        search.horizon_commit_index = len(state.workspace_commit_records or [])
        search.decisions += 1
        state.pareto_state = search
        return selected.global_candidate

    def _context_from_candidate(self, candidate):
        # The context fields are stored by the active decision and do not need
        # backend identities. Reconstructing minimal fields keeps candidates portable.
        from orchestra.control.pareto.schemas import ParetoDecisionContext

        return ParetoDecisionContext(
            context_id=candidate.context_id, parent_plan_hash="", parent_communication_hash=""
        )

    def finalize_realized(self, state) -> None:
        search = state.pareto_state
        if not isinstance(search, ParetoSearchState) or not search.pending_decision:
            return
        vector, failures = realized_horizon_objectives(
            state,
            usage_start=search.horizon_usage_index,
            delivery_start=search.horizon_delivery_index,
            commit_start=search.horizon_commit_index,
            config=self.config,
        )
        candidate = self._candidates_by_hash.get(search.pending_candidate_hash or "")
        if candidate is not None:
            candidate.objectives = vector
            candidate.raw_failure_counts = failures
            self.archive.insert(candidate)
        # A realized record is retained even when quality remains unavailable.
        search.pending_decision = search.pending_decision.model_copy(
            update={
                "evaluation_kind": "realized",
                "reason": f"risk={failures.total}",
            }
        )
        search.decision_history.append(search.pending_decision)
        search.pending_decision = None
        search.pending_candidate_hash = None
