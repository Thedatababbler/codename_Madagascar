"""The four pipeline defects found on 2026-10-15 (docs/reports/repair_evidence_mismatch_20261015.md), one block each.

1. the diagnosis ran the predecessor suites in the task's initial workspace, not on the incumbent's code;
2. the repair evidence staged only the first frozen suite's failures and dropped the rest silently;
3. the gate's result was parsed out of a 4000-character tail of its stdout;
4. a candidate id with a colon named a directory, which split PYTHONPATH (and nothing checked the evidence arrived).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestra.control.fast_loop import repair_evidence as RE
from orchestra.control.fast_loop.controller import DIAGNOSIS_WORKSPACE, FastLoopController, candidate_task_id
from orchestra.control.fast_loop.workspace import GitCandidateWorkspaceManager, path_safe_id

# --------------------------------------------------------------------------- fixtures: two frozen suites + a repo

SUITE_A = '''from spec_tests.helpers import one

def test_add():
    from pkg.core import add
    assert add(one, one) == 2

def test_sub():
    from pkg.core import sub
    assert sub(one, one) == 0
'''
SUITE_B = '''from spec_tests.helpers import two

def test_mul():
    from pkg.core import mul
    assert mul(two, two) == 4

def test_neg():
    from pkg.core import neg
    assert neg(two) == -2
'''


def _two_suites(tmp_path: Path):
    batch = tmp_path / "batch"
    harness = batch / "harness"
    for name, src, helper in (("m1", SUITE_A, "one = 1\n"), ("m2", SUITE_B, "two = 2\n")):
        frozen = harness / f"{name}.spec_tests"
        frozen.mkdir(parents=True)
        (frozen / f"test_{name}.py").write_text(src)
        (frozen / "helpers.py").write_text(helper)
        (frozen / "__init__.py").write_text("")
    (harness / "adamas_cpe_harness.json").write_text(json.dumps({"env_python": sys.executable}))
    repo = batch / "tasks" / "t" / "subtasks" / "m3" / "candidates" / "cand_R0" / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    # sub and neg are wrong
    (repo / "pkg" / "core.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b\n\n"
        "def mul(a, b):\n    return a * b\n\ndef neg(a):\n    return a\n")
    fails = [f"{harness}/m1.spec_tests/test_m1.py::test_sub", f"{harness}/m2.spec_tests/test_m2.py::test_neg",
             f"{harness}/m2.spec_tests/test_m2.py::test_mul"]
    return repo, harness, fails


# --------------------------------------------------------------------------- 2. evidence holds every suite's failures


def test_evidence_stages_every_suite(tmp_path):
    repo, harness, fails = _two_suites(tmp_path)
    ev = RE.build_repair_evidence(repo, fails)
    assert (ev / "m1" / "spec_tests" / "test_m1.py").is_file()
    assert (ev / "m2" / "spec_tests" / "test_m2.py").is_file()
    m = json.loads((ev / RE.MANIFEST).read_text())
    assert m["staged"] == 3 and m["truncated"] == 0 and m["unresolved"] == []
    counts = {s["suite"]: s["counts"] for s in m["suites"]}
    # each suite ran with its own spec_tests package root: the helper imports resolve and the results are real
    assert counts["m1"] == {"passed": 0, "failed": 1, "errors": 0, "skipped": 0}
    assert counts["m2"] == {"passed": 1, "failed": 1, "errors": 0, "skipped": 0}
    readme = (ev / "README.md").read_text()
    assert "test_m1.py::test_sub" in readme and "test_m2.py::test_neg" in readme
    assert readme.count("-m pytest") == 2          # one command per suite


def test_evidence_truncation_is_stated(tmp_path):
    repo, harness, fails = _two_suites(tmp_path)
    ev = RE.build_repair_evidence(repo, fails, max_cases=1)
    m = json.loads((ev / RE.MANIFEST).read_text())
    assert m["staged"] == 1 and m["truncated"] == 2 and len(m["not_staged"]) == 2
    readme = (ev / "README.md").read_text()
    assert "Truncated:** 2 of 3" in readme


def test_evidence_names_failures_it_could_not_find(tmp_path):
    repo, harness, fails = _two_suites(tmp_path)
    ev = RE.build_repair_evidence(repo, [*fails, "gone/test_x.py::test_y"])
    m = json.loads((ev / RE.MANIFEST).read_text())
    assert m["unresolved"] == ["gone/test_x.py::test_y"]
    assert "Not found:** 1" in (ev / "README.md").read_text()
    check = RE.verify_delivery(repo, ev, expected=4)
    assert not check["ok"] and "not found in any frozen suite" in " ".join(check["problems"])


# --------------------------------------------------------------------------- 4b. the delivery check


def test_delivery_check_passes_complete_evidence(tmp_path):
    repo, harness, fails = _two_suites(tmp_path)
    ev = RE.build_repair_evidence(repo, fails)
    check = RE.verify_delivery(repo, ev, expected=3)
    assert check["ok"], check["problems"]


def test_delivery_check_refuses_missing_and_empty(tmp_path):
    repo, harness, fails = _two_suites(tmp_path)
    assert not RE.verify_delivery(repo, None)["ok"]
    empty = repo.parent / RE.EVIDENCE_DIRNAME
    empty.mkdir()
    check = RE.verify_delivery(repo, empty)
    assert not check["ok"] and "manifest" in check["problems"][0]


def test_delivery_check_refuses_evidence_that_collected_nothing(tmp_path):
    repo, harness, fails = _two_suites(tmp_path)
    ev = RE.build_repair_evidence(repo, fails)
    m = json.loads((ev / RE.MANIFEST).read_text())
    m["suites"][0]["counts"] = {"passed": 0, "failed": 0, "errors": 1, "skipped": 0}   # "found no collectors"
    (ev / RE.MANIFEST).chmod(0o600)
    (ev / RE.MANIFEST).write_text(json.dumps(m))
    check = RE.verify_delivery(repo, ev, expected=3)
    assert not check["ok"] and "collected" in " ".join(check["problems"])


def test_delivery_check_refuses_a_short_hand_over(tmp_path):
    repo, harness, fails = _two_suites(tmp_path)
    ev = RE.build_repair_evidence(repo, fails[:1])
    check = RE.verify_delivery(repo, ev, expected=3)
    assert not check["ok"] and "accounted for" in " ".join(check["problems"])


@pytest.fixture
def real_sandbox(monkeypatch):
    from orchestra.sandbox import guard as G
    from orchestra.sandbox import policy as SB

    monkeypatch.delenv(SB.TEST_UNCONFINED_ENV, raising=False)
    G._READY.clear()
    yield
    G._READY.clear()


def test_delivery_check_reads_as_the_repairer_under_the_sandbox(tmp_path, real_sandbox):
    repo, harness, fails = _two_suites(tmp_path)
    ev = RE.build_repair_evidence(repo, fails)
    assert RE.verify_delivery(repo, ev, expected=3)["readable"] is True
    # evidence that is not where the repairer's policy grants it (beside its repo) cannot be read
    other = tmp_path / "elsewhere" / "repo"
    other.mkdir(parents=True)
    check = RE.verify_delivery(other, ev, expected=3)
    assert check["readable"] is not True and not check["ok"]


def test_relocated_evidence_names_its_new_paths(tmp_path):
    repo, harness, fails = _two_suites(tmp_path)
    ev = RE.build_repair_evidence(repo, fails)
    sample_repo = tmp_path / "sample" / "repo"
    sample_repo.mkdir(parents=True)
    import shutil

    dst = sample_repo.parent / RE.EVIDENCE_DIRNAME
    shutil.copytree(ev, dst)
    RE.relocate_evidence(dst, src=ev, repo=sample_repo)
    readme = (dst / "README.md").read_text()
    assert str(ev) not in readme and str(dst) in readme


def test_delivery_audit_flags_a_repairer_that_ran_without_evidence(tmp_path):
    from orchestra.memory.runtime import evidence_delivery

    nodes = [{"task_id": "rb_t__m1__candidate__cand_R0"}, {"task_id": "rb_t__m1__candidate__cand_v2-E3-T1"},
             {"task_id": "rb_t__m1__candidate__cand_feedback"}]
    RE.record_evidence_delivery(tmp_path, "cand_R0", {"ok": True, "problems": [], "staged": 3, "truncated": 0})
    out = evidence_delivery(tmp_path, nodes)
    assert out["problems"] == ["repairer cand_v2-E3-T1 ran with no recorded evidence hand-over"]
    RE.record_evidence_delivery(tmp_path, "cand_v2-E3-T1", {"ok": False, "problems": ["evidence directory missing"]})
    out = evidence_delivery(tmp_path, nodes)
    assert out["problems"] == ["repairer cand_v2-E3-T1 ran although its evidence failed the delivery check"]


# --------------------------------------------------------------------------- 4a. no colon in a directory name


def test_candidate_ids_are_path_safe():
    assert path_safe_id("cand_v2:E3-T1") == "cand_v2-E3-T1"
    assert path_safe_id(path_safe_id("cand_v2:E3-T1")) == "cand_v2-E3-T1"
    assert os.pathsep not in path_safe_id("a:b/c d")
    assert candidate_task_id("rb_t", "m1", "cand_v2:E3-T1") == "rb_t__m1__candidate__cand_v2-E3-T1"
    p = GitCandidateWorkspaceManager()._candidate_path("/r", "rb_t", "m1", "cand_v2:E3-T1")
    assert ":" not in str(p) and p.parent.name == "cand_v2-E3-T1"


def test_playbook_rows_get_a_path_safe_candidate_id():
    import inspect

    from orchestra.control.fast_loop import playbook_generator as PG

    src = inspect.getsource(PG.PlaybookCandidateGenerator)
    assert 'path_safe_id(f"cand_{playbook.playbook_id}")' in src


# --------------------------------------------------------------------------- 3. structured result, never a stdout tail

FAKE_CHECK = textwrap.dedent('''
    import argparse, json, sys
    p = argparse.ArgumentParser()
    p.add_argument("--manifest"); p.add_argument("--spec-tests"); p.add_argument("--results-file", default="")
    a, _ = p.parse_known_args()
    ids = ["spec_tests/test_big.py::test_case_%03d" % i for i in range(60)]
    payload = {"score": 0.5, "level": "integration", "furthest_stage": "spec_tests",
               "stages": [{"stage": "compile", "passed_units": 1, "total_units": 1},
                          {"stage": "spec_tests", "passed_units": 30, "total_units": 60,
                           "passed_tests": ids[:30], "failed_tests": ids[30:]}]}
    if a.results_file:
        open(a.results_file, "w").write(json.dumps(payload))
    print("ADAMAS_HARNESS_SCORE " + json.dumps(payload))
    print("x" * 5000)       # noise after the score line: a 4000-character tail no longer holds it
''')


@pytest.fixture
def fake_check(tmp_path, monkeypatch):
    monkeypatch.setenv("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", "1")
    harness = tmp_path / "harness"
    harness.mkdir()
    script = harness / "adamas_cpe_check.py"
    script.write_text(FAKE_CHECK)
    ws = tmp_path / "ws"
    ws.mkdir()
    return [sys.executable, str(script), "--manifest", str(harness / "m.json"), "--spec-tests", str(harness)], ws


def test_run_harness_reads_the_results_file_not_the_tail(fake_check):
    from orchestra.harness.command_runner import run_harness
    from orchestra.harness.progress import parse_progress, progress_from_payload

    cmd, ws = fake_check
    run = asyncio.run(run_harness(cwd=ws, command=cmd, timeout_seconds=60))
    assert parse_progress(run.stdout)[1] == []          # the tail lost the score line, as on 10-12
    score, stages, _ = progress_from_payload(run.payload)
    assert score == 0.5 and stages[-1].total_units == 60 and len(stages[-1].failed_tests) == 30


def test_prior_suite_check_uses_the_structured_result(fake_check, tmp_path):
    from orchestra.control.fast_loop.persistence import failure_key
    from orchestra.control.fast_loop.prior_suites import PriorSuite, run_prior_suite

    cmd, ws = fake_check
    ids = [failure_key(f"spec_tests/test_big.py::test_case_{i:03d}") for i in range(60)]   # as committed_cases records them
    prior = PriorSuite("m0", str(tmp_path / "harness"), "", "integration", frozenset(ids), frozenset())
    out = asyncio.run(run_prior_suite(workspace_path=str(ws), command=cmd, prior=prior, timeout_seconds=60))
    assert out["ran"] is True and out["passed_count"] == 30 and len(out["regressions"]) == 30


def test_prior_suite_without_a_result_says_so(tmp_path, monkeypatch):
    from orchestra.control.fast_loop.prior_suites import PriorSuite, run_prior_suite

    monkeypatch.setenv("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", "1")
    harness = tmp_path / "harness"
    harness.mkdir()
    script = harness / "adamas_cpe_check.py"
    script.write_text("import sys\n# --results-file accepted but never written\nsys.argv; print('crash')\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    prior = PriorSuite("m0", str(harness), "", "integration", frozenset({"a.py::t"}), frozenset())
    out = asyncio.run(run_prior_suite(workspace_path=str(ws), command=[sys.executable, str(script)], prior=prior,
                                      timeout_seconds=60))
    assert out["ran"] is False and "no structured result" in out["error"] and out["regressions"] == []


def test_check_script_writes_its_results_file(tmp_path):
    from orchestra.codeprojecteval.harness import _check_script_source

    script = tmp_path / "harness" / "adamas_cpe_check.py"
    script.parent.mkdir()
    script.write_text(_check_script_source())
    (tmp_path / "harness" / "m.json").write_text(json.dumps({"expected_modules": ["pkg"], "top_level_packages": ["pkg"]}))
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("x = 1\n")
    out = tmp_path / "results.json"
    import subprocess

    subprocess.run([sys.executable, str(script), "--manifest", str(tmp_path / "harness" / "m.json"), "--level", "discovery",
                    "--results-file", str(out)], cwd=repo, capture_output=True, text=True, timeout=120)
    payload = json.loads(out.read_text())
    assert payload["stages"][0]["stage"] == "compile" and payload["stages"][0]["passed_units"] == 1


def test_check_script_reads_pytest_results_from_junit(tmp_path):
    from orchestra.codeprojecteval.harness import _check_script_source

    ns: dict = {"__name__": "check_probe", "__file__": str(tmp_path / "adamas_cpe_check.py")}
    exec(compile(_check_script_source(), "adamas_cpe_check.py", "exec"), ns)
    xml = tmp_path / "r.xml"
    xml.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="spec_tests.test_a" name="test_ok" file="spec_tests/test_a.py"/>'
        '<testcase classname="spec_tests.test_a.TestK" name="test_bad[1]" file="spec_tests/test_a.py"><failure message="x"/></testcase>'
        '<testcase classname="spec_tests.test_a" name="test_skip" file="spec_tests/test_a.py"><skipped/></testcase>'
        '<testcase classname="" name="spec_tests/test_b.py"><error message="collection failure"/></testcase>'
        '</testsuite></testsuites>')
    passed, failed, errors = ns["_junit_results"](str(xml))
    assert passed == {"spec_tests/test_a.py::test_ok"}
    assert failed == {"spec_tests/test_a.py::TestK::test_bad[1]", "spec_tests/test_b.py"} and errors == 1


# --------------------------------------------------------------------------- 1. the diagnosis runs on the incumbent's code


class _FakeWorkspaces:
    def __init__(self, root: Path):
        self.root = root
        self.forked: list[str] = []
        self.patched: list[str] = []

    async def fork_candidate_workspace(self, *, base, run_dir, task_id, subtask_id, candidate_id):
        self.forked.append(candidate_id)
        p = self.root / candidate_id / "repo"
        p.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(path=str(p))

    async def apply_patch(self, ws, patch):
        self.patched.append(patch)


class _FakeStore:
    async def get(self, artifact_id):
        return SimpleNamespace(payload={"patch": f"patch-of-{artifact_id}"})


def _controller(tmp_path):
    ctl = FastLoopController.__new__(FastLoopController)
    ctl.workspace_manager = _FakeWorkspaces(tmp_path / "ws")
    ctl.artifact_store = _FakeStore()
    return ctl


def test_diagnosis_workspace_is_the_base_plus_the_incumbent_patch(tmp_path):
    ctl = _controller(tmp_path)
    fl = SimpleNamespace(candidates=[], notes=[])
    repo = asyncio.run(ctl._diagnosis_repo(
        state=SimpleNamespace(task_id="rb_t"), fl_state=fl, subtask_id="m3", base_ws=SimpleNamespace(path="/base"),
        incumbent_artifact="art-1", context=SimpleNamespace(run_dir=str(tmp_path))))
    assert repo == tmp_path / "ws" / DIAGNOSIS_WORKSPACE / "repo"
    assert ctl.workspace_manager.patched == ["patch-of-art-1"]


def test_diagnosis_workspace_from_a_recovered_candidate(tmp_path):
    ctl = _controller(tmp_path)
    fl = SimpleNamespace(candidates=[SimpleNamespace(candidate_id="cand_feedback", patch="probe-patch")], notes=[])
    asyncio.run(ctl._diagnosis_repo(
        state=SimpleNamespace(task_id="rb_t"), fl_state=fl, subtask_id="m3", base_ws=SimpleNamespace(path="/base"),
        incumbent_artifact="candidate:cand_feedback", context=SimpleNamespace(run_dir=str(tmp_path))))
    assert ctl.workspace_manager.patched == ["probe-patch"]


def test_diagnosis_never_falls_back_to_the_source_repo(tmp_path):
    ctl = _controller(tmp_path)
    fl = SimpleNamespace(candidates=[], notes=[], routing={})
    ctl._reference_filter = lambda **_: []
    initial = tmp_path / "initial_workspace"          # dataset inputs only, as on 10-12
    initial.mkdir()
    summary = SimpleNamespace(persistent=["a.py::t"], flaky=[])
    repair, classes = asyncio.run(ctl._route_and_classify(
        state=SimpleNamespace(task_id="rb_t", subtasks={}), fl_state=fl, sub=SimpleNamespace(), subtask_id="m3",
        base_graph=None, summary=summary, source_repo=str(initial), base_ws=SimpleNamespace(path="/base"),
        context=SimpleNamespace(run_dir=str(tmp_path)), incumbent_artifact=None))
    assert repair == ["a.py::t"] and classes == []
    assert fl.routing["diagnosis_workspace"] == ""
    assert any("no incumbent workspace" in n for n in fl.notes)
    assert ctl.workspace_manager.forked == []


def test_prior_suite_on_a_tree_that_does_not_compile_is_not_a_regression(tmp_path, monkeypatch):
    from orchestra.control.fast_loop.prior_suites import PriorSuite, run_prior_suite

    monkeypatch.setenv("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", "1")
    harness = tmp_path / "harness"
    harness.mkdir()
    script = harness / "adamas_cpe_check.py"
    script.write_text(textwrap.dedent('''
        import argparse, json
        p = argparse.ArgumentParser(); p.add_argument("--results-file", default=""); a, _ = p.parse_known_args()
        payload = {"score": 0.0, "stages": [{"stage": "compile", "passed_units": 0, "total_units": 1},
                   {"stage": "spec_tests", "passed_units": 0, "total_units": 2, "failed_tests": ["t.py::a", "t.py::b"]}]}
        open(a.results_file, "w").write(json.dumps(payload))
    '''))
    ws = tmp_path / "ws"
    ws.mkdir()
    prior = PriorSuite("m0", str(harness), "", "integration", frozenset({"t.py::a", "t.py::b"}), frozenset())
    out = asyncio.run(run_prior_suite(workspace_path=str(ws), command=[sys.executable, str(script)], prior=prior,
                                      timeout_seconds=60))
    assert out["regressions"] == [] and out["ran"] is False and "does not compile" in out["error"]


# --------------------------------------------------------------------------- evidence-invalid candidates leave every statistic


def test_marked_candidates_leave_the_ledger(tmp_path, monkeypatch):
    from orchestra.control.evolution import ledger as L

    marks = tmp_path / "evidence_invalid.jsonl"
    monkeypatch.setattr(L, "EVIDENCE_INVALID", marks)
    run = tmp_path / "batch" / "cpe-x-t" / "t"
    cands = tmp_path / "candidates.jsonl"
    cands.write_text("\n".join(json.dumps(r) for r in [
        {"record_id": "t:m1:cand_R0", "milestone_id": "m1", "run_dir": str(run)},
        {"record_id": "t:m1:cand_v2:E3-T1", "milestone_id": "m1", "run_dir": str(run)},
        {"record_id": "t:m2:cand_R0", "milestone_id": "m2", "run_dir": str(run)},
    ]) + "\n")
    assert L.mark_evidence_invalid([{"run_dir": str(run), "milestone_id": "m1", "candidate_id": "cand_v2:E3-T1", "reason": "x"},
                                    {"run_dir": str(run), "milestone_id": "m1", "candidate_id": "cand_R0", "reason": "x"}]) == 2
    assert L.mark_evidence_invalid([{"run_dir": str(run), "milestone_id": "m1", "candidate_id": "cand_R0", "reason": "x"}]) == 0
    kept = [r["record_id"] for r in L.read_jsonl(cands)]
    assert kept == ["t:m2:cand_R0"]
    assert len(L.read_jsonl(cands, include_invalid=True)) == 3
    assert L.is_evidence_invalid(str(run / "tasks" / "rb_t"), "m1", "cand_R0")      # a path under the marked run


def test_transfer_skips_marked_candidates():
    import inspect

    from orchestra.memory import transfer as T

    assert "is_evidence_invalid(run, mid, c[\"candidate_id\"])" in inspect.getsource(T.collect_facts)


# --------------------------------------------------------------------------- bplustree: /tmp for the gate only


def _bplustree_manifest(tmp_path):
    m = tmp_path / "adamas_cpe_harness.json"
    m.write_text(json.dumps({"source_dir": "bplustree", "top_level_packages": ["bplustree"]}))
    return m


def test_gate_tmp_applies_to_listed_tasks_only(tmp_path):
    from orchestra.sandbox import gate_tmp

    assert gate_tmp.rules() == {"bplustree": ["/tmp/bplustree-testfile*"]}
    m = _bplustree_manifest(tmp_path)
    assert gate_tmp.task_of_command(["py", "adamas_cpe_check.py", "--manifest", str(m)]) == "bplustree"
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"source_dir": "tinydb", "top_level_packages": ["tinydb"]}))
    assert gate_tmp.task_of_command(["py", "adamas_cpe_check.py", "--manifest", str(other)]) is None
    with gate_tmp.session("tinydb") as grant:
        assert grant == []


def test_gate_tmp_session_cleans_before_and_after():
    from orchestra.sandbox import gate_tmp

    stale = Path("/tmp/bplustree-testfile-check.index")
    stale.write_text("left by an earlier run")
    with gate_tmp.session("bplustree") as grant:
        assert grant == ["/tmp"] and not stale.exists()
        stale.write_text("written by this run")
    assert not stale.exists()


def test_agents_never_get_tmp(tmp_path):
    from orchestra.sandbox import policy as SB

    pol = SB.agent_policy(workspace=tmp_path / "ws", private=tmp_path / "p", venv=None)
    assert "/tmp" not in pol.write and "/tmp" not in pol.read


def test_confined_gate_writes_tmp_only_with_the_grant(tmp_path, real_sandbox):
    import subprocess

    from orchestra.sandbox import gate_tmp
    from orchestra.sandbox import policy as SB

    probe = ["/usr/bin/python3", "-I", "-c", "open('/tmp/bplustree-testfile-probe.index', 'w').write('x')"]
    ws = tmp_path / "ws"
    ws.mkdir()
    cmd, env, _ = SB.confine_command(probe, cwd=ws, env={"PATH": "/usr/bin:/bin"})
    assert subprocess.run(cmd, cwd=ws, env=env, capture_output=True).returncode != 0      # no grant: refused
    with gate_tmp.session("bplustree") as grant:
        cmd, env, _ = SB.confine_command(probe, cwd=ws, env={"PATH": "/usr/bin:/bin"}, extra_write=grant)
        r = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert Path("/tmp/bplustree-testfile-probe.index").exists()
    assert not Path("/tmp/bplustree-testfile-probe.index").exists()                      # cleaned after


# --------------------------------------------------------------------------- NL2Repo: behaviour inventory off, review on


def test_inventory_off_gives_an_empty_saved_inventory(tmp_path):
    from orchestra.control.author import assemble as A
    from orchestra.control.fast_loop.evolution_config import AuthorConfig

    assert AuthorConfig.from_mapping({"enabled": True, "inventory": True, "inventory_extract": False}).inventory_extract is False
    assert AuthorConfig.from_mapping({"enabled": True, "inventory": True}).inventory_extract is True
    m = SimpleNamespace(milestone_id="m1", objective="o", focus_paths=["a.py"], acceptance=SimpleNamespace(criteria=[]))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "PRD.md").write_text("x")
    env = {A.INVENTORY_DIR_ENV: str(tmp_path / "inv"), A.DOCS_DIR_ENV: str(docs), A.TASK_ENV: "t", A.INVENTORY_OFF_ENV: "1"}
    inv = A.milestone_inventory(m, env=env)          # no model call: it would fail without credentials
    assert inv.items == [] and (tmp_path / "inv" / "m1.inventory.json").is_file()


def test_empty_inventory_keeps_every_case_hard_and_coverage_full():
    from orchestra.codeprojecteval.behaviour_inventory import Inventory
    from orchestra.control.author.review import coverage, soft_cases

    inv = Inventory(task="t", milestone_id="m1")
    audit = SimpleNamespace(citations={"spec_tests/test_a.py::test_x": ["q"], "spec_tests/test_a.py::test_y": []},
                            soft_cases=["spec_tests/test_a.py::test_y"])
    cov = coverage(audit, inv)
    assert cov.hard_ratio == 1.0
    assert soft_cases(audit, cov, inv) == ["spec_tests/test_a.py::test_y"]    # only the fixed rule's soft case
