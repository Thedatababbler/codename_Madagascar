"""Evidence-based, candidate-specific objective estimation."""

from __future__ import annotations

from orchestra.control.backend_usage import derive_cost_usd, load_pricing_registry
from orchestra.control.pareto.archive import ParetoArchive
from orchestra.control.pareto.schemas import (
    CandidateObjectiveEstimate,
    EvaluationVisibility,
    ObjectiveSource,
    ObjectiveValue,
    ParetoEvaluationKind,
    ParetoOrchestraCandidate,
)
from orchestra.control.slow_loop.schemas import (
    ContextBudgetEdit,
    GlobalObservation,
    PendingBackendAssignmentEdit,
    RemovePayloadContractEdit,
    SchedulingConcurrencyEdit,
    SerializationGroupEdit,
    TaskBudgetRemaining,
    UpsertPayloadContractEdit,
)


class ParetoObjectiveEstimator:
    """Estimate incremental decision-horizon objectives for one candidate."""

    estimator_version = "m6.1"

    def estimate(
        self,
        candidate: ParetoOrchestraCandidate,
        *,
        observation: GlobalObservation,
        task_budget: TaskBudgetRemaining | None = None,
        archive: ParetoArchive | None = None,
        public_evaluations=None,
        state=None,
        risk_coefficients: dict[str, float] | None = None,
    ) -> CandidateObjectiveEstimate:
        del archive, task_budget
        values = dict(candidate.objectives.values)
        coeffs = risk_coefficients or {
            "backend_failures": 1.0,
            "harness_failures": 1.0,
            "delivery_failures": 1.0,
            "aggregation_failures": 1.0,
            "canonical_conflicts": 1.0,
            "timeouts": 1.0,
        }
        edit_types = {getattr(e, "type", "") for e in candidate.edits}
        history = list(getattr(state, "backend_usage_records", None) or [])
        if not history:
            history = list(getattr(observation, "backend_usage_records", []) or [])

        graph_only = edit_types == {"pending_graph_template"} or (
            "pending_graph_template" in edit_types and not (
                edit_types & {
                    "pending_backend_assignment",
                    "scheduling_concurrency",
                    "serialization_group",
                    "context_budget",
                    "upsert_payload_contract",
                    "remove_payload_contract",
                }
            )
        )
        # Honor catalog-declared evidence when present; otherwise do not invent
        # neutral values for graph-template-only candidates.
        pre_cost = values.get("cost")
        pre_latency = values.get("latency")
        if (
            graph_only
            and pre_cost is not None
            and pre_cost.available
            and pre_cost.value is not None
        ):
            values["cost"] = pre_cost
        else:
            values["cost"] = self._estimate_cost(candidate, history, edit_types)
            if (
                graph_only
                and (
                    pre_cost is None
                    or not pre_cost.available
                    or "missing defensible evidence" in str(pre_cost.detail or "")
                )
                and values["cost"].evidence_count == 0
            ):
                values["cost"] = ObjectiveValue.unavailable(
                    "catalog graph template missing defensible cost evidence"
                )
        if (
            graph_only
            and pre_latency is not None
            and pre_latency.available
            and pre_latency.value is not None
        ):
            values["latency"] = pre_latency
        else:
            values["latency"] = self._estimate_latency(
                candidate, history, observation, edit_types
            )
            if (
                graph_only
                and (
                    pre_latency is None
                    or not pre_latency.available
                    or "missing defensible evidence" in str(pre_latency.detail or "")
                )
                and values["latency"].evidence_count == 0
            ):
                values["latency"] = ObjectiveValue.unavailable(
                    "catalog graph template missing defensible latency evidence"
                )
        values["risk"] = self._estimate_risk(candidate, observation, edit_types, coeffs)
        values["communication_overhead"] = self._estimate_communication(candidate, edit_types)
        values["quality"] = self._estimate_quality(candidate, public_evaluations)
        if graph_only and not values["quality"].available:
            values["quality"] = ObjectiveValue.unavailable(
                "catalog graph template missing defensible quality evidence"
            )

        candidate.objectives.values = values
        candidate.objectives.evaluation_kind = ParetoEvaluationKind.ESTIMATED
        uncertainty = {
            name: 1.0 / max(1, value.evidence_count)
            for name, value in values.items()
            if value.available
        }
        return CandidateObjectiveEstimate(
            objective_vector=candidate.objectives,
            uncertainty=uncertainty,
            lower_bounds={
                n: float(v.value) - uncertainty[n]
                for n, v in values.items()
                if v.available and v.value is not None
            },
            upper_bounds={
                n: float(v.value) + uncertainty[n]
                for n, v in values.items()
                if v.available and v.value is not None
            },
            evidence_counts={n: v.evidence_count for n, v in values.items()},
            estimator_version=self.estimator_version,
        )

    def _estimate_cost(self, candidate, history, edit_types) -> ObjectiveValue:
        pricing = load_pricing_registry()
        backend_edit = next(
            (e for e in candidate.edits if isinstance(e, PendingBackendAssignmentEdit)),
            None,
        )
        matching = []
        if backend_edit is not None:
            matching = [
                r
                for r in history
                if getattr(r, "backend_id", None) == backend_edit.backend_id
                and getattr(r, "estimated_cost_usd", None) is not None
            ]
        if not matching:
            matching = [
                r for r in history if getattr(r, "estimated_cost_usd", None) is not None
            ]
        if matching:
            base = sum(float(r.estimated_cost_usd) for r in matching) / len(matching)
            source = ObjectiveSource.HISTORY
            detail = "history"
            evidence = len(matching)
        else:
            # Configured profile / declared upper bound fallback (not lifetime spent).
            model_name = None
            if backend_edit is not None:
                model_name = next(iter(pricing.models), None)
            cost, quality = derive_cost_usd(
                prompt_tokens=1024,
                completion_tokens=256,
                cached_tokens=0,
                model_name=model_name,
                provider_cost_usd=None,
                pricing=pricing,
            )
            if cost is None:
                return ObjectiveValue.unavailable("no incremental backend cost evidence")
            base = float(cost)
            source = ObjectiveSource.CONFIGURED_PROFILE
            detail = f"configured profile ({quality})"
            evidence = 0
        # Verifier/retry + control overhead heuristics (traceable coefficients).
        verifier = 0.15 * base if "pending_backend_assignment" in edit_types else 0.05 * base
        control = 0.02 * base
        if "serialization_group" in edit_types:
            control *= 1.1
        value = base + verifier + control
        scheduling_only = (
            "scheduling_concurrency" in edit_types
            and "pending_backend_assignment" not in edit_types
        )
        if scheduling_only:
            # Scheduling-only: keep cost near baseline.
            value = base + control
        return ObjectiveValue(
            value=value,
            available=True,
            source=source,
            evidence_count=evidence,
            detail=detail,
        )

    def _estimate_latency(
        self, candidate, history, observation: GlobalObservation, edit_types
    ) -> ObjectiveValue:
        durations = [
            float(r.latency_seconds)
            for r in history
            if getattr(r, "latency_seconds", None) is not None
        ]
        if durations:
            node_expected = sum(durations) / len(durations)
            evidence = len(durations)
            source = ObjectiveSource.HISTORY
        elif observation.backend_latency_summary:
            node_expected = sum(observation.backend_latency_summary.values()) / len(
                observation.backend_latency_summary
            )
            evidence = len(observation.backend_latency_summary)
            source = ObjectiveSource.HISTORY
        else:
            node_expected = 1.0
            evidence = 0
            source = ObjectiveSource.CONFIGURED_PROFILE

        concurrency = 1
        for edit in candidate.edits:
            if isinstance(edit, SchedulingConcurrencyEdit):
                concurrency = max(1, int(edit.max_concurrent_subtasks))
            if isinstance(edit, SerializationGroupEdit):
                # Serialization forces a longer critical path.
                node_expected *= 1.0 + 0.25 * max(0, len(edit.subtask_ids) - 1)

        # Deterministic critical-path estimator over expected node durations.
        waves = max(1, (2 + concurrency - 1) // concurrency)
        critical_path = node_expected * waves
        if "serialization_group" in edit_types:
            critical_path = max(critical_path, node_expected * 2)
        if "scheduling_concurrency" in edit_types and concurrency > 1:
            critical_path = node_expected  # parallelizable wave
        return ObjectiveValue(
            value=float(critical_path),
            available=True,
            source=source,
            evidence_count=evidence,
            detail="critical_path",
        )

    def _estimate_risk(
        self, candidate, observation: GlobalObservation, edit_types, coeffs
    ) -> ObjectiveValue:
        base = float(sum(observation.recent_failure_counts.values()))
        risk = base
        if "pending_backend_assignment" in edit_types:
            risk = max(0.0, risk - coeffs.get("backend_failures", 1.0) * 0.5)
        if "serialization_group" in edit_types:
            risk = max(0.0, risk - coeffs.get("canonical_conflicts", 1.0) * 0.4)
            risk += 0.05  # latency/contention residual
        if "context_budget" in edit_types or "remove_payload_contract" in edit_types:
            risk = max(0.0, risk - 0.2) + 0.1  # lower budget pressure, higher missing-context
        if "upsert_payload_contract" in edit_types:
            risk += 0.05
        return ObjectiveValue(
            value=float(risk),
            available=True,
            source=ObjectiveSource.HISTORY,
            evidence_count=sum(observation.recent_failure_counts.values()),
            detail="edit-conditioned risk",
        )

    def _estimate_communication(self, candidate, edit_types) -> ObjectiveValue:
        tokens = float(candidate.communication_overhead)
        for edit in candidate.edits:
            if isinstance(edit, UpsertPayloadContractEdit):
                tokens = float(getattr(edit.contract, "max_tokens", tokens) or tokens)
            if isinstance(edit, RemovePayloadContractEdit):
                tokens = 0.0
            if isinstance(edit, ContextBudgetEdit):
                tokens = min(tokens or float(edit.max_tokens), float(edit.max_tokens))
        if not edit_types.intersection(
            {
                "upsert_payload_contract",
                "remove_payload_contract",
                "context_budget",
            }
        ):
            tokens = float(candidate.communication_overhead)
        return ObjectiveValue(
            value=tokens,
            available=True,
            source=ObjectiveSource.ESTIMATED,
            evidence_count=1,
            detail="projected tokens",
        )

    def _estimate_quality(self, candidate, public_evaluations) -> ObjectiveValue:
        def field(e, name):
            return e.get(name) if isinstance(e, dict) else getattr(e, name, None)

        evaluations = [
            e
            for e in (public_evaluations or [])
            if str(field(e, "visibility") or "").lower()
            in {
                EvaluationVisibility.PUBLIC.value,
                EvaluationVisibility.DEVELOPMENT.value,
            }
        ]
        if not evaluations:
            return ObjectiveValue.unavailable(
                "quality estimator requires public harness evidence"
            )

        matched = [
            e
            for e in evaluations
            if field(e, "candidate_content_hash") == candidate.content_hash
            or field(e, "edit_signature") == candidate.edit_signature
        ]
        pool = matched or evaluations
        scores = []
        for e in pool:
            if hasattr(e, "resolved_quality"):
                scores.append(float(e.resolved_quality()))
                continue
            score = field(e, "quality")
            if score is None:
                score = field(e, "normalized_score")
            if score is not None:
                scores.append(float(score))
        if not scores:
            return ObjectiveValue.unavailable(
                "quality estimator requires public harness evidence"
            )
        return ObjectiveValue(
            value=sum(scores) / len(scores),
            available=True,
            source=ObjectiveSource.HISTORY,
            evaluation_visibility=EvaluationVisibility.PUBLIC,
            evidence_count=len(scores),
            detail="public/development harness history",
        )
