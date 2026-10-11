"""Fixture-preparation check (2026-10-11): fixtures are set up once in the task environment before the gate suite
is frozen; a failing setup or a plugin fixture that is None there goes back to the author."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.codeprojecteval.harness import TESTKIT_SRC
from orchestra.control.author.fixture_check import check, parse

SIMPLEJWT_PY = Path("/root/codex-benchmarks/cpe_envs/djangorestframework-simplejwt/bin/python")


def _suite(tmp_path: Path, body: str) -> Path:
    ws = tmp_path / "ws"
    (ws / "spec_tests").mkdir(parents=True)
    (ws / "spec_tests" / "test_x.py").write_text(body)
    return ws


@pytest.mark.skipif(not SIMPLEJWT_PY.exists(), reason="task environment missing")
def test_none_plugin_fixture_and_failing_setup_are_flagged(tmp_path):
    ws = _suite(tmp_path, '''import pytest

@pytest.fixture
def unblocked(django_db_blocker):
    with django_db_blocker.unblock():
        yield

def test_a(unblocked):
    pass

def test_b(django_db_blocker):
    pass
''')
    v, _ = check(ws, python=SIMPLEJWT_PY, packages=["rest_framework_simplejwt"], testkit=TESTKIT_SRC)
    rules = {(x.rule, x.case) for x in v}
    assert ("fixture_none", "test_a") in rules and ("fixture_none", "test_b") in rules
    assert ("fixture_setup", "test_a") in rules


@pytest.mark.skipif(not SIMPLEJWT_PY.exists(), reason="task environment missing")
def test_missing_project_package_is_tolerated_other_errors_are_not(tmp_path):
    ws = _suite(tmp_path, '''import pytest

@pytest.fixture
def client():
    from rest_framework_simplejwt.tokens import AccessToken   # the project does not exist yet
    return AccessToken

@pytest.fixture
def broken():
    raise ValueError("fixture bug")

def test_project(client):
    pass

def test_broken(broken):
    pass

def test_plain():
    pass
''')
    v, _ = check(ws, python=SIMPLEJWT_PY, packages=["rest_framework_simplejwt"], testkit=TESTKIT_SRC)
    assert [(x.rule, x.case) for x in v] == [("fixture_setup", "test_broken")]


def test_parse_reads_errors_and_none_lines():
    out = ("ADAMAS_FIXTURE_NONE django_db_blocker spec_tests/test_x.py::test_a\n"
           "ERROR spec_tests/test_x.py::test_a - AttributeError: 'NoneType' object has no attribute 'unblock'\n"
           "ERROR spec_tests/test_x.py::test_b - ModuleNotFoundError: No module named 'pkg'\n")
    v = parse(out, ["pkg"])
    assert sorted((x.rule, x.case) for x in v) == [("fixture_none", "test_a"), ("fixture_setup", "test_a")]
