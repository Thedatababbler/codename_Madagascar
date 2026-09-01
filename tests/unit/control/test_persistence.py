"""Persistent failures are what fail every time; the diagnosis acts on those."""

from __future__ import annotations

import json

import pytest

from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.llm_diagnosis import (
    DiagnosisCompletion,
    DiagnosisConfig,
    refine_diagnosis,
)
from orchestra.control.fast_loop.persistence import (
    DEFAULT_EDITING_ROLE,
    apply_role_floor,
    default_role,
    failure_key,
    persistence_ledger,
    persistent_failures,
    rule_based_roles,
)
from orchestra.control.fast_loop.playbook_generator import PlaybookCandidateGenerator
from orchestra.control.fast_loop.playbooks import SearchReason
from orchestra.control.fast_loop.quality_trigger import (
    build_incumbent_record,
    quality_search_diagnosis,
)
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    FailureDiagnosis,
    FastLoopBudget,
)
from orchestra.control.task_state import (
    SubtaskAttempt,
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import NodeKind
from orchestra.roles.pool import load_role_pool

# Two workspaces, two path prefixes, one test.
A = "../../ws_a/harness/m.spec_tests/test_x.py::test_fetch_uses_uid_keys"
B = "../../ws_b/harness/m.spec_tests/test_x.py::test_fetch_uses_uid_keys"
FLAKY_A = "../../ws_a/harness/m.spec_tests/test_y.py::test_normalise_search_criteria"
BROKEN = "../../ws_c/harness/m.spec_tests/test_z.py::test_something_only_a_broken_repo_fails"


def _pool():
    return load_role_pool("configs/roles")


def _sample(cid: str, failures: list[str], *, status=CandidateStatus.VALID, score=0.7):
    return CandidateRecord(
        candidate_id=cid,
        attempt_id=1,
        graph_hash="g",
        parent_graph_hash="g",
        edits=[],
        status=status,
        behaviour_score=score,
        behaviour_failures=failures,
    )


def test_failure_key_ignores_the_workspace_prefix() -> None:
    assert failure_key(A) == failure_key(B) == "test_x.py::test_fetch_uses_uid_keys"


def test_intersection_separates_persistent_from_flaky() -> None:
    summary = persistent_failures(
        [
            _sample("incumbent", [A, FLAKY_A]),
            _sample("r1", [B]),
            _sample("r2", [B, FLAKY_A]),
        ]
    )
    assert summary.samples == 3
    assert [failure_key(n) for n in summary.persistent] == ["test_x.py::test_fetch_uses_uid_keys"]
    assert [failure_key(n) for n in summary.flaky] == ["test_y.py::test_normalise_search_criteria"]
    # The persistent name is reported in the first sample's spelling, so a
    # prompt built from it points at a real path.
    assert summary.persistent == [A]


def test_a_sample_that_failed_its_gate_is_not_a_sample() -> None:
    """A broken repository's failure list says nothing about the shipped one."""
    summary = persistent_failures(
        [
            _sample("incumbent", [A]),
            _sample("r1", [B]),
            _sample("broken", [BROKEN], status=CandidateStatus.HARNESS_FAILED, score=0.1),
        ]
    )
    assert summary.samples == 2
    assert summary.persistent == [A]


def test_rules_fire_only_on_unambiguous_evidence() -> None:
    assert rule_based_roles([], "imports")[0] == "dependency_resolver"
    assert rule_based_roles(["t.py::test_public_surface_reexports_client"], "spec_tests")[0] == "integrator"
    assert rule_based_roles(["t.py::test_empty_mailbox_returns_none"], "spec_tests")[0] == "edge_case_hardener"
    # Mixed or semantic failures are the residue the model is for.
    assert rule_based_roles(["t.py::test_fetch_uses_uid_keys", "t.py::test_empty_mailbox"], "spec_tests") is None
    assert rule_based_roles(["t.py::test_fetch_uses_uid_keys"], "spec_tests") is None


def test_role_floor_then_default_and_the_source_is_always_recorded() -> None:
    pool = _pool()
    base = FailureDiagnosis(
        reason=SubtaskFailureReason.HARNESS,
        retryable=True,
        concise_feedback="x",
        furthest_stage="spec_tests",
        behaviour_failures=["t.py::test_fetch_uses_uid_keys"],
    )
    untouched = apply_role_floor(base, pool)
    assert untouched.recommended_role == ""
    settled = default_role(untouched, pool)
    assert settled.recommended_role == DEFAULT_EDITING_ROLE
    assert settled.role_source == "default"

    ruled = apply_role_floor(base.model_copy(update={"furthest_stage": "imports"}), pool)
    assert ruled.recommended_role == "dependency_resolver"
    assert ruled.role_source.startswith("rule:")
    # The default never overrides a rule.
    assert default_role(ruled, pool).role_source.startswith("rule:")


def test_ledger_scores_what_the_candidate_did_about_the_persistent_set() -> None:
    persistent = [A, "../../ws_a/harness/m.spec_tests/test_y.py::test_search_returns_searchids"]
    after = _sample("cand", ["../../ws_z/harness/m.spec_tests/test_y.py::test_search_returns_searchids"])
    ledger = persistence_ledger(after, persistent)
    assert ledger["persistent_total"] == 2
    assert ledger["persistent_fixed_count"] == 1
    assert [failure_key(n) for n in ledger["persistent_fixed"]] == ["test_x.py::test_fetch_uses_uid_keys"]


# --- the LLM side: closed output space and the consistency guard -------------

GRAPH = "configs/graphs/codex_single_implementer.yaml"


class _Client:
    def __init__(self, content: str) -> None:
        self.content = content

    def complete(self, *, model: str, messages: list[dict[str, str]]) -> DiagnosisCompletion:
        self.messages = messages
        return DiagnosisCompletion(content=self.content, prompt_tokens=5, completion_tokens=5, model=model)


def _sub() -> SubtaskState:
    return SubtaskState(
        spec=SubtaskSpec(
            subtask_id="s1",
            title="t",
            objective="o",
            dependencies=[],
            keystone_harness_id="repository_test_harness",
            local_graph_template=GRAPH,
            budget=BudgetSpec(max_llm_calls=1, max_steps=1, timeout_seconds=60),
        ),
        status=SubtaskStatus.COMMITTED,
        attempts=[SubtaskAttempt(attempt_id=1, status=SubtaskStatus.COMMITTED, metadata={})],
    )


def _quality_lookup(**overrides) -> FailureDiagnosis:
    payload = dict(
        reason=SubtaskFailureReason.HARNESS,
        retryable=True,
        concise_feedback="The acceptance gate passed, so this milestone is safe to build on",
        furthest_stage="spec_tests",
        behaviour_failures=["t.py::test_fetch_uses_uid_keys"],
        behaviour_total=23,
        behaviour_passed=18,
        persistence_samples=3,
    )
    payload.update(overrides)
    return FailureDiagnosis(**payload)


def _reply(**fields) -> str:
    base = {
        "failure_class": "functional",
        "confidence": 0.9,
        "target_node_id": "",
        "recommended_role": "",
        "recommended_reviewer": "",
        "rationale": "r",
        "evidence": [],
    }
    base.update(fields)
    return json.dumps(base)


def test_a_role_outside_the_pool_or_of_the_wrong_kind_is_dropped_not_trusted() -> None:
    graph = load_graph(GRAPH)
    cfg = DiagnosisConfig(mode="llm", min_confidence=0.5)
    # A read-only role offered as the writer, and an invented reviewer.
    client = _Client(_reply(recommended_role="spec_auditor", recommended_reviewer="oracle"))
    refined, call = refine_diagnosis(
        lookup=_quality_lookup(), graph=graph, subtask_state=_sub(), config=cfg, client=client
    )
    assert call.applied
    assert refined.recommended_role == ""
    assert refined.recommended_reviewer == ""
    assert not call.role_applied
    # The right kinds survive, and the source says who chose.
    client = _Client(_reply(recommended_role="implementer", recommended_reviewer="spec_auditor"))
    refined, call = refine_diagnosis(
        lookup=_quality_lookup(), graph=graph, subtask_state=_sub(), config=cfg, client=client
    )
    assert (refined.recommended_role, refined.recommended_reviewer) == ("implementer", "spec_auditor")
    assert refined.role_source == "llm" and call.role_applied


def test_the_prompt_carries_the_persistent_set_and_the_role_pool() -> None:
    graph = load_graph(GRAPH)
    client = _Client(_reply())
    refine_diagnosis(
        lookup=_quality_lookup(),
        graph=graph,
        subtask_state=_sub(),
        config=DiagnosisConfig(mode="llm"),
        client=client,
    )
    user = client.messages[1]["content"]
    assert "Persistent failures" in user and "3 independent attempts" in user
    assert "EDITING roles" in user and "implementer" in user
    assert "READ-ONLY roles" in user and "spec_auditor" in user


def test_budget_claimed_on_a_run_that_reached_the_tests_is_rejected() -> None:
    """The evidence contradicts the class, so the class is not applied."""
    graph = load_graph(GRAPH)
    client = _Client(_reply(failure_class="budget", recommended_role="implementer"))
    refined, call = refine_diagnosis(
        lookup=_quality_lookup(), graph=graph, subtask_state=_sub(), config=DiagnosisConfig(mode="llm"), client=client
    )
    assert not call.applied
    assert call.fallback_reason == "inconsistent:budget_without_signal"
    assert refined.recommended_role == ""


# --- the playbook side: the diagnosed role fills the slot ---------------------


def _test_first():
    import tempfile
    from pathlib import Path

    from orchestra.realbench.milestone_planner import parse_plan_payload
    from orchestra.realbench.subgraph_builder import (
        materialize_milestone_subgraph,
        prepare_generated_root,
    )

    plan = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "m_tf",
                    "title": "T",
                    "objective": "build",
                    "risk_rationale": "r",
                    "gate_level": "implementation",
                    "template_id": "test_first",
                    "acceptance": {"criteria": ["ok"]},
                    "agents": [
                        {"slot": "test_author", "role": "test_author", "mandate": "a"},
                        {"slot": "builder", "role": "implementer", "mandate": "b"},
                        {"slot": "repairer", "role": "gate_repairer", "mandate": "c"},
                    ],
                }
            ]
        },
        max_agents=4,
    )
    root = prepare_generated_root(Path(tempfile.mkdtemp()), base_contracts_dir="configs/contracts")
    path, _ = materialize_milestone_subgraph(
        generated_root=root,
        milestone=plan.milestones[0],
        agent_backend="codex_sdk",
        harness_command=["python", "check.py", "--spec-tests", "/tmp/frozen"],
    )
    return load_graph(path)


def _roles(graph):
    pool = _pool()
    return [
        pool.role_for_node_id(n.node_id).role_id
        for n in graph.nodes
        if n.node_kind is NodeKind.AGENT and pool.role_for_node_id(n.node_id)
    ]


def test_the_improve_row_takes_its_specialist_from_the_diagnosis() -> None:
    graph = _test_first()
    incumbent = build_incumbent_record(
        attempt_id=1,
        graph_hash=graph.content_hash,
        harness_score=0.9,
        behaviour_score=0.7,
        behaviour_failures=["t.py::test_fetch_uses_uid_keys"],
        behaviour_total=23,
        furthest_stage="spec_tests",
    )
    diagnosis = quality_search_diagnosis(incumbent, graph).model_copy(
        update={"recommended_role": "implementer", "recommended_reviewer": "spec_auditor",
                "role_source": "llm", "persistence_samples": 3}
    )
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.QUALITY,
    )
    improved = next(c for c in built if c.playbook_id == "pb_tf_q_improve_after_gate")
    assert improved.plan_recompile.slots["improver"] == "implementer"
    assert "edge_case_hardener" not in _roles(improved.graph)
    diagnosed = next(c for c in built if c.playbook_id == "pb_tf_q_diagnose_then_improve")
    assert diagnosed.plan_recompile.slots["critic"] == "spec_auditor"
    assert diagnosed.plan_recompile.slots["improver"] == "implementer"
    # And the prompt says the list is the persistent set, not one attempt's.
    text = " ".join(e.feedback for e in improved.edits if e.type == "prompt_feedback")
    assert "every one of 3 independent attempts" in text


def test_a_recommended_role_the_slot_cannot_hold_is_ignored_not_fatal() -> None:
    """A read-only role in the improver slot would be rejected by the recompile;
    the binding must fall back to the row's default instead of losing the candidate."""
    graph = _test_first()
    incumbent = build_incumbent_record(
        attempt_id=1, graph_hash=graph.content_hash, harness_score=0.9,
        behaviour_score=0.7, behaviour_failures=["t.py::test_a"], furthest_stage="spec_tests",
    )
    diagnosis = quality_search_diagnosis(incumbent, graph).model_copy(
        update={"recommended_role": "spec_auditor", "role_source": "llm"}
    )
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph, diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=99),
        # Real capabilities, so the only thing that could reject it is the recompile.
        capabilities={"codex_sdk": CodexSDKBackend().capabilities},
        search_reason=SearchReason.QUALITY,
    )
    improved = next(c for c in built if c.playbook_id == "pb_tf_q_improve_after_gate")
    assert not improved.compatibility_rejected
    assert improved.plan_recompile.slots["improver"] == "edge_case_hardener"


def test_the_four_search_modes_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        FastLoopController(
            runtime=None, artifact_store=None, task_checkpoint_store=None,
            anchor_search=True, persistence_search=True,
        )
