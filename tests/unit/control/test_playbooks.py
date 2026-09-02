"""The playbook table is keyed on class and template, not a fixed edit order."""

from __future__ import annotations

from orchestra.control.fast_loop.playbooks import (
    CATALOG,
    QUALITY_CATALOG,
    FailureClass,
    SearchReason,
    infer_failure_class,
    playbooks_for,
)
from orchestra.control.fast_loop.schemas import FailureDiagnosis
from orchestra.control.task_state import SubtaskFailureReason


def _ids(template_id: str, cls: FailureClass) -> list[str]:
    return [p.playbook_id for p in playbooks_for(template_id, cls)]


def test_test_first_functional_opens_with_the_repairer_then_the_critic() -> None:
    assert _ids("test_first", FailureClass.FUNCTIONAL) == [
        "pb_tf_failures_to_repairer",
        "pb_tf_diagnose_before_repair",
        "pb_tf_second_repairer",
    ]


def test_test_first_budget_does_not_offer_the_critic() -> None:
    assert _ids("test_first", FailureClass.BUDGET) == ["pb_tf_builder_budget"]


def test_a_template_with_its_own_rows_hides_the_generic_fallback() -> None:
    ids = _ids("test_first", FailureClass.FUNCTIONAL)
    assert "pb_failures_to_agent" not in ids


def test_an_unknown_template_gets_the_cheap_generic_playbooks() -> None:
    assert _ids("no_such_shape", FailureClass.FUNCTIONAL) == ["pb_failures_to_agent"]
    assert _ids("no_such_shape", FailureClass.BUDGET) == [
        "pb_budget_steps_time_small",
        "pb_budget_steps_time_large",
    ]


def test_solo_functional_starts_with_a_conditional_repair_pass() -> None:
    assert _ids("solo", FailureClass.FUNCTIONAL)[0] == "pb_solo_to_gate_repair"


def test_diagnose_before_repair_is_a_plan_layer_switch() -> None:
    playbook = next(
        p
        for p in playbooks_for("test_first", FailureClass.FUNCTIONAL)
        if p.playbook_id == "pb_tf_diagnose_before_repair"
    )
    assert playbook.layer == "plan"
    assert playbook.switch_template == "test_first_diagnosed"
    assert dict(playbook.switch_slots) == {"critic": "behaviour_critic"}


def test_timeout_and_an_early_stage_infer_as_budget() -> None:
    timeout = FailureDiagnosis(
        reason=SubtaskFailureReason.TIMEOUT,
        retryable=True,
        concise_feedback="timed out",
    )
    early = FailureDiagnosis(
        reason=SubtaskFailureReason.HARNESS,
        retryable=True,
        concise_feedback="imports failed",
        furthest_stage="imports",
    )
    assert infer_failure_class(timeout) is FailureClass.BUDGET
    assert infer_failure_class(early) is FailureClass.BUDGET


def test_quality_search_has_its_own_test_first_table() -> None:
    assert [
        p.playbook_id
        for p in playbooks_for("test_first", search_reason=SearchReason.QUALITY)
    ] == [
        "pb_q_continue_improve",
        "pb_tf_q_improve_after_gate",
        "pb_tf_q_diagnose_then_improve",
    ]


def test_quality_search_on_an_improve_shape_targets_the_improver() -> None:
    assert [
        p.playbook_id
        for p in playbooks_for("test_first_improve", search_reason=SearchReason.QUALITY)
    ] == [
        "pb_q_continue_improve",
        "pb_tf_q_failures_to_improver",
        "pb_tf_q_diagnose_from_improve",
    ]
    assert [
        p.playbook_id
        for p in playbooks_for(
            "test_first_quality_diagnosed", search_reason=SearchReason.QUALITY
        )
    ] == [
        "pb_q_continue_improve",
        "pb_tf_q_failures_to_improver",
    ]


def test_quality_search_does_not_offer_failure_playbooks() -> None:
    quality_ids = {
        p.playbook_id
        for p in playbooks_for("test_first", search_reason=SearchReason.QUALITY)
    }
    failure_ids = set(_ids("test_first", FailureClass.FUNCTIONAL))
    assert quality_ids.isdisjoint(failure_ids)
    assert "pb_tf_failures_to_repairer" not in quality_ids


def test_failure_search_does_not_offer_quality_playbooks() -> None:
    failure_ids = set(_ids("test_first", FailureClass.FUNCTIONAL))
    assert "pb_tf_q_failures_to_builder" not in failure_ids
    assert {p.playbook_id for p in CATALOG}.isdisjoint(
        {p.playbook_id for p in QUALITY_CATALOG}
    )


def test_quality_search_on_an_unknown_template_uses_the_generic_row() -> None:
    assert [
        p.playbook_id
        for p in playbooks_for("no_such_shape", search_reason=SearchReason.QUALITY)
    ] == ["pb_q_failures_to_agent"]


def test_a_named_class_wins_over_inference() -> None:
    diagnosis = FailureDiagnosis(
        reason=SubtaskFailureReason.TIMEOUT,
        retryable=True,
        concise_feedback="timed out",
        failure_class="functional",
    )
    assert infer_failure_class(diagnosis) is FailureClass.FUNCTIONAL


def test_evidence_reorders_the_menu_without_adding_to_it() -> None:
    """Design puts shapes first, a diagnosed shape jumps the queue, and a row
    already spent on this milestone goes to the back."""
    base = [p.playbook_id for p in playbooks_for("test_first", FailureClass.FUNCTIONAL)]
    assert base[0] == "pb_tf_failures_to_repairer"

    design = [p.playbook_id for p in playbooks_for("test_first", FailureClass.DESIGN)]
    assert design[:2] == ["pb_tf_diagnose_before_repair", "pb_tf_second_repairer"]
    assert set(design) == set(base), "reordered, never added or removed"

    shaped = [
        p.playbook_id
        for p in playbooks_for(
            "test_first",
            FailureClass.FUNCTIONAL,
            recommended_shape="test_first_double_repair",
        )
    ]
    assert shaped[0] == "pb_tf_second_repairer"

    spent = [
        p.playbook_id
        for p in playbooks_for(
            "test_first",
            FailureClass.FUNCTIONAL,
            history=["pb_tf_failures_to_repairer"],
        )
    ]
    assert spent[-1] == "pb_tf_failures_to_repairer"


def test_shape_options_is_the_whole_menu_and_nothing_else() -> None:
    from orchestra.control.fast_loop.playbooks import shape_options

    failure = {row.switch_template for row in shape_options("test_first")}
    assert failure == {"test_first_diagnosed", "test_first_double_repair"}
    quality = {
        row.switch_template
        for row in shape_options("test_first", search_reason=SearchReason.QUALITY)
    }
    assert quality == {"continuation", "test_first_improve", "test_first_quality_diagnosed"}


def test_rows_declare_the_metric_they_exist_to_move() -> None:
    """Intent is the contract the ledger checks a row against; a shape row
    without one cannot be audited into or out of the table."""
    for row in (*CATALOG, *QUALITY_CATALOG):
        if row.switch_template or row.include_failure_list:
            assert row.intent, row.playbook_id
