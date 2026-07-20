"""Construction of stable decision-horizon context identities."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from orchestra.control.pareto.schemas import ParetoDecisionContext, PreferenceProfile


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def build_decision_context(
    *,
    parent_plan_hash: str,
    parent_communication_hash: str,
    committed_prefix: list[str] | set[str],
    eligible_future_subtask_ids: list[str] | set[str],
    triggers: list[Any],
    diagnosis: Any,
    preference_profile: PreferenceProfile,
    backend_capabilities: Any,
) -> ParetoDecisionContext:
    """Build a content-addressed context without backend-specific branching."""
    payload = {
        "parent_plan_hash": parent_plan_hash,
        "parent_communication_hash": parent_communication_hash,
        "committed_prefix": sorted(committed_prefix),
        "eligible_future_subtask_ids": sorted(eligible_future_subtask_ids),
        "triggers": sorted(str(getattr(t, "value", t)) for t in triggers),
        "diagnosis": diagnosis.model_dump(mode="json")
        if hasattr(diagnosis, "model_dump")
        else diagnosis,
        "preference_profile_id": preference_profile.profile_id,
        "backend_capability_hash": _hash(backend_capabilities),
    }
    return ParetoDecisionContext(
        context_id=_hash(payload),
        parent_plan_hash=parent_plan_hash,
        parent_communication_hash=parent_communication_hash,
        committed_prefix=payload["committed_prefix"],
        eligible_future_subtask_ids=payload["eligible_future_subtask_ids"],
        triggers=payload["triggers"],
        diagnosis=payload["diagnosis"],
        preference_profile_id=preference_profile.profile_id,
        backend_capability_hash=payload["backend_capability_hash"],
    )
