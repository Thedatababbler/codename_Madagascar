"""Stage 2 of the self-evolution spec: routing (§2.2.2) and error classes (§2.2.3)."""

from __future__ import annotations

from orchestra.control.fast_loop.error_classes import (
    CaseFacts,
    classify_case,
    classify_persistent,
    marker_in,
)
from orchestra.control.fast_loop.routing import (
    Predecessor,
    citation_ok,
    citations_in,
    route_case,
    route_persistent,
)


def _f(name, **kw):
    kw.setdefault("case_id", f"../h/m.spec_tests/test_x.py::{name}")
    kw.setdefault("key", f"test_x.py::{name}")
    return CaseFacts(name=name, **kw)


# --- one class per constructed sample ---------------------------------------------


def test_each_error_class_has_a_rule_that_reaches_it() -> None:
    assert classify_case(_f("test_build", output="NotImplementedError: TODO")) == "E1"
    assert classify_case(_f("test_build"), agent_truncated=True) == "E1"
    assert classify_case(_f("test_root_reexports_client", output="ImportError: cannot import name 'Client'")) == "E2"
    assert classify_case(_f("test_anything"), structural_stage_failed=True) == "E2"
    assert classify_case(_f("test_fetch_returns_rows", source="def test_fetch_returns_rows():\n    assert x == 1")) == "E3"
    assert classify_case(_f("test_tree_grows", source="def t():\n    tree.insert(k)\n    # the node splits when full\n    assert tree.split")) == "E4"
    assert classify_case(_f("test_rejects_bad_key", source="with pytest.raises(KeyError):\n    ...", output="Failed: DID NOT RAISE")) == "E5"
    assert classify_case(_f("test_empty_input_repr", source="assert repr(x) == ''")) == "E6"
    assert classify_case(_f("test_pipeline", symbol_files={"pkg/a.py", "pkg/b.py"})) == "E7"
    assert classify_case(_f("test_bulk_insert", output="E   Failed: Timeout >20.0s")) == "E8"
    assert classify_case(_f("test_bulk_insert", timed_out=True)) == "E8"


def test_author_marker_wins() -> None:
    src = '@pytest.mark.error_class("E5")\ndef test_x():\n    pass'
    assert marker_in(src) == "E5"
    assert classify_case(_f("test_pipeline", source=src, symbol_files={"a.py", "b.py"})) == "E5"


def test_mixing_rule() -> None:
    e3 = [_f(f"test_main_{i}", key=f"t.py::m{i}") for i in range(4)]
    e5 = [_f("test_rejects", key="t.py::r", source="pytest.raises(", output="DID NOT RAISE")]
    e2 = [_f("test_root_import", key="t.py::i", output="ImportError: x")]
    # 4/5 in one class -> that class
    one = classify_persistent(e3 + e5)
    assert one.classes == ("E3",) and one.counts == {"E3": 4, "E5": 1}
    # two classes, neither at 2/3 -> both, larger first
    two = classify_persistent(e3[:2] + e5 + [_f("test_bad_key", key="t.py::k", source="pytest.raises(", output="DID NOT RAISE")])
    assert two.classes == ("E3", "E5")
    # three classes scattered -> E9
    three = classify_persistent(e3[:1] + e5 + e2)
    assert three.classes == ("E9",)
    # stuck: the same class twice without progress -> E9 whatever the mix
    assert classify_persistent(e3, stuck=True).classes == ("E9",)
    assert classify_persistent([]).classes == ()


# --- routing ------------------------------------------------------------------------


def _pred(mid, focus, failed=(), passed=(), files=None):
    files = files or {}
    return Predecessor(
        milestone_id=mid, focus_paths=tuple(focus), committed_failed=frozenset(failed),
        committed_passed=frozenset(passed), case_files=lambda k: set(files.get(k, set())),
    )


def test_inherited_defects_are_routed_to_the_owner_in_three_kinds() -> None:
    cur = ["pkg/query.py"]
    facts = _f("test_storage_reads", symbol_files={"pkg/storage.py"})
    # 1. the predecessor committed with a failing case on the same file
    p = _pred("m1", ["pkg/storage.py"], failed={"t1.py::a"}, passed={"t1.py::b"},
              files={"t1.py::a": {"pkg/storage.py"}, "t1.py::b": {"pkg/other.py"}})
    r = route_case(facts, current_focus=cur, predecessors=[p])
    assert r is not None and r.route == "inherited.committed_with_failure" and r.owner_milestone_id == "m1"
    # 2. no predecessor case touches the file
    p = _pred("m1", ["pkg/storage.py"], passed={"t1.py::b"}, files={"t1.py::b": {"pkg/other.py"}})
    assert route_case(facts, current_focus=cur, predecessors=[p]).route == "inherited.uncovered"
    # 3. the predecessor covers the file and passed -> boundary
    p = _pred("m1", ["pkg/storage.py"], passed={"t1.py::b"}, files={"t1.py::b": {"pkg/storage.py"}})
    assert route_case(facts, current_focus=cur, predecessors=[p]).route == "inherited.boundary"
    # a case that touches our own focus path stays with us
    ours = _f("test_query", symbol_files={"pkg/storage.py", "pkg/query.py"})
    assert route_case(ours, current_focus=cur, predecessors=[p]) is None


def test_suite_suspect_and_environment_routes() -> None:
    src = 'def test_x():\n    # PRD: "the cache is invalidated after every write"\n    assert 1'
    docs = "Section 3. The cache is invalidated after every write. Section 4."
    assert citations_in(src) == ["the cache is invalidated after every write"]
    assert citation_ok(src, docs) is True
    assert citation_ok(src, "nothing about caches here") is False
    assert citation_ok("def test_y():\n    assert 1", docs) is None
    suspect = _f("test_x", citation_ok=False)
    assert route_case(suspect, current_focus=[], predecessors=[]).route == "suite_suspect"
    flaky_timeout = _f("test_slow", flaky=True, timed_out=True)
    assert route_case(flaky_timeout, current_focus=[], predecessors=[]).route == "suite_suspect"
    env = _f("test_import", output="ModuleNotFoundError: No module named 'pkg'  # adamas shadow: not built in this workspace")
    assert route_case(env, current_focus=[], predecessors=[]).route == "environment"


def test_routed_cases_leave_the_repair_list_and_regressions_join_it() -> None:
    kept = _f("test_ok", key="t.py::ok", symbol_files={"pkg/query.py"})
    away = _f("test_inherited", key="t.py::inh", symbol_files={"pkg/storage.py"})
    sus = _f("test_sus", key="t.py::sus", citation_ok=False)
    p = _pred("m1", ["pkg/storage.py"])
    result = route_persistent(
        [kept, away, sus], current_focus=["pkg/query.py"], predecessors=[p],
        incumbent_prior_regressions={"m1": ["/h/m1.spec_tests/t1.py::b"]},
    )
    assert result.kept == [kept.case_id]
    assert {r.route for r in result.routed} == {"inherited.uncovered", "suite_suspect", "regression_by_current"}
    assert result.regressions == ["/h/m1.spec_tests/t1.py::b"]
    assert result.suite_suspect == ["t.py::sus"]
    rec = next(r for r in result.routed if r.route == "regression_by_current")
    assert rec.owner_milestone_id == "m1"
