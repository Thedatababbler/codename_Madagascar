"""A continuation starts from the best sample that passed, ties to the incumbent."""

from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.schemas import CandidateRecord, CandidateStatus


def _rec(cid, score, status=CandidateStatus.VALID, patch="diff"):
    r = CandidateRecord(candidate_id=cid, attempt_id=1, parent_graph_hash="h", graph_hash="g", edits=[])
    r.behaviour_score = score
    r.status = status
    r.patch = patch
    return r


def test_best_probe_wins():
    inc = _rec("inc", 0.579, CandidateStatus.COMMITTED, patch="")
    probes = [_rec("p1", 0.737), _rec("p2", 0.789)]
    assert FastLoopController._best_base(inc, probes).candidate_id == "p2"


def test_ties_and_missing_patch_go_to_incumbent():
    inc = _rec("inc", 0.8, CandidateStatus.COMMITTED, patch="")
    assert FastLoopController._best_base(inc, [_rec("p1", 0.8)]).candidate_id == "inc"
    assert FastLoopController._best_base(inc, [_rec("p1", 0.95, patch="")]).candidate_id == "inc"
    assert FastLoopController._best_base(inc, [_rec("p1", 0.95, CandidateStatus.REJECTED)]).candidate_id == "inc"
