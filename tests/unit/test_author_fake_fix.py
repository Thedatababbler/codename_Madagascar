"""Author fake-object fix: audit rules (§6.3), accommodation rules (§6.4), training-task reference filter (§6.5, test A5)."""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import pytest

from orchestra.codeprojecteval.suite_audit import fake_object_violations, io_target
from orchestra.control.fast_loop import accommodation as AC
from orchestra.control.fast_loop import reference_filter as RF


def _rules(src: str, cites: dict | None = None) -> list[tuple[str, str]]:
    return [(v.rule, v.case) for v in fake_object_violations(ast.parse(textwrap.dedent(src)), "t.py", cites or {})]


# --------------------------------------------------------------------------- §6.3 audit


def test_io_target_resolution():
    assert io_target("requests.get") == "requests.get"
    assert io_target("pkg.vcs.subprocess.check_output") == "subprocess.check_output"
    assert io_target("os.listdir") == "os.listdir" and io_target("os.sep") == ""
    assert io_target("asyncio.open_connection") == "asyncio.open_connection" and io_target("asyncio.sleep") == ""
    assert io_target("pkg.module.helper") == ""


def test_fake_io_flags_hand_written_doubles_only():
    src = '''
    from unittest import mock
    def test_bad(monkeypatch):
        monkeypatch.setattr("requests.get", lambda url: None)
        monkeypatch.setattr(subprocess, "run", fake_run)
        with mock.patch("socket.create_connection"):
            pass
    def test_good(monkeypatch):
        monkeypatch.setattr("requests.get", mock.create_autospec(requests.get))
        with mock.patch("subprocess.run", autospec=True):
            pass
        monkeypatch.setattr("socket.socket", mock.Mock(spec=socket.socket))
    '''
    got = _rules(src)
    assert got.count(("fake_io", "test_bad")) == 3 and not [g for g in got if g[1] == "test_good"]


def test_env_path_requires_prepend():
    src = '''
    import os
    def test_bad(monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        os.environ["PATH"] = "/x"
    def test_whole(monkeypatch):
        monkeypatch.setattr(os, "environ", {})
    def test_good(monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
        monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    '''
    got = _rules(src)
    assert got.count(("env_path", "test_bad")) == 2 and ("env_path", "test_whole") in got
    assert not [g for g in got if g[1] == "test_good"]


def test_call_assert_needs_a_sentence_about_the_call():
    src = '''
    def test_a(run):
        run.assert_called_once_with(["git", "clone"])
    def test_b(run):
        run.assert_called_once_with(["git", "clone"])
    def test_c(run):
        assert run.call_count == 1
    '''
    cites = {"t.py::test_a": ["returns the cloned directory"], "t.py::test_b": ["runs git clone with the url"],
             "t.py::test_c": ["returns the cloned directory"]}
    got = _rules(src, cites)
    assert ("call_assert", "test_a") in got and ("call_assert", "test_c") in got and ("call_assert", "test_b") not in got


def test_fake_inject_project_objects():
    src = '''
    from unittest.mock import Mock
    class Scripted:
        pass
    def test_x(monkeypatch):
        client = make()
        client._imap = Scripted()
        client.timeout = 5
        client.sock = Mock()
        monkeypatch.setattr(module, "IMAPClient", Scripted)
        monkeypatch.setattr(module, "LIMIT", 3)
    '''
    got = _rules(src)
    assert got.count(("fake_inject", "test_x")) == 3


# --------------------------------------------------------------------------- §6.4 accommodation


PATCH_ONLY_CALL = '''diff --git a/pkg/zipfile.py b/pkg/zipfile.py
--- a/pkg/zipfile.py
+++ b/pkg/zipfile.py
@@ -1,3 +1,3 @@
-    response = requests.get(url, stream=True)
+    response = requests.get(url)
'''
PATCH_LOGIC = '''diff --git a/pkg/find.py b/pkg/find.py
--- a/pkg/find.py
+++ b/pkg/find.py
@@ -1,3 +1,3 @@
-    raise UnknownTemplateDirException(path)
+    raise NonTemplatedInputDirException(path)
'''


def test_accommodation_rules():
    assert AC.rule_verdict(PATCH_ONLY_CALL)[0] == "accommodated"
    assert AC.rule_verdict(PATCH_LOGIC)[0] == "not_accommodated"
    assert AC.rule_verdict(PATCH_ONLY_CALL + PATCH_LOGIC)[0] == "mixed"
    j = AC.judge(PATCH_ONLY_CALL, ["t.py::test_unzip"])
    assert j.verdict == "accommodated" and j.accommodated == ["t.py::test_unzip"]
    j = AC.judge(PATCH_ONLY_CALL + PATCH_LOGIC, ["t.py::a", "t.py::b"],
                 call=lambda s, p: '{"accommodated": ["t.py::a"], "reason": "kw only"}')
    assert j.accommodated == ["t.py::a"]
    assert AC.judge(PATCH_ONLY_CALL + PATCH_LOGIC, ["t.py::a"]).verdict == "undecided"


def test_test_files_do_not_count_as_changes():
    patch = PATCH_LOGIC.replace("pkg/find.py", "tests/test_find.py")
    assert AC.rule_verdict(patch)[0] == "not_accommodated"


# --------------------------------------------------------------------------- §6.5 reference filter (A5)


@pytest.fixture
def frozen(tmp_path):
    d = tmp_path / "harness" / "m.spec_tests"
    d.mkdir(parents=True)
    (d / "test_m.py").write_text("def test_a(): pass\n")
    return d


def test_train_task_removes_reference_failures(frozen, tmp_path, monkeypatch):
    monkeypatch.setattr(RF, "enabled", lambda: True)
    monkeypatch.setattr(RF, "train_tasks", lambda: {"cookiecutter"})
    pending = tmp_path / "pending.yaml"
    out = RF.apply("rb_cookiecutter", frozen, runner=lambda t, f: ["spec_tests/test_m.py::test_a", "spec_tests/test_m.py::Cls::test_b[x]"],
                   pending_path=pending)
    assert out == ["test_m.py::Cls::test_b", "test_m.py::test_a"]
    data = json.loads(RF.suspects_path(frozen).read_text())
    assert data["cases"] == out and "test_m.py::test_a" in pending.read_text()
    # second call reads the file, does not run the reference again
    assert RF.apply("rb_cookiecutter", frozen, runner=lambda t, f: pytest.fail("ran again")) == out


def test_test_task_and_switch_off_do_nothing(frozen, monkeypatch):
    monkeypatch.setattr(RF, "train_tasks", lambda: {"cookiecutter"})
    monkeypatch.setattr(RF, "enabled", lambda: True)
    assert RF.apply("rb_tinydb", frozen, runner=lambda t, f: pytest.fail("test task")) is None
    monkeypatch.setattr(RF, "enabled", lambda: False)
    assert RF.apply("rb_cookiecutter", frozen, runner=lambda t, f: pytest.fail("switch off")) is None
    assert not RF.suspects_path(frozen).exists()


def test_check_script_drops_listed_cases(frozen):
    from orchestra.codeprojecteval import harness as H

    ns: dict = {"__name__": "adamas_check_under_test"}
    exec(compile(H._check_script_source(), "adamas_cpe_check.py", "exec"), ns)   # the script the gate runs
    RF.suspects_path(frozen).write_text(json.dumps({"cases": ["test_m.py::test_a"]}))
    assert ns["_suite_suspects"](frozen) == {"test_m.py::test_a"}
    assert ns["_suspect_key"](str(Path("/x/spec_tests/test_m.py")) + "::test_a[1]") == "test_m.py::test_a"
    assert ns["_suite_suspects"](frozen.parent / "none.spec_tests") == set()
