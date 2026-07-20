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
    state: Any | None = None,
    objective_config: Any = None,
    communication_plan: Any = None,
) -> ParetoDecisionContext:
    """Build a content-addressed context without backend-specific branching."""
    from orchestra.control.backend_usage import load_pricing_registry
    committed = []
    if state is not None:
        from orchestra.control.pareto.schemas import CommittedSubtaskFingerprint
        for sid in sorted(committed_prefix):
            sub = state.subtasks.get(sid)
            if sub is not None:
                spec_hash = (
                    sub.spec.content_hash() if hasattr(sub.spec, "content_hash") else ""
                )
                committed.append(
                    CommittedSubtaskFingerprint(
                        subtask_id=sid,
                        spec_hash=spec_hash,
                        committed_revision=getattr(sub, "base_task_revision", None),
                        artifact_hashes=sorted(
                            getattr(a, "content_hash", "") for a in sub.committed_artifacts
                        ),
                    )
                )
    comm_hash = parent_communication_hash or _hash(
        communication_plan.model_dump(mode="json") if hasattr(communication_plan, "model_dump")
        else communication_plan or {}
    )
    payload = {
        "parent_plan_hash": parent_plan_hash,
        "parent_communication_hash": comm_hash,
        "committed_prefix": sorted(committed_prefix),
        "eligible_future_subtask_ids": sorted(eligible_future_subtask_ids),
        "triggers": sorted(str(getattr(t, "value", t)) for t in triggers),
        "diagnosis": diagnosis.model_dump(mode="json")
        if hasattr(diagnosis, "model_dump")
        else diagnosis,
        "preference_profile_id": preference_profile.profile_id,
        "backend_capability_hash": _hash(backend_capabilities),
        "committed_subtasks": [x.model_dump(mode="json") for x in committed],
        "objective_config_hash": _hash(objective_config or {}),
        "preference_profile_hash": _hash(preference_profile.model_dump(mode="json")),
    }
    return ParetoDecisionContext(
        context_id=_hash(payload),
        parent_plan_hash=parent_plan_hash,
        parent_communication_hash=comm_hash,
        committed_prefix=payload["committed_prefix"],
        eligible_future_subtask_ids=payload["eligible_future_subtask_ids"],
        triggers=payload["triggers"],
        diagnosis=payload["diagnosis"],
        preference_profile_id=preference_profile.profile_id,
        backend_capability_hash=payload["backend_capability_hash"],
        task_id=getattr(state, "task_id", ""),
        repository_fingerprint=str(getattr(state, "canonical_workspace_ref", "") or ""),
        canonical_revision=getattr(state, "canonical_revision", None),
        parent_revision_id=getattr(state, "active_plan_revision_id", None),
        committed_prefix_hash=_hash(payload["committed_prefix"]),
        committed_subtasks=committed,
        objective_config_hash=payload["objective_config_hash"],
        preference_profile_hash=payload["preference_profile_hash"],
        pricing_version=load_pricing_registry().pricing_version,
        trigger_reasons=payload["triggers"],
        diagnosis_reasons=sorted(
            str(getattr(x, "value", x)) for x in getattr(diagnosis, "reasons", [])
        ),
    )
