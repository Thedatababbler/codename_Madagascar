"""Append-only search trace export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.control.pareto.schemas import (
    ParetoDecisionContext,
    ParetoObjectiveVector,
    ParetoOrchestraCandidate,
    ParetoSelectionStatus,
    PreferenceProfile,
)


class OrchestraSearchTrace(BaseModel):
    """Portable JSONL record emitted for every runtime candidate."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    task_id: str = ""
    decision_id: str = ""
    decision_context: ParetoDecisionContext | None = None
    preference_profile: PreferenceProfile | None = None

    candidate_id: str = ""
    candidate_content_hash: str = ""
    edit_signature: str = ""
    candidate_edits: list[dict[str, Any]] = Field(default_factory=list)
    candidate_provenance: str = "pareto_generator"

    feasibility_status: str = ""
    validation_errors: list[str] = Field(default_factory=list)

    estimated_objectives: ParetoObjectiveVector | None = None
    selected: bool = False
    selection_status: ParetoSelectionStatus | str = ""

    activated_revision_id: str | None = None
    realized_objectives: ParetoObjectiveVector | None = None
    pareto_status: str | None = None


class ParetoTraceExporter:
    def __init__(self, run_dir: str | Path) -> None:
        directory = Path(run_dir) / "pareto"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "search_traces.jsonl"

    def write(self, event: dict[str, Any] | Any) -> None:
        payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")) + "\n"
            )

    def emit_candidates(
        self,
        candidates: list[ParetoOrchestraCandidate],
        *,
        decision_id: str,
        decision_context: ParetoDecisionContext,
        preference_profile: PreferenceProfile,
        selected_hash: str | None,
        status: ParetoSelectionStatus | str,
        activated_revision_id: str | None = None,
    ) -> None:
        status_value = getattr(status, "value", status)
        for candidate in candidates:
            edits = []
            for edit in candidate.edits:
                if hasattr(edit, "model_dump"):
                    edits.append(edit.model_dump(mode="json"))
                elif isinstance(edit, dict):
                    edits.append(edit)
            self.write(
                OrchestraSearchTrace(
                    task_id=decision_context.task_id,
                    decision_id=decision_id,
                    decision_context=decision_context,
                    preference_profile=preference_profile,
                    candidate_id=candidate.candidate_id,
                    candidate_content_hash=candidate.content_hash,
                    edit_signature=candidate.edit_signature,
                    candidate_edits=edits,
                    feasibility_status="ok" if not candidate.validation_errors else "invalid",
                    validation_errors=list(candidate.validation_errors),
                    estimated_objectives=candidate.objectives,
                    selected=candidate.content_hash == selected_hash,
                    selection_status=status_value,
                    activated_revision_id=activated_revision_id,
                    pareto_status="selected" if candidate.content_hash == selected_hash else None,
                )
            )

    def emit_realization(
        self,
        *,
        candidate: ParetoOrchestraCandidate,
        decision,
        preference_profile: PreferenceProfile,
        status: ParetoSelectionStatus | str,
    ) -> None:
        status_value = getattr(status, "value", status)
        self.write(
            OrchestraSearchTrace(
                task_id=getattr(decision.context, "task_id", ""),
                decision_id=decision.decision_id,
                decision_context=decision.context,
                preference_profile=preference_profile,
                candidate_id=candidate.candidate_id,
                candidate_content_hash=candidate.content_hash,
                edit_signature=candidate.edit_signature,
                estimated_objectives=None,
                selected=True,
                selection_status=status_value,
                activated_revision_id=decision.activated_revision_id,
                realized_objectives=candidate.objectives,
                pareto_status="realized",
            )
        )
