"""LocalEdit discriminated union schema tests."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from orchestra.backends.capabilities import SessionPolicy
from orchestra.control.fast_loop.schemas import (
    BudgetAdjustmentEdit,
    LocalEdit,
    ModelOverrideEdit,
    PromptFeedbackEdit,
    SessionPolicyEdit,
    ToolPolicyEdit,
)

adapter = TypeAdapter(LocalEdit)


def test_prompt_feedback_edit_roundtrip():
    edit = PromptFeedbackEdit(node_id="n1", feedback="fix the bug")
    restored = adapter.validate_python(edit.model_dump(mode="json"))
    assert isinstance(restored, PromptFeedbackEdit)
    assert restored.feedback == "fix the bug"


def test_session_policy_edit_accepts_enum():
    edit = SessionPolicyEdit(node_id="n1", policy=SessionPolicy.FORK)
    assert edit.policy is SessionPolicy.FORK


def test_tool_policy_defaults():
    edit = ToolPolicyEdit(node_id="n1", add_tools=["python_interpreter"])
    assert edit.remove_tools == []


def test_budget_and_model_edits():
    assert BudgetAdjustmentEdit(node_id="n1", max_steps_delta=2).max_steps_delta == 2
    assert ModelOverrideEdit(node_id="n1", model_name="gpt-4o-mini").model_name


def test_unknown_edit_type_rejected():
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "genetic_mutation", "node_id": "n1"})
