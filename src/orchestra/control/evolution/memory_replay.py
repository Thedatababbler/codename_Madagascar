"""Memory replay (self-evolution spec §8): statistics over the ledger, never an implementer call.

* §8.1 error-class distribution of persistent failures with merge advice;
* §8.2 the shrinkage ranking score and the simulation of a new ranking on
  the historical searches where the chosen row actually ran;
* §8.3 the first-pass entries' prediction calibration (trigger -> actual
  error class), reported separately from their preventive effect;
* §8.4 the ledger rebuild after a test-author change: the mechanics with an
  injected scorer; no scorer is shipped in this stage.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

MERGE_ADVICE = {"E6": "E3", "E8": "E1"}


# --------------------------------------------------------------------------- §8.1

def class_distribution(records: Iterable[Mapping[str, Any]], *, min_class_samples: int = 5) -> dict[str, Any]:
    """Persistent-failure counts per error class over milestone or candidate records; sparse classes flagged."""
    counts: Counter[str] = Counter()
    milestones: dict[str, set[str]] = defaultdict(set)
    for r in records:
        key = f"{r.get('task_id')}:{r.get('milestone_id')}"
        actual = r.get("actual_error_classes")
        if isinstance(actual, Mapping) and actual:
            for cls, n in actual.items():
                counts[str(cls)] += int(n or 0)
                milestones[str(cls)].add(key)
            continue
        for cls in r.get("error_classes") or ():
            counts[str(cls)] += 1
            milestones[str(cls)].add(key)
    sparse = {c: n for c, n in counts.items() if n < min_class_samples}
    advice = {c: MERGE_ADVICE[c] for c in sparse if c in MERGE_ADVICE}
    return {
        "counts": dict(sorted(counts.items())),
        "milestones_per_class": {c: len(m) for c, m in sorted(milestones.items())},
        "sparse": sparse,
        "merge_advice": advice,
        "min_class_samples": min_class_samples,
    }


# --------------------------------------------------------------------------- §8.2

def shrinkage_score(n: int, mean_delta: float, *, n0: int = 3) -> float:
    """``n / (n + n0) * mean(delta_vs_R0)``: a row with few pairs is pulled toward R0 (score 0)."""
    if n <= 0:
        return 0.0
    return n / (n + n0) * mean_delta


@dataclass
class RowClassStats:
    row_id: str
    error_class: str
    deltas: list[float] = field(default_factory=list)
    costs: list[float] = field(default_factory=list)
    regressions: list[int] = field(default_factory=list)
    r0_regressions: list[int] = field(default_factory=list)
    record_ids: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.deltas)

    def score(self, n0: int) -> float:
        return shrinkage_score(self.n, statistics.fmean(self.deltas) if self.deltas else 0.0, n0=n0)

    def mean_cost(self) -> float:
        return statistics.fmean(self.costs) if self.costs else 0.0

    def mean_regressions(self) -> float:
        return statistics.fmean(self.regressions) if self.regressions else 0.0

    def mean_r0_regressions(self) -> float:
        return statistics.fmean(self.r0_regressions) if self.r0_regressions else 0.0

    def recent_score(self, k: int, n0: int) -> float:
        tail = self.deltas[-k:] if k > 0 else self.deltas
        return shrinkage_score(len(tail), statistics.fmean(tail) if tail else 0.0, n0=n0)


def _cost_of(rec: Mapping[str, Any]) -> float:
    cost = rec.get("cost") or {}
    usd = cost.get("usd")
    if usd:
        return float(usd)
    return float(cost.get("tokens") or 0) / 1_000_000.0 + float(cost.get("calls") or 0) * 0.0


def row_of(rec: Mapping[str, Any]) -> str:
    """The table row a record ran, without the swap-angle suffix."""
    return str(rec.get("row_id") or "").split("~", 1)[0]


def pair_statistics(
    records: Iterable[Mapping[str, Any]], *, training_only: bool = True,
) -> dict[tuple[str, str], RowClassStats]:
    """Row x error-class pairs from candidate records that carry ``delta_vs_R0`` (online and re-run alike)."""
    by_id = {}
    recs = []
    for r in records:
        if training_only and str(r.get("split") or "train") != "train":
            continue
        by_id[str(r.get("record_id"))] = r
        recs.append(r)
    stats: dict[tuple[str, str], RowClassStats] = {}
    for r in recs:
        row = row_of(r)
        if not row or r.get("candidate_kind") != "row" or r.get("delta_vs_R0") is None:
            continue
        r0 = by_id.get(str(r.get("paired_R0_record_id") or ""))
        classes = [str(c) for c in (r.get("error_classes") or ())] or ["*"]
        for cls in classes:
            s = stats.setdefault((row, cls), RowClassStats(row, cls))
            s.deltas.append(float(r["delta_vs_R0"]))
            s.costs.append(_cost_of(r))
            s.regressions.append(len(r.get("regressed") or []) + len(r.get("prior_regressions") or []))
            if r0 is not None:
                s.r0_regressions.append(len(r0.get("regressed") or []) + len(r0.get("prior_regressions") or []))
            s.record_ids.append(str(r.get("record_id")))
    return stats


def ranking_from_stats(stats: Mapping[tuple[str, str], RowClassStats], *, n0: int = 3) -> dict[str, dict[str, float]]:
    """``{error_class: {row_id: score}}`` as ``select_rows`` consumes it."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (row, cls), s in stats.items():
        out[cls][row] = round(s.score(n0), 4)
    return {c: dict(sorted(v.items())) for c, v in sorted(out.items())}


def _choose(ranking: Mapping[str, Mapping[str, float]], cls: str, rows_available: Iterable[str], stats) -> str | None:
    # ties break on mean cost; a row without any pair has unknown cost and sorts last
    scored = [(ranking.get(cls, {}).get(r, 0.0), stats[(r, cls)].mean_cost() if (r, cls) in stats else float("inf"), r)
              for r in rows_available]
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return scored[0][2]


def simulate_ranking(
    records: Iterable[Mapping[str, Any]], *, old_ranking: Mapping[str, Mapping[str, float]],
    new_ranking: Mapping[str, Mapping[str, float]], table_rows: Mapping[str, Iterable[str]], min_comparable: float = 0.3,
) -> dict[str, Any]:
    """§8.2: replay each historical search; take the row the new rule would pick when it actually ran there."""
    searches: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in records:
        if r.get("candidate_kind") == "row" and r.get("delta_vs_R0") is not None:
            searches[(str(r.get("task_id")), str(r.get("milestone_id")))].append(r)
    stats = pair_statistics(list(records), training_only=False)
    comparable = 0
    old_net = new_net = 0.0
    old_cost = new_cost = 0.0
    for _key, recs in searches.items():
        classes = sorted({str(c) for r in recs for c in (r.get("error_classes") or ())}) or ["*"]
        ran = {row_of(r): r for r in recs}
        cls = classes[0]
        pool = [row for row in table_rows.get(cls, ()) ] + [row for row in table_rows.get("*", ())]
        pick_new = _choose(new_ranking, cls, pool, stats)
        pick_old = _choose(old_ranking, cls, pool, stats)
        if pick_new in ran and pick_old in ran:
            comparable += 1
            new_net += float(ran[pick_new]["delta_vs_R0"])
            old_net += float(ran[pick_old]["delta_vs_R0"])
            new_cost += _cost_of(ran[pick_new])
            old_cost += _cost_of(ran[pick_old])
    total = len(searches)
    share = (comparable / total) if total else 0.0
    return {
        "searches": total,
        "comparable": comparable,
        "comparable_share": round(share, 3),
        "sufficient": share >= min_comparable and comparable > 0,
        "old": {"net_vs_R0": old_net, "cost": round(old_cost, 4)},
        "new": {"net_vs_R0": new_net, "cost": round(new_cost, 4)},
        "verdict": ("insufficient evidence" if not (share >= min_comparable and comparable > 0)
                    else "new rule better" if new_net > old_net else "new rule not better"),
    }


# --------------------------------------------------------------------------- §8.3

def f_calibration(
    milestone_records: Iterable[Mapping[str, Any]], entries: Iterable[Any], *, matched: Callable[[Mapping[str, Any], Any], bool],
) -> dict[str, Any]:
    """Per F entry: occurrence rate of its target classes on triggered vs untriggered milestones (prediction only)."""
    recs = [r for r in milestone_records if r.get("features")]
    out: dict[str, Any] = {}
    for e in entries:
        targets = set(getattr(e, "predicted_error_classes", ()) or ())
        if not targets:
            continue
        hit_t = hit_u = n_t = n_u = 0
        for r in recs:
            actual = set(r.get("error_classes") or ())
            fired = bool(matched(r["features"], e))
            has = bool(actual & targets)
            if fired:
                n_t += 1
                hit_t += int(has)
            else:
                n_u += 1
                hit_u += int(has)
        rate_t = hit_t / n_t if n_t else None
        rate_u = hit_u / n_u if n_u else None
        ratio = (rate_t / rate_u) if rate_t is not None and rate_u else None
        advice = ""
        if n_t + n_u < 10:
            advice = "too few milestones to calibrate"
        elif ratio is not None and ratio < 1.2:
            advice = "trigger barely separates its target classes; consider a stricter threshold"
        elif n_t == 0:
            advice = "never triggered; consider a looser threshold"
        out[getattr(e, "entry_id", str(e))] = {
            "targets": sorted(targets), "triggered": n_t, "untriggered": n_u,
            "rate_triggered": rate_t, "rate_untriggered": rate_u, "ratio": ratio, "advice": advice,
        }
    return out


# --------------------------------------------------------------------------- §8.4

def rebuild_ledger(
    records: Iterable[Mapping[str, Any]], *, new_suite_version: str,
    rescore: Callable[[Mapping[str, Any]], Mapping[str, str] | None],
) -> dict[str, list[dict[str, Any]]]:
    """Re-score every training record that kept a workspace with the new suite; archive the rest.

    ``rescore`` receives a record and returns per-case results under the new
    suite (or ``None`` when the workspace is gone). This stage ships the
    interface only: the caller supplies the scorer.
    """
    rebuilt: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []
    for r in records:
        if str(r.get("split") or "train") != "train" or not r.get("workspace_ref"):
            archived.append(dict(r, archived_reason="no workspace_ref" if not r.get("workspace_ref") else "not a training record"))
            continue
        per_case = rescore(r)
        if per_case is None:
            archived.append(dict(r, archived_reason="workspace unavailable"))
            continue
        rebuilt.append(dict(r, per_case_results=dict(per_case), suite_version=new_suite_version, rescored_from=r.get("suite_version")))
    return {"rebuilt": rebuilt, "archived": archived}


__all__ = [
    "MERGE_ADVICE", "RowClassStats", "class_distribution", "f_calibration", "pair_statistics", "ranking_from_stats",
    "rebuild_ledger", "row_of", "shrinkage_score", "simulate_ranking",
]
