"""Stage 1 of the self-evolution spec: probes, R0, the repair trigger, prior suites, config."""

from __future__ import annotations

import json

import pytest

import tempfile
from pathlib import Path

import yaml

from orchestra.control.fast_loop.budget import FastLoopBudgetTracker, remaining_budget
from orchestra.control.fast_loop.committed_cases import read_committed_cases, write_committed_cases
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.evolution_config import (
    REPAIR_TRIGGER_ENV,
    EvolutionConfig,
    repair_trigger_from_env,
)
from orchestra.control.fast_loop.prior_suites import PriorSuite, rewrite_command
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
    FailureDiagnosis,
    FastLoopBudget,
    FastLoopState,
)
from orchestra.control.task_state import SubtaskFailureReason
from orchestra.ir.edges import EdgeCondition
from orchestra.ir.compiler import GraphCompiler
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.graph_invariants import _is_failure_condition, check_graph_invariants
from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.realbench.subgraph_builder import (
    materialize_milestone_subgraph,
    prepare_generated_root,
    repair_edge_condition,
)

P = "../../ws/harness/m.spec_tests/test_a.py::"
A, B, C = P + "test_a", P + "test_b", P + "test_c"


def _rec(cid, failed, *, status=CandidateStatus.VALID, calls=1, **meta):
    return CandidateRecord(
        candidate_id=cid, attempt_id=1, graph_hash="g", parent_graph_hash="g", edits=[],
        status=status, behaviour_score=0.5, behaviour_failures=list(failed),
        cost=CostRecord(backend_calls=calls), metadata=dict(meta),
    )


# --- config --------------------------------------------------------------------


def test_evolution_config_is_off_unless_enabled() -> None:
    assert EvolutionConfig.from_config({}) == EvolutionConfig()
    assert EvolutionConfig.from_config({"probes": {"default": 5}}).enabled is False
    cfg = EvolutionConfig.from_config({
        "evolution": {"enabled": True, "repair_trigger": "failures"},
        "probes": {"default": 2, "adaptive": True, "flaky_extra_threshold": 3},
        "budget": {"mode": "B", "probes_separate": True},
        "trial": {"prob": 0.3},
    })
    assert cfg.enabled and cfg.row_slots == 1 and cfg.max_probes == 3 and cfg.probes_separate
    assert EvolutionConfig.from_config({"evolution": {"enabled": True}, "budget": {"mode": "A"}}).row_slots == 2
    with pytest.raises(ValueError):
        EvolutionConfig.from_config({"evolution": {"enabled": True}, "budget": {"mode": "C"}})


# --- P1 probes -------------------------------------------------------------------


def test_probe_count_adapts_to_what_the_probes_show() -> None:
    evo = EvolutionConfig(enabled=True, probes_default=2, probes_adaptive=True, flaky_extra_threshold=3)
    inc = _rec("incumbent_first_pass", [A, B], incumbent=True)
    more, _ = FastLoopController._more_probes_wanted(evo, inc, [])
    assert more
    # the first probe reproduced the incumbent exactly -> one probe is enough
    same = _rec("cand_feedback", [B, A], persistence_phase=1, probe=True)
    more, why = FastLoopController._more_probes_wanted(evo, inc, [same])
    assert not more and "1 probe" in why
    # it differed -> the default second probe
    diff = _rec("cand_feedback", [A], persistence_phase=1, probe=True)
    assert FastLoopController._more_probes_wanted(evo, inc, [diff])[0]
    # two probes, fewer than three flaky cases -> stop at the default
    p2 = _rec("cand_feedback_r2", [A, B], persistence_phase=1, probe=True)
    assert not FastLoopController._more_probes_wanted(evo, inc, [diff, p2])[0]
    # three flaky cases -> a third probe, and never a fourth
    flaky_inc = _rec("incumbent_first_pass", [A, B, C, P + "test_d"], incumbent=True)
    p1 = _rec("cand_feedback", [A], persistence_phase=1, probe=True)
    p2 = _rec("cand_feedback_r2", [A, B, C, P + "test_d"], persistence_phase=1, probe=True)
    more, why = FastLoopController._more_probes_wanted(evo, flaky_inc, [p1, p2])
    assert more and "third" in why
    p3 = _rec("cand_feedback_r3", [A], persistence_phase=1, probe=True)
    assert not FastLoopController._more_probes_wanted(evo, flaky_inc, [p1, p2, p3])[0]
    # a fixed count ignores what the probes show: exactly probes_default of them
    fixed = EvolutionConfig(enabled=True, probes_default=2, probes_adaptive=False)
    assert FastLoopController._more_probes_wanted(fixed, inc, [same])[0]
    assert not FastLoopController._more_probes_wanted(fixed, inc, [same, p2])[0]


def test_probes_do_not_occupy_candidate_slots_when_budgeted_apart() -> None:
    budget = FastLoopBudget(max_candidates=2, max_total_backend_calls=20, max_attempts_per_subtask=3, probes_separate=True)
    diagnosis = FailureDiagnosis(reason=SubtaskFailureReason.HARNESS, retryable=True, concise_feedback="x")
    state = FastLoopState(subtask_id="m", base_attempt_id=1, base_graph_hash="g", diagnosis=diagnosis)
    state.candidates = [
        _rec("incumbent_first_pass", [A], incumbent=True, calls=0),
        _rec("cand_feedback", [A], persistence_phase=1, probe=True),
        _rec("cand_feedback_r2", [A], persistence_phase=1, probe=True),
        _rec("cand_feedback_r3", [A], persistence_phase=1, probe=True),
    ]
    tracker = FastLoopBudgetTracker(budget)
    ok, reason, _ = tracker.can_generate_candidate(state)
    assert ok, reason
    # the same three probes inside the slots would consume three of them
    inside = budget.model_copy(update={"probes_separate": False})
    assert remaining_budget(budget, state)["candidates"] == remaining_budget(inside, state)["candidates"] + 3
    assert not FastLoopBudgetTracker(inside).can_generate_candidate(state)[0]


# --- the repair slot's trigger ----------------------------------------------------


def test_repair_slot_runs_on_failures_under_the_new_trigger(monkeypatch) -> None:
    monkeypatch.delenv(REPAIR_TRIGGER_ENV, raising=False)
    assert repair_trigger_from_env() == "gate"
    gate_only = EdgeCondition.model_validate(repair_edge_condition("gate"))
    on_failures = EdgeCondition.model_validate(repair_edge_condition("failures"))
    passed_with_failures = {"passed": True, "score": 0.93, "behaviour_failed_count": 3}
    failed = {"passed": False, "score": 0.4, "behaviour_failed_count": 9}
    clean = {"passed": True, "score": 1.0, "behaviour_failed_count": 0}
    assert not gate_only.evaluate(passed_with_failures) and gate_only.evaluate(failed)
    assert on_failures.evaluate(passed_with_failures) and on_failures.evaluate(failed)
    assert not on_failures.evaluate(clean)
    # a harness that predates the field never triggers the repair slot by accident
    assert not on_failures.evaluate({"passed": True})
    monkeypatch.setenv(REPAIR_TRIGGER_ENV, "failures")
    assert repair_edge_condition()["source_field"] == "behaviour_failed_count"
    assert _is_failure_condition(on_failures) and _is_failure_condition(gate_only)


# --- prior suites and the committed-cases sidecar --------------------------------


def test_prior_suite_command_points_at_the_predecessor_without_custody() -> None:
    prior = PriorSuite(
        milestone_id="m1", spec_dir="/h/m1.spec_tests", contracts="/h/m1.contracts.json",
        level="implementation", committed_passed=frozenset({"t.py::a"}), committed_failed=frozenset(),
    )
    current = ["python", "check.py", "--manifest", "/h/manifest.json", "--level", "integration",
               "--contracts", "/h/m2.contracts.json", "--spec-tests", "/h/m2.spec_tests", "--take-custody"]
    out = rewrite_command(current, prior)
    assert "--take-custody" not in out
    assert out[out.index("--spec-tests") + 1] == "/h/m1.spec_tests"
    assert out[out.index("--contracts") + 1] == "/h/m1.contracts.json"
    assert out[out.index("--level") + 1] == "implementation"
    assert out[:4] == current[:4]


def test_committed_cases_round_trip_with_workspace_prefixes_stripped(tmp_path) -> None:
    spec = tmp_path / "m1.spec_tests"
    spec.mkdir()
    path = write_committed_cases(spec, passed=[A, B], failed=[C], source="first_pass", milestone_id="m1")
    assert path is not None and path.name == "m1.spec_tests.committed_cases.json"
    data = json.loads(path.read_text())
    assert data["passed"] == ["test_a.py::test_a", "test_a.py::test_b"] and data["failed"] == ["test_a.py::test_c"]
    assert read_committed_cases(spec)["milestone_id"] == "m1"
    assert read_committed_cases(tmp_path / "missing.spec_tests") is None
    assert write_committed_cases(None, passed=[], failed=[], source="x") is None


def _compiled_test_first(monkeypatch, trigger: str) -> dict:
    monkeypatch.setenv(REPAIR_TRIGGER_ENV, trigger)
    plan = parse_plan_payload(
        {"milestones": [{
            "milestone_id": "m", "objective": "build it", "risk_rationale": "r", "template_id": "test_first",
            "agents": [
                {"slot": "test_author", "role": "test_author", "mandate": "suite"},
                {"slot": "builder", "role": "implementer", "mandate": "build"},
                {"slot": "repairer", "role": "gate_repairer", "mandate": "repair"},
            ],
        }]},
        max_agents=4,
    )
    root = prepare_generated_root(Path(tempfile.mkdtemp()), base_contracts_dir="configs/contracts")
    path, _ = materialize_milestone_subgraph(
        generated_root=root, milestone=plan.milestones[0], agent_backend="codex_sdk",
        harness_command=["python", "check.py", "--spec-tests", "/tmp/frozen"],
    )
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    generated = Path(payload["metadata"]["agent_roster"][0]["contract_path"]).parent
    compiler = GraphCompiler(
        contracts=load_contracts(str(generated)), harness_ids={"repository_test_harness"},
        transform_ids={"freeze_repository_change"}, selector_ids=set(),
        backend_ids={"codex_sdk", "smolagents_code", "structured_llm"},
    )
    compiler.compile(OrchestraGraph(**payload))
    return payload


def test_test_first_compiles_with_the_failures_trigger_and_keeps_the_invariants(monkeypatch) -> None:
    payload = _compiled_test_first(monkeypatch, "failures")
    gate_edges = [e for e in payload["edges"] if e["destination_input"] == "gate_report"]
    assert gate_edges and all(e["condition"] == repair_edge_condition("failures") for e in gate_edges)
    graph = OrchestraGraph(**payload)
    assert not [v for v in check_graph_invariants(graph) if v.invariant == "early_gate_has_consumer"]
    legacy = _compiled_test_first(monkeypatch, "gate")
    legacy_edges = [e for e in legacy["edges"] if e["destination_input"] == "gate_report"]
    assert all(e["condition"] == {"source_field": "passed", "operator": "is_false"} for e in legacy_edges)
