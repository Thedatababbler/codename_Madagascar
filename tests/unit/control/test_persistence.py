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
        budget=FastLoopBudget(max_candidates=4, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.QUALITY,
    )
    continued = next(c for c in built if c.playbook_id == "pb_q_continue_improve")
    assert continued.continue_from_incumbent
    assert continued.plan_recompile.slots["improver"] == "implementer"
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
        budget=FastLoopBudget(max_candidates=3, max_total_backend_calls=99),
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


# --- the failure table takes its roles from the diagnosis too ---------------


def _solo_graph():
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
                    "milestone_id": "m_solo",
                    "title": "T",
                    "objective": "build",
                    "risk_rationale": "r",
                    "gate_level": "implementation",
                    "template_id": "solo",
                    "acceptance": {"criteria": ["ok"]},
                    "agents": [{"slot": "author", "role": "implementer", "mandate": "a"}],
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


def _failure(graph, **overrides) -> FailureDiagnosis:
    author = next(n.node_id for n in graph.nodes if n.node_kind is NodeKind.AGENT)
    payload = dict(
        reason=SubtaskFailureReason.HARNESS,
        retryable=True,
        concise_feedback="FAIL spec_tests: 2 failed",
        furthest_stage="spec_tests",
        behaviour_failures=["t.py::test_fetch_uses_uid_keys", "t.py::test_search_returns_ids"],
        primary_failed_node_id=author,
        failed_node_ids=[author],
    )
    payload.update(overrides)
    return FailureDiagnosis(**payload)


def test_failure_shape_rows_take_their_pair_from_the_diagnosis() -> None:
    """A solo milestone recompiled for repair gets the writer *and* the reader the
    failure calls for, and both are told the names -- not a constant repairer
    reading only the new shape's own gate report."""
    graph = _solo_graph()
    # The public-surface pair from the rule floor; both target templates'
    # writer slots accept `integrator`, so the same diagnosis seats it in each.
    diagnosis = _failure(
        graph,
        recommended_role="integrator",
        recommended_reviewer="contract_critic",
        role_source="rule:names:public_surface",
    )
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=4, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.FAILURE,
    )
    by_id = {c.playbook_id: c for c in built}
    gate = by_id["pb_solo_to_gate_repair"]
    assert gate.plan_recompile.slots["repairer"] == "integrator"
    assert any(
        e.type == "prompt_feedback" and "test_fetch_uses_uid_keys" in e.feedback
        for e in gate.edits
    )
    review = by_id["pb_solo_to_review_fix"]
    assert review.plan_recompile.slots["reviewer"] == "contract_critic"
    assert review.plan_recompile.slots["fixer"] == "integrator"
    nodes_with_names = {
        e.node_id for e in review.edits if e.type == "prompt_feedback" and "test_search" in e.feedback
    }
    assert len(nodes_with_names) == 2, "both the reviewer and the fixer are told the names"


def test_a_shape_row_still_applies_when_the_gate_named_nothing() -> None:
    """A compile failure names no test. The shape change is the point of the
    row; the list is a bonus that binds when present, so the candidate must
    still be drafted -- with the dependency resolver the rules chose."""
    graph = _solo_graph()
    diagnosis = _failure(
        graph,
        concise_feedback="compile failed",
        furthest_stage="compile",
        behaviour_failures=[],
        recommended_role="dependency_resolver",
        role_source="rule:stage:imports",
        failure_class="functional",
    )
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(max_candidates=4, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.FAILURE,
    )
    ids = [c.playbook_id for c in built]
    assert "pb_solo_to_gate_repair" in ids
    gate = next(c for c in built if c.playbook_id == "pb_solo_to_gate_repair")
    assert not gate.compatibility_rejected or "capabilities" in (gate.rejection_message or "")
    assert gate.plan_recompile.slots["repairer"] == "dependency_resolver"
    assert not any(e.type == "prompt_feedback" for e in gate.edits)


def test_default_pairs_follow_the_search_reason_and_fill_only_gaps() -> None:
    pool = _pool()
    base = FailureDiagnosis(
        reason=SubtaskFailureReason.HARNESS, retryable=True, concise_feedback="x",
        furthest_stage="spec_tests", behaviour_failures=["t.py::test_a"],
    )
    q = default_role(base, pool, "quality")
    assert (q.recommended_role, q.recommended_reviewer) == ("implementer", "spec_auditor")
    f = default_role(base, pool, "failure")
    assert (f.recommended_role, f.recommended_reviewer) == ("gate_repairer", "behaviour_critic")
    # A writer already chosen keeps it; only the missing reviewer is paired in.
    half = base.model_copy(update={"recommended_role": "integrator", "role_source": "llm"})
    paired = default_role(half, pool, "quality")
    assert paired.recommended_role == "integrator"
    assert paired.recommended_reviewer == "spec_auditor"
    assert paired.role_source == "llm"


def test_recommended_shape_is_chosen_from_the_menu_or_dropped() -> None:
    """The model picks among prepared recompilations; an invented topology id
    is discarded, and the prompt carries the milestone and the menu."""
    graph = _test_first()
    cfg = DiagnosisConfig(mode="llm", min_confidence=0.5)
    client = _Client(_reply(recommended_shape="my_clever_new_graph"))
    refined, call = refine_diagnosis(
        lookup=_quality_lookup(), graph=graph, subtask_state=_sub(),
        config=cfg, client=client, search_reason="quality",
    )
    assert call.applied and refined.recommended_shape == ""
    user = client.messages[1]["content"]
    assert "# Milestone" in user
    assert "# Shape options" in user and "test_first_improve" in user

    client = _Client(_reply(recommended_shape="test_first_quality_diagnosed"))
    refined, _ = refine_diagnosis(
        lookup=_quality_lookup(), graph=graph, subtask_state=_sub(),
        config=cfg, client=client, search_reason="quality",
    )
    assert refined.recommended_shape == "test_first_quality_diagnosed"


def test_the_generator_moves_a_spent_row_to_the_back() -> None:
    graph = _test_first()
    incumbent = build_incumbent_record(
        attempt_id=1, graph_hash=graph.content_hash, harness_score=0.9,
        behaviour_score=0.7, behaviour_failures=["t.py::test_a"], furthest_stage="spec_tests",
    )
    built = PlaybookCandidateGenerator(role_pool=_pool()).generate(
        graph=graph,
        diagnosis=quality_search_diagnosis(incumbent, graph),
        budget=FastLoopBudget(max_candidates=2, max_total_backend_calls=99),
        capabilities={},
        search_reason=SearchReason.QUALITY,
        history=["pb_q_continue_improve"],
    )
    # k=2 leaves one playbook slot; the spent continuation row yields it.
    assert [c.playbook_id for c in built] == ["", "pb_tf_q_improve_after_gate"]


def test_widened_slots_accept_the_diagnosed_writers() -> None:
    """The slot must not veto the diagnosis: every repair/fix/improve position
    accepts the general editing set the rules and the model draw from."""
    from orchestra.roles.templates import default_templates

    templates = default_templates()
    for template_id, slot_id in (
        ("review_then_fix", "fixer"),
        ("gate_then_repair", "repairer"),
        ("test_first_improve", "improver"),
        ("chain", "third"),
    ):
        slot = templates[template_id].slot(slot_id)
        for role in ("dependency_resolver", "edge_case_hardener", "gate_repairer", "implementer", "integrator"):
            assert slot.accepts(role), (template_id, slot_id, role)


def test_the_rules_read_the_test_name_not_the_file_it_lives_in() -> None:
    """EXP-20260902-01: a suite file named ..._public_surface.py made two
    config/oauth semantics tests match the surface rule and seated an
    integrator for work an implementer was diagnosed for the run before."""
    names = [
        "harness/m.spec_tests/test_client_behaviour_and_public_surface.py::ConfigHelperTests::test_create_client_from_config_constructs_and_logs_in_client",
        "harness/m.spec_tests/test_client_behaviour_and_public_surface.py::ConfigHelperTests::test_get_oauth2_token_caches_tokens_for_repeated_requests",
    ]
    assert rule_based_roles(names, "spec_tests") is None, "semantic residue belongs to the model"
    surface = ["t.py::SurfaceTests::test_public_surface_reexports_client"]
    assert rule_based_roles(surface, "spec_tests")[0] == "integrator"


def test_apply_patch_replays_the_incumbent_onto_a_fresh_fork(tmp_path) -> None:
    """The continuation base: fork clean from the milestone base, replay the
    incumbent's patch, and the collected changeset then carries incumbent work
    plus whatever the specialist adds -- so the commit path needs no change."""
    import asyncio
    import subprocess

    from orchestra.control.fast_loop.workspace import (
        CandidateWorkspaceError,
        GitCandidateWorkspaceManager,
    )

    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.py").write_text("VALUE = 1\n")
    for cmd in (
        ["git", "init"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base"],
    ):
        subprocess.run(cmd, cwd=src, check=True, capture_output=True)

    mgr = GitCandidateWorkspaceManager()

    async def flow():
        base = await mgr.prepare_base_snapshot(
            source_repo=str(src), run_dir=str(tmp_path), task_id="t", subtask_id="m"
        )
        ws = await mgr.fork_candidate_workspace(
            base=base, run_dir=str(tmp_path), task_id="t", subtask_id="m",
            candidate_id="cand_continue",
        )
        (src / "mod2.py").write_text("ADDED = 2\n")
        patch = subprocess.run(
            ["git", "diff", "--no-index", "--", "/dev/null", "mod2.py"],
            cwd=src, capture_output=True, text=True,
        ).stdout
        await mgr.apply_patch(ws, patch)
        repo = tmp_path / "tasks/t/subtasks/m/candidates/cand_continue/repo"
        assert (repo / "mod2.py").read_text() == "ADDED = 2\n"
        cs = await mgr.collect_changeset(ws)
        assert "mod2.py" in cs.added_untracked_files
        # Garbage must reject, never run on the wrong base.
        try:
            await mgr.apply_patch(ws, "not a patch at all")
        except CandidateWorkspaceError:
            return
        raise AssertionError("garbage patch was accepted")

    asyncio.run(flow())
