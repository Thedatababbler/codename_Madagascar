"""Unified acceptance: one commit rule for probes, R0, playbook rows and resamples.

adamas_milestone_self_evolution_prompt.md §2.5. A candidate replaces the
incumbent only if it

1. fixes at least one persistent failure (failed in the incumbent and every
   probe, passes in the candidate);
2. fails none of the cases that passed stably (in the incumbent and every
   probe);
3. adds no failure to any committed predecessor's frozen suite;
4. does not lower the gate state (an incumbent that passed its gate is only
   replaced by a candidate that passes it).

Cases tagged ``suite_suspect`` are left out of 1 and 2. When R0 and at least
one other candidate both fix A while breaking a stably passing B, (A, B) is a
suite conflict: routed to the author and excluded from this milestone's
verdicts. Among accepted candidates the highest net fix wins, then the lowest
cost. The old ε quality threshold is kept as a log field only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from orchestra.control.fast_loop.persistence import failure_key
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    FastLoopBudget,
)

ACCEPTANCE_KEY = "acceptance"
PRIOR_SUITES_KEY = "prior_suites"
_SCORED = {CandidateStatus.VALID, CandidateStatus.COMMITTED, CandidateStatus.DISCARDED}


def _keys(names: Iterable[str] | None) -> set[str]:
    return {failure_key(n) for n in (names or [])}


def _scored(record: CandidateRecord) -> bool:
    return (
        record.status in _SCORED
        and record.behaviour_score is not None
        and not (record.behaviour_score == 0 and not record.behaviour_failures)
    )


@dataclass(frozen=True)
class CaseSets:
    """The three populations the rule is written in terms of, as failure keys."""

    persistent: frozenset[str]
    flaky: frozenset[str]
    #: Passed in the incumbent and every probe. ``None`` when no sample carries
    #: passing ids (records from before 2026-09-30): condition 2 then falls
    #: back to "not failed in any sample", which is the same set whenever every
    #: case was collected everywhere.
    stable_pass: frozenset[str] | None
    samples: int


def case_sets(incumbent: CandidateRecord, probes: Sequence[CandidateRecord]) -> CaseSets:
    samples = [r for r in (incumbent, *probes) if _scored(r)]
    if not samples:
        return CaseSets(frozenset(), frozenset(), None, 0)
    failed = [_keys(r.behaviour_failures) for r in samples]
    persistent = frozenset(set.intersection(*failed))
    flaky = frozenset(set.union(*failed) - persistent)
    passed = [_keys(r.behaviour_passed) for r in samples]
    stable = frozenset(set.intersection(*passed)) if all(passed) else None
    return CaseSets(persistent=persistent, flaky=flaky, stable_pass=stable, samples=len(samples))


@dataclass
class Verdict:
    candidate_id: str
    accepted: bool
    fixed: list[str] = field(default_factory=list)
    regressed: list[str] = field(default_factory=list)
    prior_regressions: list[str] = field(default_factory=list)
    gate_ok: bool = True
    reasons: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    #: how the candidate was (or could be) accepted: ``persistent_fix``,
    #: ``gate_recovery``, ``flaky_resolution`` (pending the suite re-check) or ``""``
    accept_path: str = ""
    #: flaky cases the candidate passes; the re-check must confirm them
    flaky_resolved: list[str] = field(default_factory=list)

    @property
    def net_fix(self) -> int:
        return len(self.fixed) - len(self.regressed)

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "fixed": list(self.fixed),
            "regressed": list(self.regressed),
            "prior_regressions": list(self.prior_regressions),
            "net_fix": self.net_fix,
            "gate_ok": self.gate_ok,
            "reasons": list(self.reasons),
            "excluded": list(self.excluded),
            "accept_path": self.accept_path,
            "flaky_resolved": list(self.flaky_resolved),
        }


def judge(
    candidate: CandidateRecord,
    sets: CaseSets,
    incumbent: CandidateRecord,
    *,
    prior_regressions: Iterable[str] = (),
    suspect: Iterable[str] = (),
) -> Verdict:
    """Apply the four conditions to one candidate. Never raises."""
    excluded = _keys(suspect)
    v = Verdict(candidate_id=candidate.candidate_id, accepted=False, excluded=sorted(excluded))
    if candidate.metadata.get("incumbent"):
        v.reasons.append("incumbent")
        return v
    if not _scored(candidate):
        v.gate_ok = False
        v.reasons.append("not scored (no gate result or an empty zero)")
        return v
    cand_failed = _keys(candidate.behaviour_failures)
    cand_passed = _keys(candidate.behaviour_passed)
    persistent = sets.persistent - excluded
    # 1. fixed persistent failures
    if cand_passed:
        fixed = persistent & cand_passed
    else:
        fixed = {k for k in persistent if k not in cand_failed}
    v.fixed = sorted(fixed)
    # 2. stably passing cases the candidate fails
    if sets.stable_pass is not None:
        stable = sets.stable_pass - excluded
        regressed = cand_failed & stable
    else:
        sampled_failures = sets.persistent | sets.flaky
        regressed = {k for k in cand_failed if k not in sampled_failures and k not in excluded}
    v.regressed = sorted(regressed)
    # 3. predecessors' frozen suites
    v.prior_regressions = sorted(_keys(prior_regressions))
    # 4. gate state
    incumbent_gate = incumbent.status in (CandidateStatus.VALID, CandidateStatus.COMMITTED)
    cand_gate = candidate.status in (CandidateStatus.VALID, CandidateStatus.COMMITTED)
    v.gate_ok = cand_gate or not incumbent_gate
    if not incumbent_gate and cand_gate and not v.fixed:
        # The incumbent failed its gate on a stage the behaviour suite does not
        # see (imports, contracts, the dataset's check tests): a candidate that
        # passes the whole gate is the fix, even with no persistent case to name.
        v.fixed = ["<gate recovered>"]
        v.accept_path = "gate_recovery"
        v.reasons.append("gate recovered")
    elif v.fixed:
        v.accept_path = "persistent_fix"
    if not v.fixed and not persistent and sets.flaky:
        # Nothing persistent to fix: the incumbent and the probes disagree only
        # on flaky cases. Two samples of one design are a best-of-N, so a
        # candidate that fails strictly fewer of the sampled failures than the
        # incumbent may still be the better package -- if a suite re-check
        # (no agent) confirms that the cases are stable on it, not lucky.
        inc_failed = _keys(incumbent.behaviour_failures)
        resolved = sorted((sets.flaky - excluded) & inc_failed - cand_failed)
        if resolved and len(cand_failed - excluded) < len(inc_failed - excluded):
            v.flaky_resolved = resolved
            v.accept_path = "flaky_resolution"
            v.reasons.append(f"flaky only: resolves {len(resolved)} flaky case(s); suite re-check required")
    if not v.fixed:
        v.reasons.append("fixes no persistent failure")
    if v.regressed:
        v.reasons.append(f"fails {len(v.regressed)} stably passing case(s)")
    if v.prior_regressions:
        v.reasons.append(f"breaks {len(v.prior_regressions)} case(s) of a committed predecessor")
    if not v.gate_ok:
        v.reasons.append("gate state below the incumbent's")
    v.accepted = bool(v.fixed) and not v.regressed and not v.prior_regressions and v.gate_ok
    if not v.accepted and v.accept_path != "flaky_resolution":
        v.accept_path = ""
    return v


def confirm_flaky_resolution(verdict: Verdict, runs: Sequence[Iterable[str]], *, incumbent_failed: Iterable[str],
                             stable_pass: Iterable[str] | None) -> tuple[bool, list[str]]:
    """Settle a ``flaky_resolution`` verdict from the re-check runs' failure sets.

    Every run must fail strictly fewer of the sampled cases than the incumbent
    did, never one of the incumbent's stably passing cases, and must pass every
    case the verdict counts as resolved. Returns ``(confirmed, suspect)``: the
    cases that still flipped go to the author as ``suite_suspect``.
    """
    inc_failed = _keys(incumbent_failed)
    stable = _keys(stable_pass) if stable_pass is not None else set()
    resolved = set(verdict.flaky_resolved)
    suspect: set[str] = set()
    ok = bool(runs)
    for run in runs:
        failed = _keys(run)
        if len(failed) >= len(inc_failed) or (failed & stable) or (failed & resolved):
            ok = False
            suspect |= (failed & resolved)
    if ok:
        verdict.accepted = not verdict.regressed and not verdict.prior_regressions and verdict.gate_ok
        verdict.fixed = sorted(resolved)
        verdict.reasons = [r for r in verdict.reasons if not r.startswith("fixes no persistent") and not r.startswith("flaky only")]
        verdict.reasons.append(f"flaky resolution confirmed by {len(runs)} suite re-run(s)")
    else:
        verdict.accepted = False
        verdict.accept_path = ""
        verdict.reasons.append("flaky resolution not confirmed: the cases flip on the candidate too")
    return ok, sorted(suspect)


def suite_conflicts(verdicts: Sequence[Verdict], r0_id: str) -> list[tuple[str, str]]:
    """(A, B) pairs where fixing A costs stably-passing B in R0 *and* another candidate."""
    r0 = next((v for v in verdicts if v.candidate_id == r0_id), None)
    if r0 is None:
        return []
    pairs: list[tuple[str, str]] = []
    others = [v for v in verdicts if v.candidate_id != r0_id]
    for a in r0.fixed:
        for b in r0.regressed:
            if any(a in o.fixed and b in o.regressed for o in others):
                pairs.append((a, b))
    return pairs


def judge_all(
    candidates: Sequence[CandidateRecord],
    incumbent: CandidateRecord,
    probes: Sequence[CandidateRecord],
    *,
    r0_id: str = "",
    suspect: Iterable[str] = (),
) -> tuple[dict[str, Verdict], list[tuple[str, str]]]:
    """Judge every executed candidate, then re-judge with suite conflicts excluded.

    Prior-suite regressions are read from ``record.metadata["prior_suites"]``
    (written by the controller's diagnostic re-run) so this stays pure.
    """
    sets = case_sets(incumbent, probes)
    exclude = set(_keys(suspect))
    probe_ids = {p.candidate_id for p in probes}

    def sets_for(c: CandidateRecord) -> CaseSets:
        # A probe is judged against the incumbent and the *other* probes: it is
        # one of the samples the persistent set is intersected over, so judged
        # against sets that include itself it could never fix anything, and
        # §2.1 says a probe that beats the incumbent becomes the incumbent.
        if c.candidate_id in probe_ids:
            return case_sets(incumbent, [p for p in probes if p.candidate_id != c.candidate_id])
        return sets

    def run(excl: set[str]) -> dict[str, Verdict]:
        out: dict[str, Verdict] = {}
        for c in candidates:
            prior = []
            for entry in (c.metadata.get(PRIOR_SUITES_KEY) or {}).values():
                prior.extend(entry.get("regressions") or [])
            out[c.candidate_id] = judge(c, sets_for(c), incumbent, prior_regressions=prior, suspect=excl)
        return out

    verdicts = run(exclude)
    conflicts = suite_conflicts(list(verdicts.values()), r0_id) if r0_id else []
    if conflicts:
        for a, b in conflicts:
            exclude.update((a, b))
        verdicts = run(exclude)
    for c in candidates:
        v = verdicts.get(c.candidate_id)
        if v is not None:
            c.metadata[ACCEPTANCE_KEY] = v.to_dict()
    return verdicts, conflicts


class UnifiedAcceptanceSelector:
    """Pick the accepted candidate that leaves the fewest failures, else the incumbent.

    Reads the verdicts ``judge_all`` stored on each record; a record without
    one (never judged) is not selectable. ``net_fix`` is not comparable across
    candidates: a probe is judged leave-one-out against a larger persistent
    set than a repair candidate, so its count comes out higher for the same
    package. The common yardstick is the candidate's own remaining failure
    count on the frozen suite; ``net_fix`` breaks ties, then cost. (tinydb
    2026-10-01: a probe failing one case outranked R0 passing every case.)
    Returning the incumbent is how the controller learns the search declined,
    exactly as with the Pareto selector.
    """

    def __init__(self) -> None:
        self.last_frontier: list[str] = []
        self.last_rule: str = "unified_acceptance"

    def select(
        self, candidates: Sequence[CandidateRecord], budget: FastLoopBudget
    ) -> CandidateRecord | None:
        del budget
        judged = [c for c in candidates if ACCEPTANCE_KEY in c.metadata]
        if not judged:
            # No incumbent record (the first pass failed its gate, so there is
            # nothing to judge against): a candidate that passes the gate is the
            # improvement. Fewest remaining failures, then score, then cost.
            # (simplejwt 2026-10-01: a 0.96 gate-passing candidate was declined.)
            valid = [c for c in candidates if c.status is CandidateStatus.VALID and not c.metadata.get("incumbent")]
            self.last_rule = "unified_acceptance:gate_recovery"
            self.last_frontier = [c.candidate_id for c in valid]
            if not valid:
                return next((c for c in candidates if c.metadata.get("incumbent")), None)
            valid.sort(key=lambda c: (len(_keys(c.behaviour_failures)), -float(c.behaviour_score or 0.0),
                                      float(c.cost.estimated_cost_usd or 0.0), c.candidate_id))
            return valid[0]
        self.last_rule = "unified_acceptance"
        accepted = [
            c for c in candidates
            if (c.metadata.get(ACCEPTANCE_KEY) or {}).get("accepted")
            and c.status is CandidateStatus.VALID
        ]
        self.last_frontier = [c.candidate_id for c in accepted]
        if accepted:
            accepted.sort(
                key=lambda c: (
                    len(_keys(c.behaviour_failures)),
                    -int((c.metadata.get(ACCEPTANCE_KEY) or {}).get("net_fix") or 0),
                    float(c.cost.estimated_cost_usd or 0.0),
                    c.candidate_id,
                )
            )
            for c in accepted:
                (c.metadata.get(ACCEPTANCE_KEY) or {})["remaining_failures"] = len(_keys(c.behaviour_failures))
            return accepted[0]
        incumbent = next((c for c in candidates if c.metadata.get("incumbent")), None)
        return incumbent


__all__ = [
    "ACCEPTANCE_KEY",
    "PRIOR_SUITES_KEY",
    "CaseSets",
    "UnifiedAcceptanceSelector",
    "Verdict",
    "case_sets",
    "confirm_flaky_resolution",
    "judge",
    "judge_all",
    "suite_conflicts",
]
