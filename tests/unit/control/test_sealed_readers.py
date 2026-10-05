"""Stage B2 of the author-evolution loop: only scripts/sealed/ reads the held-out suite or
the reference, and nothing a sealed script writes overlaps held-out source."""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

SEALED = Path("scripts/sealed").resolve()
sys.path.insert(0, str(SEALED))
_guard = importlib.import_module("_guard")
DATASET_OK = (_guard.DATASET_ROOT / "tinydb" / "config.json").is_file()
needs_dataset = pytest.mark.skipif(not DATASET_OK, reason="CodeProjectEval dataset not on this machine")


def test_a_module_outside_sealed_cannot_get_the_heldout_or_reference_path() -> None:
    with pytest.raises(_guard.SealedAccessError):
        _guard.heldout_dir("tinydb")
    with pytest.raises(_guard.SealedAccessError):
        _guard.reference_root("tinydb")
    with pytest.raises(_guard.SealedAccessError):
        _guard.heldout_source_windows("tinydb")


@needs_dataset
def test_a_sealed_module_can() -> None:
    selfcheck = importlib.import_module("_selfcheck")
    assert selfcheck.probe("tinydb").endswith("unit_tests")


@needs_dataset
def test_overlap_guard_refuses_source_and_allows_ids_and_symbol_names() -> None:
    selfcheck = importlib.import_module("_selfcheck")
    src = next(Path(selfcheck.probe("tinydb")).rglob("test_*.py")).read_text(encoding="utf-8")
    chunk = " ".join(src.split())[200:260]

    def check(text):  # call through a sealed frame, as the scripts do
        exec_globals = {"_guard": _guard, "text": text}
        code = compile("_guard.assert_no_source_overlap(text, 'tinydb')", str(SEALED / "_selfcheck.py"), "exec")
        exec(code, exec_globals)

    with pytest.raises(_guard.SealedAccessError):
        check(json.dumps({"leak": chunk}))
    check(json.dumps({"cases": {"unit_tests/test_tables.py::test_insert_multiple[#1a2b3c4d]": "pass"},
                      "symbols": ["CachingMiddleware.WRITE_CACHE_SIZE", "Table.insert_multiple"]}))


def test_parametrize_ids_lose_their_literals() -> None:
    sid = _guard.sealed_case_id("unit_tests/test_x.py::test_clone[git+https://example.org/repo.git]")
    assert re.fullmatch(r"unit_tests/test_x\.py::test_clone\[#[0-9a-f]{8}\]", sid)
    assert _guard.sealed_case_id("unit_tests/test_x.py::test_plain") == "unit_tests/test_x.py::test_plain"


@needs_dataset
@pytest.mark.parametrize("kind", ["attribution", "doc_labels"])
def test_sealed_outputs_on_disk_do_not_overlap_heldout_source(kind: str) -> None:
    root = Path("outputs/evolution/sealed") / kind
    files = sorted(root.glob("*.json"))
    if not files:
        pytest.skip(f"no {kind} outputs yet")
    code = compile("_guard.assert_no_source_overlap(text, task)", str(SEALED / "_selfcheck.py"), "exec")
    for f in files[:4]:
        task = json.loads(f.read_text(encoding="utf-8")).get("task_id") or json.loads(f.read_text(encoding="utf-8")).get("task")
        exec(code, {"_guard": _guard, "text": f.read_text(encoding="utf-8"), "task": task})


#: allowed outside scripts/sealed/: runtime path users (exclude the held-out directory from
#: workspaces, never read it), official scoring, the dataset converter that *builds* tasks, and
#: one environment probe that only reports collection counts
ALLOWLIST = {
    "src/orchestra/codeprojecteval/dataset.py", "src/orchestra/codeprojecteval/harness.py",
    "src/orchestra/control/fast_loop/node_resample.py", "src/orchestra/cli/run_codeprojecteval_decomp.py",
    "scripts/eval_codeprojecteval.py", "src/orchestra/codeprojecteval/ceiling.py",
    "scripts/nl2repo_to_cpe.py", "scripts/probe_codeprojecteval_env.py",
}
READ_PATTERNS = re.compile(r"\.unit_tests\b|/\s*[\"']unit_tests[\"']|get\(\s*[\"']unit_tests[\"']|[\"']unit_tests/|heldout_dir\(|reference_root\(")


def test_no_heldout_reader_outside_sealed_and_the_allowlist() -> None:
    offenders = []
    for base in (Path("src"), Path("scripts")):
        for p in base.rglob("*.py"):
            rel = p.as_posix()
            if rel.startswith("scripts/sealed/") or rel in ALLOWLIST:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                code = line.split("#", 1)[0]
                if "ignore_patterns" in code:
                    continue  # excluding the directory from a copy is not reading it
                if READ_PATTERNS.search(code):
                    offenders.append(f"{rel}:{i}")
    assert offenders == [], offenders


def test_inventory_gap_marks_documented_cases_whose_sentences_the_inventory_lacks() -> None:
    gap = importlib.import_module("inventory_gap")
    paras = ["`insert(document)`: Inserts a document and returns its id.", "`remove(cond)`: Removes matching documents."]
    labels = {
        "t.py::a": {"milestone": "m", "label": "documented", "symbols": ["Table.insert"], "paragraphs": [0]},
        "t.py::b": {"milestone": "m", "label": "documented", "symbols": ["Table.remove"], "paragraphs": [1]},
        "t.py::c": {"milestone": "m", "label": "undocumented", "symbols": ["Table.__repr__"], "paragraphs": []},
        "t.py::d": {"milestone": "other", "label": "documented", "symbols": ["Table.insert"], "paragraphs": [0]},
    }
    items = [{"symbol": "Table.insert", "quote": "Inserts a document and returns its id"}]
    assert gap.inventory_misses(labels, paras, items, "m") == {"t.py::a": False, "t.py::b": True}
