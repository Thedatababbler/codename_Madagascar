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
        self.estimated: dict[str, list[ParetoOrchestraCandidate]] = {}
        self.realized: dict[str, list[ParetoOrchestraCandidate]] = {}

    def entries(
        self, context_id: str, kind: ParetoEvaluationKind
    ) -> list[ParetoOrchestraCandidate]:
        source = self.realized if kind is ParetoEvaluationKind.REALIZED else self.estimated
        return list(source.get(context_id, []))

    def by_context(
        self, context_id: str, kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    ) -> list[ParetoOrchestraCandidate]:
        return self.entries(context_id, kind)

    def frontier(
        self, context_id: str, kind: ParetoEvaluationKind = ParetoEvaluationKind.ESTIMATED
    ) -> list[ParetoOrchestraCandidate]:
        return sorted(self.entries(context_id, kind), key=lambda c: c.content_hash)

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
        required = set(self.config.objectives)
        out = []
        for c in self.entries(context_id, kind):
            vals = c.objectives.values
            if any(
                name not in vals or not vals[name].available or vals[name].value is None
                for name in required
            ):
                out.append(c)
        return sorted(out, key=lambda c: c.content_hash)

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
        for source in (self.estimated, self.realized):
            for context_id, bucket in list(source.items()):
                source[context_id] = [c for c in bucket if c.content_hash != content_hash]

    def insert(
        self, candidate: ParetoOrchestraCandidate, kind: ParetoEvaluationKind | None = None
    ) -> bool:
        kind = kind or candidate.objectives.evaluation_kind
        source = self.realized if kind is ParetoEvaluationKind.REALIZED else self.estimated
        bucket = source.setdefault(candidate.context_id, [])
        if any(c.content_hash == candidate.content_hash for c in bucket):
            return False

        def _complete(item: ParetoOrchestraCandidate) -> bool:
            vals = item.objectives.values
            return all(
                name in vals and vals[name].available and vals[name].value is not None
                for name in self.config.objectives
            )

        if _complete(candidate):
            if any(
                _complete(c)
                and dominates(c, candidate, self.config.objectives, self.config.epsilon)
                for c in bucket
            ):
                return False
            bucket[:] = [
                c
                for c in bucket
                if not (
                    _complete(c)
                    and dominates(candidate, c, self.config.objectives, self.config.epsilon)
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
        remainder.sort(key=lambda c: (len(c.edits), c.communication_overhead, c.content_hash))
        bucket[:] = sorted(
            (keep + remainder[: max(0, maximum - len(keep))]), key=lambda c: c.content_hash
        )
