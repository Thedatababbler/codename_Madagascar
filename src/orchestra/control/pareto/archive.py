"""Context-local, bounded archive of estimated and realized Pareto candidates."""

from __future__ import annotations

from orchestra.control.pareto.dominance import dominates
from orchestra.control.pareto.schemas import (
    ParetoConfig,
    ParetoEvaluationKind,
    ParetoOrchestraCandidate,
)


class ParetoArchive:
    def __init__(self, config: ParetoConfig | None = None) -> None:
        self.config = config or ParetoConfig()
        self.estimated_complete: dict[str, list[ParetoOrchestraCandidate]] = {}
        self.estimated_partial: dict[str, list[ParetoOrchestraCandidate]] = {}
        self.realized_complete: dict[str, list[ParetoOrchestraCandidate]] = {}
        self.realized_partial: dict[str, list[ParetoOrchestraCandidate]] = {}

    @property
    def estimated(self):
        return self.estimated_complete

    @property
    def realized(self):
        return self.realized_complete

    def _buckets(self, kind: ParetoEvaluationKind):
        return (
            (self.realized_complete, self.realized_partial)
            if kind is ParetoEvaluationKind.REALIZED
            else (self.estimated_complete, self.estimated_partial)
        )

    def _complete(self, item: ParetoOrchestraCandidate) -> bool:
        vals = item.objectives.values
        return not item.validation_errors and all(
            name in vals and vals[name].available and vals[name].value is not None
            for name in self.config.objectives
        )

    def entries(
        self, context_id: str, kind: ParetoEvaluationKind
    ) -> list[ParetoOrchestraCandidate]:
        complete, partial = self._buckets(kind)
        return list(complete.get(context_id, [])) + list(partial.get(context_id, []))

    def by_context(
        self, context_id: str, kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    ) -> list[ParetoOrchestraCandidate]:
        return self.entries(context_id, kind)

    def frontier(
        self, context_id: str, kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    ) -> list[ParetoOrchestraCandidate]:
        # Legacy diagnostic API; runtime selection must use complete_frontier().
        return sorted(self.entries(context_id, kind), key=lambda c: c.content_hash)

    def complete_frontier(
        self, context_id: str, kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    ) -> list[ParetoOrchestraCandidate]:
        complete, _ = self._buckets(kind)
        return sorted(complete.get(context_id, []), key=lambda c: c.content_hash)

    def dominated(
        self,
        context_id: str,
        candidate: ParetoOrchestraCandidate,
        kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED,
    ) -> bool:
        return any(
            dominates(c, candidate, self.config.objectives, self.config.epsilon)
            for c in self.entries(context_id, kind)
        )

    def partial(
        self, context_id: str, kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    ) -> list[ParetoOrchestraCandidate]:
        """Candidates missing at least one required objective."""
        return self.partial_candidates(context_id, kind)

    def partial_candidates(
        self, context_id: str, kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    ) -> list[ParetoOrchestraCandidate]:
        _, partial = self._buckets(kind)
        return sorted(partial.get(context_id, []), key=lambda c: c.content_hash)

    def diagnostic_entries(self, context_id: str, kind: ParetoEvaluationKind):
        return sorted(self.entries(context_id, kind), key=lambda c: c.content_hash)

    def best_extremes(
        self, context_id: str, kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    ) -> dict[str, ParetoOrchestraCandidate]:
        extremes: dict[str, ParetoOrchestraCandidate] = {}
        for name, direction in self.config.objectives.items():
            available = [
                c
                for c in self.entries(context_id, kind)
                if c.objectives.values.get(name) and c.objectives.values[name].available
            ]
            if not available:
                continue
            if direction.value == "maximize":
                extremes[name] = max(
                    available,
                    key=lambda c: (c.objectives.values[name].value, c.content_hash),
                )
            else:
                extremes[name] = min(
                    available,
                    key=lambda c: (c.objectives.values[name].value, c.content_hash),
                )
        return extremes

    def remove(self, content_hash: str) -> None:
        for source in (
            self.estimated_complete, self.estimated_partial,
            self.realized_complete, self.realized_partial,
        ):
            for context_id, bucket in list(source.items()):
                source[context_id] = [c for c in bucket if c.content_hash != content_hash]

    def insert(
        self, candidate: ParetoOrchestraCandidate, kind: ParetoEvaluationKind | None = None
    ) -> bool:
        kind = kind or candidate.objectives.evaluation_kind
        complete, partial = self._buckets(kind)
        if self._complete(candidate):
            bucket = complete.setdefault(candidate.context_id, [])
        else:
            bucket = partial.setdefault(candidate.context_id, [])
        if any(c.content_hash == candidate.content_hash for c in bucket):
            return False
        if self._complete(candidate):
            if any(
                dominates(c, candidate, self.config.objectives, self.config.epsilon)
                for c in bucket
            ):
                return False
            bucket[:] = [
                c
                for c in bucket
                if not (
                    dominates(candidate, c, self.config.objectives, self.config.epsilon)
                )
            ]
        bucket.append(candidate)
        self._prune(
            bucket,
            self.config.max_realized_archive_size
            if kind is ParetoEvaluationKind.REALIZED
            else self.config.max_estimated_archive_size,
        )
        return True

    def _prune(self, bucket: list[ParetoOrchestraCandidate], maximum: int) -> None:
        if len(bucket) <= maximum:
            bucket.sort(key=lambda c: c.content_hash)
            return
        extremes: set[str] = set()
        for name, direction in self.config.objectives.items():
            available = [
                c
                for c in bucket
                if c.objectives.values.get(name, None) and c.objectives.values[name].available
            ]
            if not available:
                continue
            if direction.value == "maximize":
                best = max(
                    available,
                    key=lambda c: (c.objectives.values[name].value, c.content_hash),
                )
            else:
                best = min(
                    available,
                    key=lambda c: (c.objectives.values[name].value, c.content_hash),
                )
            extremes.add(best.content_hash)
        keep = [c for c in bucket if c.content_hash in extremes]
        remainder = [c for c in bucket if c.content_hash not in extremes]
        # Preserve coverage across objective space instead of preferring small edits.
        def grid_key(c):
            cells = []
            for name in self.config.objectives:
                value = c.objectives.values.get(name)
                if value and value.available:
                    step = self.config.epsilon.get(name, 1.0)
                    cells.append(int((value.value or 0.0) / step))
                else:
                    cells.append(-1)
            return tuple(cells)
        chosen_grids = {grid_key(c) for c in keep}
        remainder.sort(key=lambda c: (grid_key(c) in chosen_grids, grid_key(c), c.content_hash))
        bucket[:] = sorted(
            (keep + remainder[: max(0, maximum - len(keep))]), key=lambda c: c.content_hash
        )
