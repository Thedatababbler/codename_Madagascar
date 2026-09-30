"""Stage 1 of the self-evolution spec: one commit rule for every candidate (§2.5)."""

from __future__ import annotations

from orchestra.control.fast_loop.acceptance import (
    ACCEPTANCE_KEY,
    PRIOR_SUITES_KEY,
    UnifiedAcceptanceSelector,
    case_sets,
    judge,
    judge_all,
    suite_conflicts,
)
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
    FastLoopBudget,
)

P = "../../ws/harness/m.spec_tests/test_a.py::"
A, B, C, D, F = (P + n for n in ("test_a", "test_b", "test_c", "test_d", "test_flaky"))
ALL = [A, B, C, D, F]


def _rec(cid, *, failed, passed=None, status=CandidateStatus.VALID, score=0.5, cost=1.0, **meta):
    passed = [t for t in ALL if t not in failed] if passed is None else passed
    return CandidateRecord(
        candidate_id=cid, attempt_id=1, graph_hash="g", parent_graph_hash="g", edits=[],
        status=status, behaviour_score=score, behaviour_failures=list(failed),
        behaviour_passed=list(passed), behaviour_total=len(ALL),
        cost=CostRecord(estimated_cost_usd=cost), metadata=dict(meta),
    )


def _incumbent(failed):
    return _rec("incumbent_first_pass", failed=failed, incumbent=True)


def test_case_sets_split_persistent_flaky_and_stable_pass() -> None:
    inc = _incumbent([A, B, F])
    probes = [_rec("p1", failed=[A, B]), _rec("p2", failed=[A, B, F])]
    sets = case_sets(inc, probes)
    assert sets.persistent == {"test_a.py::test_a", "test_a.py::test_b"}
    assert sets.flaky == {"test_a.py::test_flaky"}
    assert sets.stable_pass == {"test_a.py::test_c", "test_a.py::test_d"}


def test_fix_three_break_three_is_refused() -> None:
    inc = _incumbent([A, B, C])
    probes = [_rec("p1", failed=[A, B, C])]
    cand = _rec("cand", failed=[D, F, P + "test_e"], passed=[A, B, C])
    v = judge(cand, case_sets(inc, probes), inc)
    assert sorted(v.fixed) == ["test_a.py::test_a", "test_a.py::test_b", "test_a.py::test_c"]
    assert v.regressed == ["test_a.py::test_d", "test_a.py::test_flaky"]
    assert not v.accepted and "stably passing" in " ".join(v.reasons)


def test_a_flaky_case_is_not_a_regression() -> None:
    inc = _incumbent([A])
    probes = [_rec("p1", failed=[A, F])]
    cand = _rec("cand", failed=[F])
    v = judge(cand, case_sets(inc, probes), inc)
    assert v.fixed == ["test_a.py::test_a"] and v.regressed == [] and v.accepted


def test_a_candidate_that_fixes_nothing_is_refused_even_with_zero_regressions() -> None:
    inc = _incumbent([A])
    v = judge(_rec("cand", failed=[A]), case_sets(inc, []), inc)
    assert not v.accepted and "fixes no persistent failure" in v.reasons


def test_predecessor_regressions_block_acceptance() -> None:
    inc = _incumbent([A])
    cand = _rec("cand", failed=[])
    cand.metadata[PRIOR_SUITES_KEY] = {"m1": {"regressions": ["test_m1.py::test_x"], "ran": True}}
    verdicts, _ = judge_all([cand], inc, [])
    v = verdicts["cand"]
    assert v.fixed and not v.accepted and v.prior_regressions == ["test_m1.py::test_x"]


def test_gate_state_may_not_drop() -> None:
    inc = _incumbent([A])
    cand = _rec("cand", failed=[], status=CandidateStatus.HARNESS_FAILED)
    v = judge(cand, case_sets(inc, []), inc)
    assert not v.accepted and not v.gate_ok


def test_a_probe_that_beats_the_incumbent_is_accepted() -> None:
    """§2.1: probes are judged against the incumbent and the *other* probes."""
    inc = _incumbent([A, B])
    p1 = _rec("cand_feedback", failed=[A], persistence_phase=1, probe=True)
    p2 = _rec("cand_feedback_r2", failed=[A, B], persistence_phase=1, probe=True)
    verdicts, _ = judge_all([p1, p2], inc, [p1, p2])
    assert verdicts["cand_feedback"].accepted and verdicts["cand_feedback"].fixed == ["test_a.py::test_b"]
    assert not verdicts["cand_feedback_r2"].accepted


def test_suite_conflict_is_detected_and_both_cases_excluded() -> None:
    inc = _incumbent([A])
    probes = [_rec("p1", failed=[A])]
    r0 = _rec("cand_R0", failed=[B], candidate_kind="R0")          # fixes A, breaks B
    other = _rec("cand_row", failed=[B], candidate_kind="row")     # same trade
    clean = _rec("cand_clean", failed=[A])                          # fixes nothing
    first = {c.candidate_id: judge(c, case_sets(inc, probes), inc) for c in (r0, other)}
    assert suite_conflicts(list(first.values()), "cand_R0") == [("test_a.py::test_a", "test_a.py::test_b")]
    verdicts, conflicts = judge_all([r0, other, clean], inc, probes, r0_id="cand_R0")
    assert conflicts == [("test_a.py::test_a", "test_a.py::test_b")]
    # With A and B excluded neither R0 nor the row fixes or breaks anything.
    assert verdicts["cand_R0"].fixed == [] and verdicts["cand_R0"].regressed == []
    assert not verdicts["cand_R0"].accepted
    assert r0.metadata[ACCEPTANCE_KEY]["excluded"] == ["test_a.py::test_a", "test_a.py::test_b"]


def test_selector_prefers_net_fix_then_cost_and_declines_to_the_incumbent() -> None:
    inc = _incumbent([A, B, C])
    probes = [_rec("p1", failed=[A, B, C])]
    big = _rec("big", failed=[C], cost=5.0)          # fixes 2
    small = _rec("small", failed=[B, C], cost=1.0)   # fixes 1
    cheap2 = _rec("cheap2", failed=[C], cost=2.0)    # fixes 2, cheaper than big
    judge_all([big, small, cheap2], inc, probes)
    winner = UnifiedAcceptanceSelector().select([inc, big, small, cheap2], FastLoopBudget())
    assert winner is cheap2
    # Nothing accepted -> the incumbent is returned, which is how the search declines.
    nothing = _rec("nothing", failed=[A, B, C])
    judge_all([nothing], inc, probes)
    assert UnifiedAcceptanceSelector().select([inc, nothing], FastLoopBudget()) is inc


def test_records_without_passing_ids_fall_back_to_the_failure_union() -> None:
    inc = _rec("incumbent_first_pass", failed=[A], passed=[], incumbent=True)
    probes = [_rec("p1", failed=[A, F], passed=[])]
    sets = case_sets(inc, probes)
    assert sets.stable_pass is None
    good = judge(_rec("cand", failed=[F], passed=[]), sets, inc)
    bad = judge(_rec("cand2", failed=[B], passed=[]), sets, inc)
    assert good.accepted and good.regressed == []
    assert not bad.accepted and bad.regressed == ["test_a.py::test_b"]
