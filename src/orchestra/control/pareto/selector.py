"""Deterministic preference-profile selection from a Pareto frontier."""

from __future__ import annotations

from collections.abc import Sequence

from orchestra.control.pareto.schemas import (
    ObjectiveDirection,
    ParetoOrchestraCandidate,
    PreferenceProfile,
)

PROFILES = {
    "quality_first": {"quality": 10.0},
    "cost_capped_quality": {"quality": 5.0, "cost": 1.0},
    "latency_capped_quality": {"quality": 5.0, "latency": 1.0},
    "robustness_first": {"risk": 5.0, "quality": 2.0},
    "balanced_knee": {},
    "data_collection": {"communication_overhead": 1.0},
}


class DeterministicParetoSelector:
    def select(
        self,
        candidates: Sequence[ParetoOrchestraCandidate],
        profile: PreferenceProfile,
        objective_config: dict[str, ObjectiveDirection],
    ) -> ParetoOrchestraCandidate | None:
        # Incomplete vectors cannot dominate a complete frontier, but may still
        # be selected deterministically for data collection or when no complete
        # quality evidence exists.
        usable = [c for c in candidates if not c.validation_errors]
        usable = [c for c in usable if self._passes_constraints(c, profile)]
        if not usable:
            return None
        weights = profile.objective_weights or PROFILES.get(profile.profile_id, {})

        def score(c):
            vals = c.objectives.values
            if profile.profile_id == "balanced_knee":
                # normalized distance to ideal reference point
                return sum(
                    self._normalized_distance(c, usable, name, direction)
                    for name, direction in objective_config.items()
                )
            total = 0.0
            # Named preference profiles only score their weighted objectives.
            names = list(weights) if weights else list(objective_config)
            for name in names:
                direction = objective_config.get(name)
                if direction is None:
                    continue
                if name not in vals or not vals[name].available:
                    total += 1_000_000.0
                    continue
                value = vals[name].value
                total += float(weights.get(name, 1.0)) * (
                    -value if direction is ObjectiveDirection.MAXIMIZE else value
                )
            return total

        return min(
            usable, key=lambda c: (score(c), len(c.edits), c.communication_overhead, c.content_hash)
        )

    @staticmethod
    def _passes_constraints(candidate, profile: PreferenceProfile) -> bool:
        vals = candidate.objectives.values

        def _get(name: str) -> float | None:
            item = vals.get(name)
            if item is None or not item.available or item.value is None:
                return None
            return float(item.value)

        quality = _get("quality")
        cost = _get("cost")
        latency = _get("latency")
        risk = _get("risk")
        if profile.minimum_quality is not None and (
            quality is None or quality < profile.minimum_quality
        ):
            return False
        if profile.maximum_cost_usd is not None and (
            cost is None or cost > profile.maximum_cost_usd
        ):
            return False
        if profile.maximum_latency_seconds is not None and (
            latency is None or latency > profile.maximum_latency_seconds
        ):
            return False
        if profile.maximum_failure_risk is not None and (
            risk is None or risk > profile.maximum_failure_risk
        ):
            return False
        for name, cap in (profile.caps or {}).items():
            value = _get(name)
            if value is None or value > cap:
                return False
        return True

    @staticmethod
    def _normalized_distance(candidate, candidates, name, direction):
        if (
            name not in candidate.objectives.values
            or not candidate.objectives.values[name].available
        ):
            return 1_000_000.0
        values = [
            c.objectives.values[name].value
            for c in candidates
            if name in c.objectives.values and c.objectives.values[name].available
        ]
        if not values:
            return 0.0
        lo, hi = min(values), max(values)
        if hi == lo:
            return 0.0
        value = candidate.objectives.values[name].value
        ideal = hi if direction is ObjectiveDirection.MAXIMIZE else lo
        return abs(value - ideal) / (hi - lo)
