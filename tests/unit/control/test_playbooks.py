"""The playbook table is keyed on class and template, not a fixed edit order."""

from __future__ import annotations

from orchestra.control.fast_loop.playbooks import (
    FailureClass,
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


def test_a_named_class_wins_over_inference() -> None:
    diagnosis = FailureDiagnosis(
        reason=SubtaskFailureReason.TIMEOUT,
        retryable=True,
        concise_feedback="timed out",
        failure_class="functional",
    )
    assert infer_failure_class(diagnosis) is FailureClass.FUNCTIONAL
