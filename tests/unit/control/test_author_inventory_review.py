"""Stage B3 of the author-evolution loop: documented-behaviour inventory, coverage check,
fix rounds, hard / soft split (author-evolution spec §2; user's option A for soft cases)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from orchestra.codeprojecteval.behaviour_inventory import (
    Inventory,
    InventoryItem,
    check_items,
    decide_tier,
    extract_inventory,
    load_tier_rules,
    render_for_author,
)
from orchestra.control.author import assemble as A
from orchestra.control.author.node_review import author_review_dir, run_author_review
from orchestra.control.author.review import review_and_fix, split_soft

TREE = "├── pkg\n│   ├── __init__.py\n│   └── store.py\n"
DOCS = {
    "architecture_design.md": "```\n" + TREE + "```\n\n`store.py` :\n\n- `Store`: a key-value store.\n"
    "  - `get(key)`: Returns None when the key is absent.\n"
    "  - `put(key, value)`: Raises `KeyError` when the key is empty.\n"
    "- `Store` supports several backends.\n",
    "directory_tree.txt": TREE,
}
RULES = load_tier_rules()


def test_tier_rule_needs_condition_and_result() -> None:
    assert decide_tier("Returns None when the key is absent.", RULES)[0] == "hard"
    assert decide_tier("Raises `KeyError` when the key is empty.", RULES)[0] == "hard"
    assert decide_tier("Store supports several backends.", RULES)[0] == "soft"


def test_quote_not_in_documents_and_unowned_symbols_are_dropped() -> None:
    raw = [
        {"symbol": "Store.get", "quote": "Returns None when the key is absent.", "kind": "boundary", "tier": "soft"},
        {"symbol": "Store.get", "quote": "Returns a default object when nothing is stored.", "kind": "boundary"},
        {"symbol": "Other.thing", "quote": "Raises `KeyError` when the key is empty.", "kind": "error_path"},
    ]
    kept, dropped = check_items(raw, docs=DOCS, owned={"Store", "Store.get", "Store.put"}, rules=RULES)
    assert [i.quote for i in kept] == ["Returns None when the key is absent."]
    assert kept[0].tier == "hard" and kept[0].extractor_tier == "soft"  # the program decides the tier
    assert sorted(d["reason"] for d in dropped) == ["quote_not_in_documents", "symbol_not_owned"]


def test_extraction_writes_prompt_and_reply_and_is_cached(tmp_path) -> None:
    calls = []

    def fake(system, prompt):
        calls.append(prompt)
        assert "`Store.get`" in prompt  # the owned symbols are in the prompt
        return json.dumps([
            {"symbol": "Store.get", "quote": "Returns None when the key is absent.", "kind": "boundary", "tier": "hard"},
            {"symbol": "Store", "quote": "supports several backends", "kind": "main_path", "tier": "soft"},
        ]), "fake-model"

    kw = dict(task="t", milestone_id="m", objective="build the store", criteria=["get works"], focus_paths=["pkg/store.py"],
              docs=DOCS, out_dir=tmp_path, call=fake)
    inv = extract_inventory(**kw)
    assert [i.tier for i in inv.items] == ["hard", "soft"]
    assert Path(inv.prompt_path).is_file() and Path(inv.reply_path).is_file() and inv.model == "fake-model"
    again = extract_inventory(**kw)
    assert len(calls) == 1 and [i.quote for i in again.items] == [i.quote for i in inv.items]
    text = render_for_author(inv)
    assert "Required:" in text and "Optional:" in text and "Returns None when the key is absent." in text


INV = Inventory(task="t", milestone_id="m", items=[
    InventoryItem("I001", "Store.get", "Returns None when the key is absent.", "boundary", "hard"),
    InventoryItem("I002", "Store.put", "Raises `KeyError` when the key is empty.", "error_path", "hard"),
    InventoryItem("I003", "Store", "Store supports several backends", "main_path", "soft"),
])

GET_TEST = '''import pytest


@pytest.mark.timeout(20)
def test_get_absent_is_none():
    # Architecture: "Returns None when the key is absent."
    from pkg.store import Store
    assert Store().get("x") is None
'''

PUT_TEST = '''

def test_put_empty_key_raises():
    # Architecture: "Raises `KeyError` when the key is empty."
    from pkg.store import Store
    with pytest.raises(KeyError):
        Store().put("", 1)
'''


def _workspace(tmp_path: Path, body: str) -> Path:
    ws = tmp_path / "ws"
    (ws / "spec_tests").mkdir(parents=True)
    (ws / "spec_tests" / "test_store.py").write_text(body, encoding="utf-8")
    return ws


def _review(ws, run_fix, **kw):
    return asyncio.run(review_and_fix(
        workspace=ws, milestone_id="m", docs=DOCS, packages=["pkg"], allowed_symbols=None, inventory=INV,
        hard_min=1.0, max_rounds=2, run_fix=run_fix, soft_dest=ws.parent / "frozen" / "m.spec_tests_soft",
        record_path=ws.parent / "review.json", **kw))


def test_uncovered_hard_item_triggers_a_fix_round_with_only_the_missing_sentence(tmp_path) -> None:
    ws = _workspace(tmp_path, GET_TEST)
    seen = []

    async def fix(text):
        seen.append(text)
        p = ws / "spec_tests" / "test_store.py"
        p.write_text(p.read_text() + PUT_TEST, encoding="utf-8")
        return True

    rec = _review(ws, fix)
    assert rec.met and rec.fix_calls == 1
    assert "Raises `KeyError` when the key is empty." in seen[0] and "Returns None when the key is absent." not in seen[0]
    assert rec.rounds[0]["hard_covered"] == 1 and rec.rounds[1]["hard_covered"] == 2


def test_rounds_are_capped_and_the_suite_is_frozen_as_is_with_the_gap_recorded(tmp_path) -> None:
    ws = _workspace(tmp_path, GET_TEST)
    calls = []

    async def fix(text):
        calls.append(text)
        return True

    rec = _review(ws, fix)
    assert not rec.met and rec.fix_calls == 2 and len(rec.rounds) == 3
    assert [u["item_id"] for u in rec.uncovered_final] == ["I002"]
    saved = json.loads((tmp_path / "review.json").read_text())
    assert saved["uncovered_final"][0]["item_id"] == "I002"
    assert (ws / "spec_tests" / "test_store.py").is_file()  # nothing deleted; custody takes it as it is


SOFT_SUITE = GET_TEST + PUT_TEST + '''

@pytest.fixture
def store():
    from pkg.store import Store
    return Store()


def helper(s):
    return s


def test_backends_soft(store):
    # Architecture: "Store supports several backends"
    assert helper(store) is not None


def test_mixed_hard_and_soft():
    # Architecture: "Store supports several backends"
    # Architecture: "Returns None when the key is absent."
    from pkg.store import Store
    assert Store().get("y") is None


class TestGroup:
    def test_soft_in_class(self):
        # Architecture: "Store supports several backends"
        assert True

    def test_hard_in_class(self):
        # Architecture: "Returns None when the key is absent."
        assert True
'''


def _collect(d: Path) -> int:
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", str(d)],
                         capture_output=True, text=True, cwd=d.parent)
    return sum(1 for ln in out.stdout.splitlines() if "::" in ln)


def test_soft_cases_leave_the_workspace_both_sides_collect_and_counts_add_up(tmp_path) -> None:
    ws = _workspace(tmp_path, SOFT_SUITE)
    (ws / "spec_tests" / "conftest.py").write_text("import pytest\n", encoding="utf-8")
    pkg = ws / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "store.py").write_text("class Store:\n    def get(self, k):\n        return None\n    def put(self, k, v):\n"
                                  "        if not k:\n            raise KeyError(k)\n")
    before = _collect(ws / "spec_tests")

    async def fix(text):
        return True

    rec = _review(ws, fix)
    assert rec.met
    assert sorted(rec.soft_cases) == ["test_store.py::TestGroup::test_soft_in_class", "test_store.py::test_backends_soft"]
    assert not (ws / "spec_tests_soft").exists()          # moved out of the workspace
    soft = Path(rec.soft_dest)
    assert soft.is_dir() and (soft / "conftest.py").is_file()
    hard_n, soft_n = _collect(ws / "spec_tests"), _collect(soft)
    assert before == 6 and soft_n == 2 and hard_n == 4 and hard_n + soft_n == before
    hard_src = (ws / "spec_tests" / "test_store.py").read_text()
    assert "test_mixed_hard_and_soft" in hard_src          # cites a hard item too: stays hard
    assert "def store(" in (soft / "test_store.py").read_text()  # fixtures copied along


def test_split_is_a_no_op_without_soft_cases(tmp_path) -> None:
    ws = _workspace(tmp_path, GET_TEST)
    assert split_soft(ws / "spec_tests", tmp_path / "soft", [])["soft"] == 0
    assert not (tmp_path / "soft").exists()


def test_feature_off_changes_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(A.INVENTORY_DIR_ENV, raising=False)
    assert author_review_dir("contract_m_test_author") is None
    assert A.milestone_inventory(SimpleNamespace(milestone_id="m"), env={}) is None
    monkeypatch.setenv(A.INVENTORY_DIR_ENV, str(tmp_path))
    assert author_review_dir("contract_m_test_author") is None  # no contract record: not an author node of this run


class _Req(SimpleNamespace):
    def model_copy(self, update):
        return _Req(**{**self.__dict__, **update})


def test_node_review_calls_the_backend_again_and_sums_usage(monkeypatch, tmp_path) -> None:
    from orchestra.backends.base import AgentRunStatus
    from orchestra.llm.usage import LLMUsage

    ws = _workspace(tmp_path, GET_TEST)
    inv_dir = tmp_path / "inv"
    (inv_dir / "contracts").mkdir(parents=True)
    (inv_dir / "contracts" / "c1.json").write_text(json.dumps({"contract_id": "c1", "milestone_id": "m", "focus_paths": ["pkg/store.py"]}))
    INV.save(inv_dir / "m.inventory.json")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    for k, v in DOCS.items():
        (docs_dir / k).write_text(v)
    monkeypatch.setenv(A.INVENTORY_DIR_ENV, str(inv_dir))
    monkeypatch.setenv(A.DOCS_DIR_ENV, str(docs_dir))
    monkeypatch.setenv(A.PACKAGES_ENV, "pkg")

    class Backend:
        calls = 0

        async def run(self, request, ctx):
            Backend.calls += 1
            assert request.messages[-1]["content"].startswith("Your suite under `spec_tests/` is not finished")
            p = ws / "spec_tests" / "test_store.py"
            p.write_text(p.read_text() + PUT_TEST)
            return _Res(status=AgentRunStatus.SUCCESS, output_artifacts=["diff2"], usage=LLMUsage(total_tokens=5))

    first = _Res(status=AgentRunStatus.SUCCESS, output_artifacts=["diff1"], usage=LLMUsage(total_tokens=10))
    assert author_review_dir("c1") == inv_dir
    req = _Req(request_id="abcdef0123", contract_id="c1", messages=[{"role": "user", "content": "go"}], rendered_context="go")
    result, summary = asyncio.run(run_author_review(review_dir=inv_dir, request=req, backend=Backend(),
                                                    backend_context=SimpleNamespace(workspace_ref=str(ws)), first=first,
                                                    semaphore=asyncio.Semaphore(1)))
    assert Backend.calls == 1 and summary["met"] and summary["fix_calls"] == 1
    assert result.usage.total_tokens == 15
    assert Path(summary["record"]).is_file()


class _Res(SimpleNamespace):
    def model_copy(self, update):
        return _Res(**{**self.__dict__, **update})


def test_shared_cache_reuses_the_extraction_across_runs(tmp_path, monkeypatch) -> None:
    calls = []

    def fake(system, prompt):
        calls.append(1)
        return json.dumps([{"symbol": "Store.get", "quote": "Returns None when the key is absent.", "kind": "boundary"}]), "m"

    monkeypatch.setenv("ADAMAS_AUTHOR_INVENTORY_CACHE", str(tmp_path / "cache"))
    kw = dict(task="t", milestone_id="m", objective="o", criteria=[], focus_paths=["pkg/store.py"], docs=DOCS, call=fake)
    a = extract_inventory(out_dir=tmp_path / "run1", **kw)
    b = extract_inventory(out_dir=tmp_path / "run2", **kw)
    assert len(calls) == 1 and [i.quote for i in a.items] == [i.quote for i in b.items]
    assert Path(b.reply_path).is_file()
