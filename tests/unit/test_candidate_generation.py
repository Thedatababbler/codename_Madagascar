"""Bounded deterministic candidate generation."""

from __future__ import annotations

from orchestra.backends.catalog import KNOWN_BACKEND_CAPABILITIES
from orchestra.control.fast_loop.candidate_generator import RuleBasedLocalCandidateGenerator
from orchestra.control.fast_loop.diagnosis import diagnose_subtask_failure
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.task_state import (
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec
from orchestra.ir.graph import load_graph

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def test_generator_bounds_to_k():
    graph = load_graph(GRAPH)
    sub = SubtaskState(
        spec=SubtaskSpec(
            subtask_id="s1",
            title="t",
            objective="o",
            keystone_harness_id="repository_test_harness",
            local_graph_template=GRAPH,
            budget=BudgetSpec(),
        ),
        status=SubtaskStatus.HARNESS_FAILED,
        failure_reason=SubtaskFailureReason.HARNESS,
        failure_message="assert add(1,1)==2 failed",
    )
    diagnosis = diagnose_subtask_failure(subtask_state=sub, graph=graph)
    gen = RuleBasedLocalCandidateGenerator()
    cands = gen.generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=3),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
    )
    assert 1 <= len(cands) <= 3
    assert all(c.session_policy.value == "fresh" for c in cands)
    assert all(c.parent_graph_hash == graph.content_hash for c in cands)


def test_infra_diagnosis_generates_no_candidates():
    graph = load_graph(GRAPH)
    sub = SubtaskState(
        spec=SubtaskSpec(
            subtask_id="s1",
            title="t",
            objective="o",
            keystone_harness_id="repository_test_harness",
            local_graph_template=GRAPH,
            budget=BudgetSpec(),
        ),
        status=SubtaskStatus.FAILED,
        failure_reason=SubtaskFailureReason.INFRA,
        failure_message="oom",
    )
    diagnosis = diagnose_subtask_failure(subtask_state=sub, graph=graph)
    cands = RuleBasedLocalCandidateGenerator().generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
    )
    assert cands == []
