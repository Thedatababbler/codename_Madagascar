"""Edit engine must not mutate the base graph."""

from __future__ import annotations

import pytest

from orchestra.control.fast_loop.edit_engine import LocalEditError, apply_local_edits
from orchestra.control.fast_loop.schemas import (
    BudgetAdjustmentEdit,
    PromptFeedbackEdit,
    ToolPolicyEdit,
)
from orchestra.ir.graph import load_graph

GRAPH = "configs/graphs/codex_single_implementer.yaml"


def test_apply_prompt_feedback_is_immutable():
    base = load_graph(GRAPH)
    base_hash = base.content_hash
    edited = apply_local_edits(
        base,
        [PromptFeedbackEdit(node_id="codex_implementer", feedback="return a+b")],
    )
    assert base.content_hash == base_hash
    assert edited.content_hash != base_hash
    assert edited.metadata["parent_graph_hash"] == base_hash
    node = next(n for n in edited.nodes if n.node_id == "codex_implementer")
    assert node.prompt_feedback == "return a+b"
    base_node = next(n for n in base.nodes if n.node_id == "codex_implementer")
    assert base_node.prompt_feedback is None


def test_reject_unknown_node():
    base = load_graph(GRAPH)
    with pytest.raises(LocalEditError, match="unknown node_id"):
        apply_local_edits(
            base,
            [PromptFeedbackEdit(node_id="missing", feedback="x")],
        )


def test_reject_dangerous_tools():
    base = load_graph(GRAPH)
    with pytest.raises(LocalEditError, match="dangerous"):
        apply_local_edits(
            base,
            [
                ToolPolicyEdit(
                    node_id="codex_implementer",
                    add_tools=["private_evaluator"],
                )
            ],
        )


def test_budget_adjustment_updates_max_steps():
    base = load_graph(GRAPH)
    edited = apply_local_edits(
        base,
        [
            BudgetAdjustmentEdit(
                node_id="codex_implementer",
                max_steps_delta=2,
                timeout_seconds_delta=10,
            )
        ],
        max_timeout_seconds=400,
        max_steps_cap=64,
    )
    node = next(n for n in edited.nodes if n.node_id == "codex_implementer")
    assert node.resolved_backend().max_steps == 3
    assert node.timeout_seconds == 310
