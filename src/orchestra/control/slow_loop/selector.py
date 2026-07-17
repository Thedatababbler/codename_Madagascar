"""Deterministic Slow Loop candidate selector (not Pareto)."""

from __future__ import annotations

from collections.abc import Sequence

from orchestra.control.slow_loop.schemas import (
    GlobalCandidate,
    GlobalCandidateValidationStatus,
    GlobalObservation,
    PendingBackendAssignmentEdit,
)


class DeterministicGlobalCandidateSelector:
    def select(
        self,
        candidates: Sequence[GlobalCandidate],
        observation: GlobalObservation,
    ) -> GlobalCandidate | None:
        del observation
        valid = [
            c
            for c in candidates
            if c.validation_status is GlobalCandidateValidationStatus.VALID
            and c.rejection_reason is None
        ]
        if not valid:
            return None

        def key(c: GlobalCandidate) -> tuple:
            switches_backend = any(
                isinstance(e, PendingBackendAssignmentEdit) for e in c.edits
            )
            payload_tokens = sum(
                getattr(e, "contract", None).max_tokens
                if hasattr(e, "contract")
                else 0
                for e in c.edits
            )
            return (
                len(c.edits),
                1 if switches_backend else 0,
                payload_tokens,
                c.candidate_id,
            )

        return sorted(valid, key=key)[0]
