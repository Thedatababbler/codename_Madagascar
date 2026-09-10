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
