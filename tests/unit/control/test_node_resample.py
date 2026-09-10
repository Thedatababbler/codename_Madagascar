import json
from pathlib import Path

from orchestra.control.fast_loop.node_resample import (
    Sample, WriterStep, blame_v1, consensus_select, suffix_graph, writer_steps,
)
from orchestra.ir.graph import OrchestraGraph

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "node_resample_graph.json"
P1 = "diff --git a/src/tablib/a.py b/src/tablib/a.py\n--- a/src/tablib/a.py\n+++ b/src/tablib/a.py\n@@\n+x = 1\n"
P2 = P1 + "diff --git a/src/tablib/b.py b/src/tablib/b.py\n--- a/src/tablib/b.py\n+++ b/src/tablib/b.py\n@@\n+y = 2\n"
P3 = P2.replace("+x = 1", "+x = 3")
SPEC = "diff --git a/spec_tests/test_x.py b/spec_tests/test_x.py\n--- a/spec_tests/test_x.py\n+++ b/spec_tests/test_x.py\n@@\n+def test_a(): pass\n"


def _graph() -> OrchestraGraph:
    return OrchestraGraph.model_validate(json.loads(FIX.read_text()))


def _ids(g):
    return [n.node_id for n in g.nodes if n.node_id.startswith("agent_")]


def test_writer_steps_from_cumulative_patches():
    g = _graph(); a = _ids(g)   # test_author, implementer, spec_auditor, contract_critic, gate_repairer
    steps = writer_steps(g, {a[0]: ("s", SPEC), a[1]: ("i", SPEC + P2), a[2]: ("r", SPEC + P2), a[3]: ("c", SPEC + P2), a[4]: ("f", SPEC + P3)})
    assert [s.node_id for s in steps] == [a[0], a[1], a[4]]          # read-only reviewers produce no step
    assert steps[0].is_author and steps[1].files == {"src/tablib/a.py", "src/tablib/b.py"} and steps[2].files == {"src/tablib/a.py"}


def test_blame_earliest_owner_and_positions():
    steps = [WriterStep("t", "s", SPEC, {"spec_tests/test_x.py"}, True), WriterStep("impl", "i", P2, {"src/tablib/a.py", "src/tablib/b.py"}), WriterStep("fix", "f", P3, {"src/tablib/a.py"})]
    b = blame_v1(["t::test_b", "t::test_c"], {"t::test_b": "src/tablib/b.py", "t::test_c": "src/tablib/a.py"}, steps)
    assert (b.node_id, b.position) == ("impl", "first")
    assert blame_v1(["t::test_c"], {"t::test_c": "src/tablib/a.py"}, steps).position == "last"
    assert blame_v1(["t::test_z"], {}, steps).node_id == "fix"          # unowned -> last writer
    assert blame_v1(["t::x"], {}, steps, suite_collected_zero=True).position == "author"


def test_suffix_graph_injects_dropped_outputs():
    g = _graph(); a = _ids(g)
    outputs = {a[0]: {"repository_change": "art_spec"}, "authored_suite_custody": {"result": "art_custody"}}
    new, injected, dropped = suffix_graph(g, a[1], outputs)
    assert a[0] in dropped and "authored_suite_custody" in dropped and a[1] not in dropped
    assert injected == {"suite_custody": "art_custody"}
    assert new.initial_artifact_slots["suite_custody"] == "RepositoryHarnessResultArtifact"
    assert all(e.source_node not in dropped for e in new.edges)
    assert [n.node_id for n in new.nodes][0] == a[1]


def test_consensus_prefers_agreement_within_epsilon():
    s = [Sample(0, True, 1.0, 0.90, {"a"}), Sample(1, True, 1.0, 0.90, {"a"}), Sample(2, True, 1.0, 0.91, {"b", "c"}), Sample(3, False, 0.5, 0.95, set())]
    chosen, agreement, consensus, _ = consensus_select(s)
    assert chosen.index in (0, 1) and consensus == {"a"} and 0 < agreement <= 1
    assert consensus_select([Sample(0, False, None, None, set())])[0] is None


def test_v2_helpers(tmp_path):
    from orchestra.control.fast_loop.node_resample import (
        condemned_suite_feedback, frozen_spec_dir, reauthor_graph, repo_symbol_index, should_stop, symbols_in_test, targeted_feedback,
    )
    (tmp_path / "pkg").mkdir(); (tmp_path / "pkg" / "core.py").write_text("class GitWildMatchPattern:\n    pass\n\ndef helper():\n    pass\n")
    t = tmp_path / "test_x.py"; t.write_text("def test_regex():\n    assert GitWildMatchPattern.pattern_to_regex('*') == 'x'\n")
    idx = repo_symbol_index(tmp_path); assert idx["GitWildMatchPattern"] == "pkg/core.py"
    assert "GitWildMatchPattern" in symbols_in_test(t, "test_regex")
    assert "target" in targeted_feedback(["a.py::test_b"]) and "repair_evidence" in targeted_feedback(["a.py::test_b"])
    assert should_stop([Sample(0, True, 1.0, 0.5, {"a"}), Sample(1, True, 1.0, 0.5, {"a"})], 0.5)
    assert should_stop([Sample(0, True, 1.0, 0.5, {"a"}), Sample(1, True, 1.0, 0.7, {"a"})], 0.5) is None
    assert should_stop([Sample(0, True, 1.0, 0.5, {"a"}), Sample(1, True, 1.0, 0.5, {"b"})], 0.5) is None
    g = _graph(); old = frozen_spec_dir(g); assert old and old.endswith(".spec_tests")
    g2 = reauthor_graph(g, _ids(g)[0], old + ".reauthor", condemned_suite_feedback(["x.py::t"], 3))
    assert frozen_spec_dir(g2) == old + ".reauthor" and "condemned" not in old
    assert "common cause is in the suite" in [n for n in g2.nodes if n.node_id == _ids(g)[0]][0].prompt_feedback


def test_primary_failed_node_uses_graph_order():
    from orchestra.control.fast_loop.diagnosis import _primary_failed_node
    g = _graph()
    ids = _ids(g)
    author, last = ids[0], ids[-1]
    # dictionary order lists the author last; graph order must still pick the last editing agent
    assert _primary_failed_node([last, author, "authored_suite_custody", "repository_tests"], g) == last
    assert _primary_failed_node([author, last], g) == last


def test_v2_reauthor_helpers(tmp_path):
    from orchestra.control.fast_loop.node_resample import blame_v1, gradable_count, with_spec_dir, frozen_spec_dir
    g = _graph(); old = frozen_spec_dir(g)
    g2 = with_spec_dir(g, old + ".reauthor_s1"); assert frozen_spec_dir(g2) == old + ".reauthor_s1"
    d = tmp_path / "ms.spec_tests"; (tmp_path / "ms.spec_tests.baseline.json").write_text('{"vacuous": 7, "collected": 10}')
    assert gradable_count(str(d)) == 3 and gradable_count(str(tmp_path / "none")) is None
    steps = [WriterStep("a1", "art", "", {"spec_tests/t.py"}, True), WriterStep("a2", "art2", "", {"pkg/x.py"}, False)]
    b = blame_v1(["t.py::test_x"], {}, steps, suite_collected_zero=True)
    assert b.position == "author" and "every gradable case" in b.reason
    b2 = blame_v1(["spec_tests/t.py"], {}, steps); assert b2.position == "author" and "collects nothing" in b2.reason


def test_routing_helpers_and_table_row():
    from orchestra.control.fast_loop.node_resample import FLAKY_SHARE, flaky_feedback, node_position, owner_share
    from orchestra.control.fast_loop.playbooks import QUALITY_CATALOG, playbook_applies
    steps = [WriterStep("author", "a", "", {"spec_tests/t.py"}, True), WriterStep("impl", "i", "", {"pkg/a.py", "pkg/b.py"}, False), WriterStep("fix", "f", "", {"pkg/b.py"}, False)]
    frames = {"t.py::x": "pkg/a.py", "t.py::y": "pkg/a.py", "t.py::z": "pkg/b.py"}
    node, share, owners = owner_share(["t.py::x", "t.py::y", "t.py::z"], frames, steps)
    assert node == "impl" and abs(share - 2 / 3) < 1e-9 and owners["t.py::z"] == "fix" and share >= FLAKY_SHARE
    node2, share2, _ = owner_share(["t.py::x", "t.py::z"], frames, steps)
    assert share2 == 0.5  # spread: below the precondition
    assert node_position("impl", steps) == "first" and node_position("fix", steps) == "last"
    assert "unstable" in flaky_feedback(["t.py::x"]) and "repair_evidence" in flaky_feedback(["t.py::x"])
    row = next(r for r in QUALITY_CATALOG if r.playbook_id == "pb_q_node_resample")
    assert row.controller_built and row.precondition == "flaky_concentrated"
    class Ctx:  # the generator must skip a controller-built row whatever the context says
        behaviour_failures = ["t.py::x"]
    assert playbook_applies(row, Ctx()) is False


def test_condemned_feedback_distinguishes_collect_nothing():
    from orchestra.control.fast_loop.node_resample import condemned_suite_feedback
    assert "collected NO test" in condemned_suite_feedback(["spec_tests/test_x.py"], 3)
    assert "collected NO test" not in condemned_suite_feedback(["spec_tests/test_x.py::test_a"], 3)
