"""Stage 4 of the self-evolution spec: the first-pass designer (§4)."""

from __future__ import annotations

import json
from pathlib import Path

from orchestra.control.first_pass.designer import (
    DEFAULT_THRESHOLDS,
    FEntry,
    Trigger,
    add_reviewer,
    default_first_pass_entries,
    design_milestone,
    design_plan,
    load_first_pass_table,
    matched_entries,
    set_template,
    shape_leaves_an_interface,
)
from orchestra.control.first_pass.features import milestone_features
from orchestra.realbench.milestone_planner import parse_plan_payload
from orchestra.roles.pool import load_role_pool
from orchestra.roles.templates import load_templates

DOCS = """
# PRD

The storage module keeps a Registry of formats. Registry.register(name) raises UnsupportedFormat
(a TablibException) when the name is unknown; InvalidDimensions and HeaderMismatchError are raised on bad rows.
The cache is invalidated after every write and the file is flushed on close; reopen restores the table.
The Dataset class, the Databook class, the Row class and the Query class are the public API.

# architecture

storage.py implements Registry and Row. query.py implements Query and QueryBuilder.
"""


def _plan(n=3, template="test_first"):
    ms = []
    for i in range(1, n + 1):
        ms.append({
            "milestone_id": f"m{i}", "title": f"M{i}", "objective": f"part {i}", "split_reason": "feature_module",
            "risk_rationale": "delivers", "gate_level": "implementation", "template_id": template,
            "focus_paths": [f"pkg/storage.py"] if i == 1 else [f"pkg/query.py"],
            "acceptance": {"criteria": ["Registry raises UnsupportedFormat on unknown names" if i == 1 else "Query works"]},
            "agents": [
                {"slot": "test_author", "role": "test_author", "mandate": "suite"},
                {"slot": "builder", "role": "implementer", "mandate": "build"},
                {"slot": "repairer", "role": "gate_repairer", "mandate": "repair"},
            ],
        })
    return parse_plan_payload(json.dumps({"milestones": ms}), max_milestones=5)


def test_features_are_deterministic_and_read_the_relevant_documents() -> None:
    plan = _plan()
    f = milestone_features(plan.milestones[0], index=0, total=3, docs_text=DOCS)
    assert f["kind"] == "foundation" and f["n_focus_files"] == 1
    assert f["n_documented_exceptions"] >= 3 and f["n_state_transitions"] >= 3 and f["n_public_classes"] >= 2
    assert milestone_features(plan.milestones[0], index=0, total=3, docs_text=DOCS) == f
    assert milestone_features(plan.milestones[2], index=2, total=3, docs_text=DOCS)["kind"] == "integration"


def test_entries_trigger_on_their_features() -> None:
    table = default_first_pass_entries()
    thr = dict(DEFAULT_THRESHOLDS)
    ids = lambda fs: [e.entry_id for e in matched_entries(fs, table, thr)]  # noqa: E731
    assert ids({"kind": "foundation"}) == ["F1"]
    assert ids({"kind": "integration"}) == ["F4"]
    assert ids({"kind": "middle", "n_documented_exceptions": 5}) == ["F2"]
    assert ids({"kind": "middle", "n_state_transitions": 3}) == ["F3"]
    assert ids({"kind": "middle", "n_focus_files": 8}) == ["F5"] and ids({"kind": "middle", "n_public_symbols": 20}) == ["F5"]
    assert ids({"kind": "middle", "n_public_classes": 4}) == ["F6"]
    assert ids({"kind": "middle"}) == []
    assert load_first_pass_table() == table


def test_f0_leaves_the_plan_byte_identical() -> None:
    plan = _plan()
    # F1 (trial) would be drawn at random on the foundation milestone; with it
    # back to candidate, nothing but the active F4 can apply.
    table = tuple(e if e.entry_id != "F1" else FEntry(**{**e.__dict__, "state": "candidate"}) for e in default_first_pass_entries())
    designed, decisions = design_plan(plan, docs_text="", table=table)
    # nothing triggers without documents except the integration milestone (F4 is active)
    assert designed.milestones[0] == plan.milestones[0] and designed.milestones[1] == plan.milestones[1]
    assert decisions[0]["applied"] == ["F0"] and decisions[0]["template_after"] == "test_first"
    assert decisions[2]["applied"] == ["F4"] and designed.milestones[2].template_id == "parallel_audit"


def test_applied_shapes_are_valid_templates_with_accepted_roles() -> None:
    pool = load_role_pool("configs/roles")
    templates = load_templates("configs/subgraph_templates", pool=pool)
    plan = _plan()
    m = plan.milestones[0]
    for role in ("contract_critic", "spec_auditor"):
        reviewed, note = add_reviewer(m, role)
        assert reviewed.template_id == "review_then_fix" and role in [a.role for a in reviewed.agents]
        for a in reviewed.agents:
            assert templates[reviewed.template_id].slot(a.slot_id).accepts(a.role), (a.slot_id, a.role)
        assert shape_leaves_an_interface(reviewed)
    audited, _ = set_template(m, "parallel_audit")
    for a in audited.agents:
        assert templates["parallel_audit"].slot(a.slot_id).accepts(a.role)
    chained, _ = set_template(m, "chain")
    assert [a.slot_id for a in chained.agents] == ["test_author", "first", "second", "third"]
    for a in chained.agents:
        assert templates["chain"].slot(a.slot_id).accepts(a.role)
    # a shape that already has the role is left alone
    again, note = add_reviewer(reviewed, role)
    assert again == reviewed and "already" in note


def test_combinations_are_not_applied_until_validated_and_trials_are_randomised() -> None:
    plan = _plan()
    m = plan.milestones[0]
    two_active = (
        FEntry("F0", (), (), (), state="active"),
        FEntry("FA", (Trigger("kind", "==", "foundation"),), ("E2",), ({"kind": "instruction", "file": "F2.md"},), state="active"),
        FEntry("FB", (Trigger("kind", "==", "foundation"),), ("E5",), ({"kind": "add_reviewer", "role": "contract_critic"},), state="active"),
    )
    designed, d = design_milestone(m, index=0, total=3, docs_text=DOCS, table=two_active, thresholds=DEFAULT_THRESHOLDS)
    assert d["matched"] == ["FA", "FB"] and d["applied"] == ["FA"]
    assert any("combination is not validated" in n for n in d["notes"])
    combo = two_active + (FEntry("FA+FB", (Trigger("kind", "==", "foundation"),), ("E2", "E5"),
                                 ({"kind": "instruction", "file": "F2.md"}, {"kind": "add_reviewer", "role": "contract_critic"}),
                                 state="active", combination_of=("FA", "FB")),)
    designed, d = design_milestone(m, index=0, total=3, docs_text=DOCS, table=combo, thresholds=DEFAULT_THRESHOLDS)
    assert d["applied"] == ["FA+FB"] and designed.template_id == "review_then_fix"
    # a trial entry is drawn with probability 0.5, seeded: the same seed repeats
    trial = (FEntry("F0", (), (), (), state="active"),
             FEntry("FT", (Trigger("kind", "==", "foundation"),), ("E2",), ({"kind": "instruction", "file": "F2.md"},), state="trial"))
    outcomes = {design_milestone(m, index=0, total=3, docs_text=DOCS, table=trial, thresholds=DEFAULT_THRESHOLDS, seed=str(s))[1]["applied"][0] for s in range(40)}
    assert outcomes == {"F0", "FT"}
    a = design_milestone(m, index=0, total=3, docs_text=DOCS, table=trial, thresholds=DEFAULT_THRESHOLDS, seed="x")[1]
    b = design_milestone(m, index=0, total=3, docs_text=DOCS, table=trial, thresholds=DEFAULT_THRESHOLDS, seed="x")[1]
    assert a["applied"] == b["applied"] and a["assignment"] == "randomized"


def test_the_shape_must_leave_an_interface_for_the_repairer() -> None:
    plan = _plan()
    m = plan.milestones[0]
    bad = (FEntry("F0", (), (), (), state="active"),
           FEntry("FX", (Trigger("kind", "==", "foundation"),), ("E1",), ({"kind": "template", "template_id": "solo"},), state="active"))
    designed, d = design_milestone(m, index=0, total=3, docs_text=DOCS, table=bad, thresholds=DEFAULT_THRESHOLDS)
    # `solo` is not handled by set_template, so the milestone is unchanged and F0 recorded
    assert designed == m and d["applied"] == ["FX"] or d["applied"] == ["F0"]
    assert not shape_leaves_an_interface(m.__class__(**{**m.__dict__, "template_id": "solo"}))
