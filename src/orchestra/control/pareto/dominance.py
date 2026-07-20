"""Deterministic Pareto dominance over partially observable objective vectors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestra.control.pareto.schemas import ObjectiveDirection, ParetoObjectiveVector


def _value(item: Any, name: str) -> tuple[float | None, bool]:
    vector = item.objectives if hasattr(item, "objectives") else item
    if isinstance(vector, ParetoObjectiveVector):
        value = vector.values.get(name)
    elif isinstance(vector, Mapping):
        value = vector.get(name)
    else:
        value = None
    if value is None:
        return None, False
    if hasattr(value, "available"):
        return value.value, bool(value.available and value.value is not None)
    if isinstance(value, Mapping):
        raw = value.get("value")
        return raw, bool(value.get("available", raw is not None) and raw is not None)
    return float(value), True


def dominates(
    a: Any, b: Any, objective_config: Mapping[str, Any], epsilon: Mapping[str, float] | None = None
) -> bool:
    """Return true when ``a`` is no worse and strictly better than ``b``.

    A candidate without every configured objective is ineligible for a complete
    frontier; this deliberately prevents unknown quality from becoming a value.
    """
    eps = epsilon or {}
    strictly_better = False
    for name, direction_raw in objective_config.items():
        av, a_ok = _value(a, name)
        bv, b_ok = _value(b, name)
        if not a_ok or not b_ok:
            return False
        direction = ObjectiveDirection(direction_raw)
        tolerance = float(eps.get(name, 0.0))
        if direction is ObjectiveDirection.MAXIMIZE:
            if av + tolerance < bv:
                return False
            if av > bv + tolerance:
                strictly_better = True
        else:
            if av - tolerance > bv:
                return False
            if av + tolerance < bv:
                strictly_better = True
    return strictly_better
