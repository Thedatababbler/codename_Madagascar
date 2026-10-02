"""Stage 3 of the self-evolution spec: playbook table v2 and the ledger."""

from __future__ import annotations

import json
from pathlib import Path

from orchestra.control.evolution.ledger import candidate_records, milestone_record, split_of, write_search_ledger
from orchestra.control.fast_loop.acceptance import ACCEPTANCE_KEY
from orchestra.control.fast_loop.playbook_v2 import (
    PlaybookRow,
    Precondition,
    RowFacts,
    SlotEdit,
    compose,
    default_legacy_rows,
    default_repair_rows,
    load_repair_table,
    precondition_holds,
    save_repair_table,
    select_rows,
    to_playbook,
    transition,
)
from orchestra.control.fast_loop.schemas import (
    CandidateRecord,
    CandidateStatus,
    CostRecord,
    FailureDiagnosis,
    FastLoopState,
)
from orchestra.control.task_state import SubtaskFailureReason
from orchestra.roles.pool import load_role_pool
from orchestra.roles.templates import load_templates

ROWS = default_repair_rows()
BY_ID = {r.row_id: r for r in ROWS}


def _facts(**kw) -> RowFacts:
    base = dict(template_id="test_first", roles_by_slot={"test_author": "test_author", "builder": "implementer", "repairer": "gate_repairer"})
    base.update(kw)
    return RowFacts(**base)


def test_the_table_matches_the_spec_and_round_trips(tmp_path) -> None:
    active = sorted(r.row_id for r in ROWS if r.state == "active")
    assert active == ["E1-B2", "E2-R1", "E3-T1", "E7-T1", "E9-T1", "U-B1", "U-N1"]
    assert len(ROWS) == 25  # 3 universal + 22 class rows (§3.2-3.3)
    assert all(r.from_incumbent for r in ROWS if r.row_id != "E9-T1") and not BY_ID["E9-T1"].from_incumbent
    path = save_repair_table(ROWS + default_legacy_rows(), tmp_path / "repair.yaml", version="v1")
    back = load_repair_table(path)
    assert back == ROWS + default_legacy_rows()
    assert all(r.table == "legacy" for r in back if r.row_id.startswith("pb_solo"))


def test_every_row_maps_onto_an_existing_template_and_pool_role() -> None:
    pool = load_role_pool("configs/roles")
    templates = load_templates("configs/subgraph_templates", pool=pool)
    facts = _facts()
    for row in ROWS:
        pb = to_playbook(row, facts=facts)
        if row.action == "N":
            assert pb is None
            continue
        assert pb is not None, row.row_id
        if pb.switch_template:
            assert pb.switch_template in templates, (row.row_id, pb.switch_template)
            for slot, role in pb.switch_slots:
                assert templates[pb.switch_template].slot(slot).accepts(role), (row.row_id, slot, role)
        assert pb.search_reasons and pb.playbook_id == f"v2:{row.row_id}"


def test_composition_rule_swaps_a_present_reviewer_or_skips() -> None:
    facts = _facts(template_id="review_then_fix", roles_by_slot={"reviewer": "contract_critic", "author": "implementer", "fixer": "gate_repairer"})
    swapped, note = compose(BY_ID["E2-R1"], facts)
    assert swapped is not None and swapped.row_id == "E2-R1~swap"
    assert swapped.slot_edits[0].role == "spec_auditor" and "swapped" in note
    every = _facts(roles_by_slot={"a": "contract_critic", "b": "spec_auditor", "c": "behaviour_critic", "d": "gate_repairer"})
    skipped, note = compose(BY_ID["E2-R1"], every)
    assert skipped is None and "already" in note


def test_e9_t1_leaves_the_incumbent_and_runs_out_of_alternatives() -> None:
    row = BY_ID["E9-T1"]
    pb = to_playbook(row, facts=_facts(template_id="test_first"))
    assert pb is not None and pb.switch_template == "gate_then_repair" and not pb.continue_from_incumbent
    pb2 = to_playbook(row, facts=_facts(template_id="gate_then_repair", templates_tried=("review_then_fix",)))
    assert pb2 is None
    # its preconditions: a failed gate and a stuck class
    assert not precondition_holds(Precondition("gate_failed"), _facts())
    assert precondition_holds(Precondition("gate_failed"), _facts(gate_failed=True))


def test_selection_filters_preconditions_history_and_records_why() -> None:
    sel = select_rows(ROWS, classes=["E2"], facts=_facts(), row_slots=1, trial_prob=0.0, max_trial_concurrent=2, trials_running=0)
    assert [r.row_id for r in sel.rows] == ["E2-R1"]
    assert sel.filtered["U-N1"].startswith("precondition") and sel.filtered["U-B1"].startswith("precondition")
    again = select_rows(ROWS, classes=["E2"], facts=_facts(tried_rows=("E2-R1",)), row_slots=1, trial_prob=0.0,
                        max_trial_concurrent=2, trials_running=0)
    assert sel.rows and "E2-R1" not in [r.row_id for r in again.rows]
    assert again.filtered["E2-R1"] == "already tried on this milestone"
    # high variance: U-N1 takes the slot first
    hv = select_rows(ROWS, classes=["E3"], facts=_facts(high_variance=True), row_slots=1, trial_prob=0.0,
                     max_trial_concurrent=2, trials_running=0)
    assert [r.row_id for r in hv.rows] == ["U-N1"]


def test_trial_slot_probability_and_global_cap() -> None:
    rows = list(ROWS) + [
        PlaybookRow(row_id="E2-X1", table="repair", error_classes=("E2",), action="S", state="trial", intent="t"),
    ]
    always = select_rows(rows, classes=["E2"], facts=_facts(), row_slots=1, trial_prob=1.0, max_trial_concurrent=2, trials_running=0)
    assert [r.row_id for r in always.rows] == ["E2-X1"] and always.trial_slot_used
    never = select_rows(rows, classes=["E2"], facts=_facts(), row_slots=1, trial_prob=0.0, max_trial_concurrent=2, trials_running=0)
    assert [r.row_id for r in never.rows] == ["E2-R1"]
    capped = select_rows(rows, classes=["E2"], facts=_facts(), row_slots=1, trial_prob=1.0, max_trial_concurrent=2, trials_running=2)
    assert [r.row_id for r in capped.rows] == ["E2-R1"] and not capped.trial_slot_used
    # mode A: two slots -> the active row and the trial row
    both = select_rows(rows, classes=["E2"], facts=_facts(), row_slots=2, trial_prob=0.0, max_trial_concurrent=2, trials_running=0)
    assert [r.row_id for r in both.rows] == ["E2-R1", "E2-X1"]
    # the draw is seeded: the same inputs give the same choice
    a = select_rows(rows, classes=["E2"], facts=_facts(), row_slots=1, trial_prob=0.5, max_trial_concurrent=2, trials_running=0, seed=("t", "m", "1"))
    b = select_rows(rows, classes=["E2"], facts=_facts(), row_slots=1, trial_prob=0.5, max_trial_concurrent=2, trials_running=0, seed=("t", "m", "1"))
    assert [r.row_id for r in a.rows] == [r.row_id for r in b.rows]


def test_state_machine() -> None:
    row = PlaybookRow(row_id="X", table="repair", error_classes=("E3",), action="S")
    t = transition(row, "select")
    assert t.state == "trial"
    assert transition(t, "pass").state == "active"
    back = transition(t, "fail")
    assert back.state == "candidate"
    assert transition(transition(back, "select"), "fail").state == "retired"
    assert transition(transition(t, "pass"), "fail").state == "candidate"


def _rec(cid, failed, passed, **meta):
    return CandidateRecord(candidate_id=cid, attempt_id=1, graph_hash="g", parent_graph_hash="g", edits=[],
                           status=CandidateStatus.VALID, behaviour_score=0.5, behaviour_failures=failed,
                           behaviour_passed=passed, cost=CostRecord(backend_calls=1, estimated_cost_usd=1.0), metadata=dict(meta))


def test_ledger_records_pair_rows_with_r0_and_require_a_workspace(tmp_path) -> None:
    diagnosis = FailureDiagnosis(reason=SubtaskFailureReason.HARNESS, retryable=True, concise_feedback="x")
    fl = FastLoopState(subtask_id="m2", base_attempt_id=1, base_graph_hash="g", diagnosis=diagnosis)
    fl.persistence = {"persistent": ["t.py::a", "t.py::b"], "flaky": []}
    fl.error_classes = {"classes": ["E3"], "counts": {"E3": 2}}
    inc = _rec("incumbent_first_pass", ["t.py::a", "t.py::b"], ["t.py::c"], incumbent=True)
    r0 = _rec("cand_R0", ["t.py::b"], ["t.py::a", "t.py::c"], candidate_kind="R0", persistence_phase=2)
    r0.metadata[ACCEPTANCE_KEY] = {"accepted": True, "fixed": ["t.py::a"], "regressed": [], "net_fix": 1}
    row = _rec("cand_v2:E3-S1", [], ["t.py::a", "t.py::b", "t.py::c"], candidate_kind="row", v2_row="E3-S1", v2_row_state="candidate", persistence_phase=2)
    row.playbook_id = "v2:E3-S1"
    row.metadata[ACCEPTANCE_KEY] = {"accepted": True, "fixed": ["t.py::a", "t.py::b"], "regressed": [], "net_fix": 2}
    fl.candidates = [inc, r0, row]
    fl.selected_candidate_id = "cand_v2:E3-S1"
    recs = candidate_records(fl, task_id="tinydb", milestone_id="m2", split="train", playbook_version="v1")
    by = {r["candidate_kind"]: r for r in recs}
    assert by["first_run"]["per_case_results"] == {"t.py::a": "fail", "t.py::b": "fail", "t.py::c": "pass"}
    assert by["row"]["row_id"] == "E3-S1" and by["row"]["paired_R0_record_id"] == "tinydb:m2:cand_R0"
    assert by["row"]["delta_vs_R0"] == 1 and by["R0"]["paired_R0_record_id"] == ""
    assert by["row"]["stable_pass_before"] == ["t.py::c"]
    ms = milestone_record(fl, task_id="tinydb", milestone_id="m2", split="train")
    assert ms["rows_tried"] == ["E3-S1"] and ms["committed_record_id"] == "tinydb:m2:cand_v2:E3-S1"
    cand_path, ms_path = write_search_ledger(fl, task_id="tinydb", milestone_id="m2", playbook_version="v1",
                                             ledger_root=tmp_path, split_file=Path("configs/datasets/evolution_split.yaml"))
    assert cand_path.is_file() and ms_path.is_file()
    first = json.loads(cand_path.read_text().splitlines()[0])
    assert first["split"] == "train" and "workspace_ref" in first
    assert split_of("pyjwt") == "test" and split_of("nl2_tablib") == "train" and split_of("nope") == "unknown"


def test_upstream_row_is_a_continuation_whose_improver_takes_the_blamed_role() -> None:
    """U-S1 hands the failures back to the kind of writer the ownership blame named (EXP-20261002-01)."""
    row = next(r for r in ROWS if r.row_id == "U-S1")
    pb = to_playbook(row, facts=_facts(blamed_before_repairer=True))
    assert pb is not None and pb.switch_template == "continuation" and pb.role_from_diagnosis == "improver"
    assert pb.continue_from_incumbent and pb.include_failure_list
    sel = select_rows(ROWS, classes=["E3"], facts=_facts(blamed_before_repairer=False), row_slots=1, trial_prob=0.0,
                      max_trial_concurrent=2, trials_running=0)
    assert sel.filtered.get("U-S1", "").startswith("precondition")
    from orchestra.roles.templates import load_templates
    improver = load_templates()["continuation"].slot("improver")
    assert improver.accepts("contract_author") and improver.accepts("implementer")
