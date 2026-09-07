"""The repairer gets exactly the persistent tests, their output, and a command."""

import json
import os
import stat
import sys
from pathlib import Path

from orchestra.control.fast_loop.repair_evidence import (
    EVIDENCE_DIRNAME,
    build_repair_evidence,
    resolve_failure,
    strip_to_tests,
)

SUITE = '''import pytest
from spec_tests.helpers import shared

@pytest.fixture
def one():
    return 1

# PRD: "add returns the sum"
def test_add(one):
    from pkg.core import add
    assert add(one, one) == 2

# PRD: "sub returns the difference"
def test_sub(one):
    from pkg.core import sub
    assert sub(one, one) == 0

class TestMul:
    def test_mul(self):
        from pkg.core import mul
        assert mul(2, 3) == 6

    def test_mul_zero(self):
        from pkg.core import mul
        assert mul(2, 0) == 0

class TestDiv:
    def test_div(self):
        assert 1
'''


def _stage(tmp_path: Path):
    batch = tmp_path / "batch"
    frozen = batch / "harness" / "m1.spec_tests"
    frozen.mkdir(parents=True)
    (frozen / "test_ops.py").write_text(SUITE)
    (frozen / "helpers.py").write_text("shared = 1\n")
    (frozen / "__init__.py").write_text("")
    (batch / "harness" / "adamas_cpe_harness.json").write_text(json.dumps({"env_python": sys.executable}))
    repo = batch / "tasks" / "t" / "subtasks" / "m1" / "candidates" / "c1" / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    # add is right, sub is wrong, mul is right
    (repo / "pkg" / "core.py").write_text("def add(a, b):\n    return a + b\n\ndef sub(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n")
    rel = os.path.relpath(frozen / "test_ops.py", repo)
    return repo, frozen, rel


def test_strip_keeps_named_tests_helpers_and_fixtures():
    out = strip_to_tests(SUITE, {"test_sub", "TestMul::test_mul_zero"})
    assert "def test_sub" in out and "def test_add" not in out
    assert "def test_mul_zero" in out and "def test_mul(" not in out
    assert "class TestDiv" not in out
    assert "def one" in out and "from spec_tests.helpers import shared" in out
    assert '# PRD: "sub returns the difference"' in out


def test_resolve_failure_finds_frozen_root(tmp_path):
    repo, frozen, rel = _stage(tmp_path)
    got = resolve_failure(repo, f"{rel}::test_sub")
    assert got == (frozen.resolve(), "test_ops.py", "test_sub")
    assert resolve_failure(repo, "nowhere/test_x.py::test_y") is None


def test_build_stages_only_persistent_tests_and_runs_them(tmp_path):
    repo, frozen, rel = _stage(tmp_path)
    evidence = build_repair_evidence(repo, [f"{rel}::test_sub", f"{rel}::TestMul::test_mul"])
    assert evidence == repo.parent / EVIDENCE_DIRNAME
    assert not (repo / EVIDENCE_DIRNAME).exists()  # beside the repo, never inside
    staged = (evidence / "spec_tests" / "test_ops.py").read_text()
    assert "def test_sub" in staged and "def test_add" not in staged
    assert (evidence / "spec_tests" / "helpers.py").exists()
    readme = (evidence / "README.md").read_text()
    assert "::test_sub" in readme and "::TestMul::test_mul" in readme and "-m pytest" in readme
    failures = (evidence / "failures.md").read_text()
    assert "test_sub" in failures and "1 failed, 1 passed" in failures
    mode = stat.S_IMODE((evidence / "spec_tests" / "test_ops.py").stat().st_mode)
    assert mode & stat.S_IWUSR == 0  # read-only bits (os.access lies for root)
    # the frozen copy is untouched
    assert (frozen / "test_ops.py").read_text() == SUITE


def test_build_never_raises_on_garbage(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    assert build_repair_evidence(repo, ["not-a-node-id", "x.py::t"]) is None
