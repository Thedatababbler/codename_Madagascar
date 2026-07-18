"""Alternate models must come only from configured BackendModelPool."""

from __future__ import annotations

from orchestra.backends.catalog import KNOWN_BACKEND_CAPABILITIES
from orchestra.control.fast_loop.candidate_generator import RuleBasedLocalCandidateGenerator
from orchestra.control.fast_loop.diagnosis import diagnose_subtask_failure
from orchestra.control.fast_loop.schemas import BackendModelPool, FastLoopBudget
from orchestra.control.task_state import (
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec
from orchestra.ir.graph import load_graph

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def _diag():
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
        failure_message="FAILED assert",
    )
    return graph, diagnose_subtask_failure(subtask_state=sub, graph=graph)


def test_alternate_model_comes_only_from_configured_pool():
    graph, diagnosis = _diag()
    pools = {
        "codex_sdk": BackendModelPool(
            backend_id="codex_sdk",
            allowed_models=["gpt-4o-mini", "special-model-x"],
            fallback_order=["special-model-x"],
        )
    }
    gen = RuleBasedLocalCandidateGenerator(model_pools=pools)
    cands = gen.generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=3),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
    )
    model_cands = [c for c in cands if c.candidate_id == "cand_model"]
    assert model_cands
    edits = model_cands[0].edits
    override = next(e for e in edits if e.type == "model_override")
    assert override.model_name == "special-model-x"


def test_no_model_candidate_when_pool_has_no_alternative():
    graph, diagnosis = _diag()
    # Pool contains only the graph's current model → no alternate.
    from orchestra.ir.nodes import AgentNodeSpec

    agent = next(n for n in graph.nodes if isinstance(n, AgentNodeSpec))
    current = agent.model.name if agent.model else "gpt-4o-mini"
    pools = {
        "codex_sdk": BackendModelPool(
            backend_id="codex_sdk",
            allowed_models=[current],
        )
    }
    gen = RuleBasedLocalCandidateGenerator(model_pools=pools)
    cands = gen.generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=3),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
    )
    assert not any(c.candidate_id == "cand_model" for c in cands)
