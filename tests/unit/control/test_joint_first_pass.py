"""Joint experiment: F7 hands the implementer the inventory's main-path / boundary / error-path items,
from the experiment namespace only; F0 is the planner's choice unchanged."""

from __future__ import annotations

import json

from orchestra.control.first_pass import designer as D
from orchestra.realbench.milestone_planner import parse_plan_payload

NS = "configs/playbook_v2/experiments/joint_20261007/first_pass.yaml"


def _m():
    plan = parse_plan_payload({"milestones": [{"milestone_id": "m", "objective": "o", "risk_rationale": "r",
                                               "template_id": "test_first", "agents": [
                                                   {"slot": "test_author", "role": "test_author", "mandate": "suite"},
                                                   {"slot": "builder", "role": "implementer", "mandate": "build it"}]}]},
                              max_agents=4)
    return plan.milestones[0]


def _builder(m):
    return next(a for a in m.agents if a.role == "implementer")


def test_forced_f7_from_the_namespace_appends_the_inventory_items(monkeypatch, tmp_path) -> None:
    (tmp_path / "m.inventory.json").write_text(json.dumps({"items": [
        {"kind": "main_path", "symbol": "Store.get", "quote": "Returns the stored value."},
        {"kind": "boundary", "symbol": "Store.get", "quote": "Returns None when the key is absent."},
        {"kind": "error_path", "symbol": "Store.put", "quote": "Raises KeyError on an empty key."},
        {"kind": "protocol", "symbol": "Store", "quote": "Store supports iteration."},
    ]}))
    monkeypatch.setenv(D.FIRST_PASS_TABLE_ENV, NS)
    monkeypatch.setenv(D.FP_INVENTORY_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(D.FORCE_F_ENV, "F7")
    m = _m()
    designed, decision = D.design_milestone(m, index=0, total=2, docs_text="", table=D.load_first_pass_table(),
                                            thresholds=D.DEFAULT_THRESHOLDS)
    mandate = _builder(designed).mandate
    assert decision["applied"] == ["F7"]
    assert "Returns the stored value." in mandate and "Raises KeyError on an empty key." in mandate
    assert "Store supports iteration." not in mandate          # protocol items are not F7's
    assert "Before writing any code" in mandate                 # the F7 instruction itself


def test_forced_f0_leaves_the_milestone_unchanged(monkeypatch) -> None:
    monkeypatch.setenv(D.FIRST_PASS_TABLE_ENV, NS)
    monkeypatch.setenv(D.FORCE_F_ENV, "F0")
    m = _m()
    designed, decision = D.design_milestone(m, index=0, total=2, docs_text="", table=D.load_first_pass_table(),
                                            thresholds=D.DEFAULT_THRESHOLDS)
    assert designed == m and decision["applied"] == ["F0"]


def test_official_table_is_untouched_without_the_namespace(monkeypatch) -> None:
    monkeypatch.delenv(D.FIRST_PASS_TABLE_ENV, raising=False)
    f7 = next(e for e in D.load_first_pass_table() if e.entry_id == "F7")
    assert all(a.get("kind") != "inventory" for a in f7.actions)
    assert all(e.entry_id != "F8" for e in D.load_first_pass_table())
