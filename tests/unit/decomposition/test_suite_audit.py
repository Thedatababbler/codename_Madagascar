"""The mechanical audit of an authored suite (author-evolution spec §2.4, user's
error-path rule of 2026-10-04). One violating and one compliant sample per rule."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from orchestra.codeprojecteval.suite_audit import audit_suite
from orchestra.control.fast_loop import routing

DOCS = {
    "architecture_design.md": (
        "`store.py` :\n\n- `Store`: a store.\n  - `put(key, value)`: Stores a value; raises `KeyError` when the key is empty.\n"
        "  - `get(key)`: Returns the stored value, or None when the key is absent.\n- `close()`: Closes the store; an error is raised if it is already closed.\n"
    ),
}
ALLOWED = {"Store", "Store.put", "Store.get", "close", "put", "get"}


def _suite(*files: tuple[str, str]) -> Path:
    d = Path(tempfile.mkdtemp()) / "spec_tests"
    d.mkdir()
    for name, body in files:
        (d / name).write_text(body, encoding="utf-8")
    return d


def _audit(body: str, allowed=ALLOWED):
    return audit_suite(_suite(("test_store.py", body)), DOCS, packages=["mypkg"], allowed_symbols=allowed)


GOOD = '''import pytest


@pytest.mark.timeout(20)
def test_get_returns_none_when_absent():
    # Architecture: "Returns the stored value, or None when the key is absent."
    from mypkg.store import Store
    assert Store().get("missing") is None


def test_put_rejects_empty_key():
    # DOC: "raises `KeyError` when the key is empty."
    from mypkg.store import Store
    with pytest.raises(KeyError):
        Store().put("", 1)
'''


def test_compliant_suite_has_no_violations_and_full_citation() -> None:
    r = _audit(GOOD)
    assert r.ok, r.violations
    assert r.cases == 2 and r.cited_ratio == 1.0 and r.timeout_marks == 1


def test_missing_and_paraphrased_citations() -> None:
    r = _audit('''import pytest

def test_no_citation():
    from mypkg.store import Store
    assert Store().get("x") is None

def test_paraphrased():
    # PRD: "the store gives back None for keys that were never stored"
    from mypkg.store import Store
    assert Store().get("x") is None
''')
    rules = [(v.case, v.rule) for v in r.violations]
    assert ("test_no_citation", "citation") in rules
    assert ("test_paraphrased", "citation") in rules
    assert r.cited_ratio == 0.0


def test_citation_matching_ignores_punctuation_and_whitespace() -> None:
    r = _audit('''def test_x():
    # Architecture:   "Returns the stored value,   or None when the key is absent"
    from mypkg.store import Store
    assert Store().get("x") is None
''')
    assert r.ok, r.violations


def test_private_representation_is_refused_unless_the_suite_defines_it_or_the_documents_name_it() -> None:
    r = _audit('''def test_private():
    # Architecture: "Returns the stored value, or None when the key is absent."
    from mypkg.store import Store
    s = Store()
    assert s._items == {}
    assert s.__dict__
    assert vars(s)
    assert getattr(s, "_hidden") is None

class FakeBackend:
    def _read(self):
        return 1

def test_own_private_is_fine():
    # Architecture: "Returns the stored value, or None when the key is absent."
    assert FakeBackend()._read() == 1
''')
    details = [v.detail for v in r.violations if v.rule == "private"]
    assert any("._items" in d for d in details)
    assert any("__dict__" in d for d in details)
    assert any("vars()" in d for d in details)
    assert any("'_hidden'" in d for d in details)
    assert not any("_read" in d for d in details)


def test_error_path_three_cases() -> None:
    r = _audit('''import pytest

def test_named_class_ok():
    # Architecture: "raises `KeyError` when the key is empty."
    from mypkg.store import Store
    with pytest.raises(KeyError):
        Store().put("", 1)

def test_named_class_mismatch():
    # Architecture: "raises `KeyError` when the key is empty."
    from mypkg.store import Store
    with pytest.raises(ValueError):
        Store().put("", 1)

def test_unnamed_error_generic_is_soft():
    # Architecture: "an error is raised if it is already closed."
    from mypkg.store import close
    with pytest.raises(Exception):
        close()

def test_unnamed_error_specific_class():
    # Architecture: "an error is raised if it is already closed."
    from mypkg.store import close
    with pytest.raises(RuntimeError):
        close()

def test_no_error_in_citation():
    # Architecture: "Returns the stored value, or None when the key is absent."
    from mypkg.store import Store
    with pytest.raises(KeyError):
        Store().get(None)
''')
    by_case = {v.case: v for v in r.violations if v.rule == "error_path"}
    assert "test_named_class_ok" not in by_case
    assert "ValueError" in by_case["test_named_class_mismatch"].detail
    assert "test_unnamed_error_generic_is_soft" not in by_case
    assert r.soft_cases == ["test_store.py::test_unnamed_error_generic_is_soft"]
    assert "pytest.raises(Exception)" in by_case["test_unnamed_error_specific_class"].detail
    assert "mentions no error" in by_case["test_no_error_in_citation"].detail


def test_shims_and_module_top_imports() -> None:
    r = _audit('''import sys
import pytest
import tomllib
import requests
from mypkg.store import Store

try:
    import mypkg.other
except ImportError:
    mypkg = None

def test_shims(monkeypatch):
    # Architecture: "Returns the stored value, or None when the key is absent."
    import importlib, mypkg.store
    sys.modules["mypkg.store"] = None
    sys.path.insert(0, "/x")
    importlib.reload(mypkg.store)
    pytest.importorskip("mypkg")
    monkeypatch.setattr(mypkg.store.Store, "get", lambda self, k: 1)
    assert Store().get("x") is None
''')
    shim = [v.detail for v in r.violations if v.rule == "shim"]
    imp = [v.detail for v in r.violations if v.rule == "import"]
    assert any("import fallback" in d for d in shim)
    assert any("sys.modules" in d for d in shim)
    assert any("sys.path.insert" in d for d in shim)
    assert any("importlib.reload" in d for d in shim)
    assert any("importorskip" in d for d in shim)
    assert any("monkeypatches the project" in d for d in shim)
    assert any("tomllib" in d for d in imp)
    assert any("third-party module (requests)" in d for d in imp)
    assert any("import of the project" in d for d in imp)
    assert not any("sys)" in d or "(sys" in d for d in imp)  # sys and pytest are fine at module top


def test_suite_module_named_like_the_package_is_a_shim() -> None:
    d = _suite(("mypkg.py", "x = 1\n"), ("test_a.py", GOOD))
    r = audit_suite(d, DOCS, packages=["mypkg"], allowed_symbols=ALLOWED)
    assert any(v.rule == "shim" and "named like the project package" in v.detail for v in r.violations)


def test_undocumented_symbols_are_reported_only_when_an_allowed_set_is_given() -> None:
    body = '''def test_x():
    # Architecture: "Returns the stored value, or None when the key is absent."
    from mypkg.store import Store, Secret
    import mypkg.store
    mypkg.store.helper()
    mypkg.store.Store.get(Store(), "k")
    assert Store().get("x") is None
'''
    r = _audit(body)
    sym = [v.detail for v in r.violations if v.rule == "symbol"]
    assert any("Secret" in d for d in sym)
    assert any("helper" in d for d in sym)
    assert not any("Store.get" in d for d in sym)
    assert not [v for v in _audit(body, allowed=None).violations if v.rule == "symbol"]


def test_author_text_lists_every_violation_grouped_by_test() -> None:
    r = _audit('''def test_bad():
    from mypkg.store import Store
    assert Store()._x
''')
    text = r.to_author_text()
    assert "test_store.py::test_bad" in text and "[citation]" in text and "[private]" in text
    assert _audit(GOOD).to_author_text() == ""


def test_routing_citation_regex_accepts_doc_form_only_under_the_author_flag(monkeypatch) -> None:
    src = '# DOC: "Returns the stored value, or None when the key is absent."\n'
    monkeypatch.delenv("ADAMAS_AUTHOR_RULES_DOC", raising=False)
    assert routing.citations_in(src) == []
    monkeypatch.setenv("ADAMAS_AUTHOR_RULES_DOC", "configs/author/rules_doc/current")
    assert routing.citations_in(src) == ["Returns the stored value, or None when the key is absent."]
    legacy = '# PRD: "Returns the stored value, or None when the key is absent."\n'
    monkeypatch.delenv("ADAMAS_AUTHOR_RULES_DOC", raising=False)
    assert routing.citations_in(legacy) == ["Returns the stored value, or None when the key is absent."]
