"""Stage B4 of the author-evolution loop: evaluation suites scored on stored code (memory
replay) and the report. The §6 metric arithmetic is tested in test_author_metrics.py."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from orchestra.codeprojecteval.case_symbols import suite_case_symbols

sys.path.insert(0, str(Path("scripts").resolve()))
author_eval = importlib.import_module("author_eval")

SUITE = '''import pytest


@pytest.mark.parametrize("k", ["a", "b"])
def test_param(k):
    # DOC: "x"
    from pkg.store import Store
    assert Store().get(k) is None


class TestStore:
    def test_put(self):
        from pkg.store import Store
        s = Store()
        s.put("a", 1)
        assert s.get("a") == 1


@pytest.fixture
def store():
    from pkg.store import Store
    return Store()


def test_with_fixture(store):
    assert store.size() == 0
'''

STORE = '''class Store:
    def __init__(self):
        self.d = {}
    def get(self, k):
        return None if k == "a" else self.d.get(k)
    def put(self, k, v):
        self.d[k] = v
    def size(self):
        return len(self.d)
'''


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "store.py").write_text(STORE)
    suite = tmp_path / "m.spec_tests"
    suite.mkdir()
    (suite / "test_store.py").write_text(SUITE)
    return repo, suite


def test_run_suite_folds_parametrisations_and_keeps_class_spelling(tmp_path) -> None:
    repo, suite = _repo(tmp_path)
    res = author_eval.run_suite(repo, suite, Path(sys.executable), timeout_flags=False)
    assert res == {
        "spec_tests/test_store.py::test_param": "pass",        # both parameters pass
        "spec_tests/test_store.py::TestStore::test_put": "fail",  # get("a") is None by construction
        "spec_tests/test_store.py::test_with_fixture": "pass",
    }


def test_suite_case_symbols_use_the_same_keys_and_follow_fixtures(tmp_path) -> None:
    _repo_, suite = _repo(tmp_path)
    syms = suite_case_symbols(suite, ["pkg"], {"store"}, "spec_tests/")
    assert set(syms) == {"spec_tests/test_store.py::test_param", "spec_tests/test_store.py::TestStore::test_put",
                         "spec_tests/test_store.py::test_with_fixture"}
    assert "Store.get" in syms["spec_tests/test_store.py::test_param"]
    assert {"Store.put", "Store.get"} <= set(syms["spec_tests/test_store.py::TestStore::test_put"])
    assert "Store.size" in syms["spec_tests/test_store.py::test_with_fixture"]


def test_report_prints_every_split_and_sample_spread(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(author_eval, "EVAL_ROOT", tmp_path)
    for lab, vals in (("A", (0.8, 0.6)), ("C", (0.5, 0.5))):
        (tmp_path / lab).mkdir()
        for i, v in enumerate(vals):
            summ = {"evolution": {"true_miss": v, "true_miss_n": 10}, "acceptance": {"true_miss": v, "true_miss_n": 6},
                    "acceptance_without": {"true_miss": v, "true_miss_n": 4}}
            (tmp_path / lab / f"metrics.s{i}.json").write_text(json.dumps({"rows": [], "summary": summ}))
    out = tmp_path / "r.md"
    author_eval.report(type("A", (), {"labels": ["A", "C"], "out": str(out)})())
    text = out.read_text()
    assert "## evolution" in text and "## acceptance without" in text
    assert "| true miss rate (main) | 0.8 (n=10), sd 0.1 | 0.5 (n=10), sd 0.0 |" in text
