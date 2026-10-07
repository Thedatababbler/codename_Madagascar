#!/usr/bin/env python3
"""Label every attributed held-out case ``documented`` / ``undocumented`` / ``unknown``
(author-evolution spec §5 item 2). Training tasks only. Output: case ids, labels, reason
codes and public symbol names -- never assertion text or literals.

A case is ``documented`` when
  * at least one project symbol it reaches (attribution ``case_symbols``) appears in the
    documents as an identifier, and
  * at least one assertion feature of the case -- an attribute name it asserts on, an
    exception class in ``pytest.raises`` / ``assertRaises``, or a string / number literal in
    an assertion -- occurs in a document paragraph that names one of those symbols;
and it does not rest on a magic method or internal attribute the documents never mention
(``repr``, ``__slots__``, ``_private`` ...). With no project symbol at all it is ``unknown``.

    uv run python scripts/sealed/doc_label.py --task tinydb      # all training tasks: --all
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import DATASET_ROOT, assert_no_source_overlap, heldout_dir  # noqa: E402

EVO = Path(os.environ.get("ADAMAS_EVOLUTION_ROOT") or "outputs/evolution")
ATTRIBUTION_ROOT = EVO / "sealed" / "attribution"
OUT_ROOT = EVO / "sealed" / "doc_labels"
TRAIN = ["bplustree", "cookiecutter", "csvs-to-sqlite", "deprecated", "djangorestframework-simplejwt", "flask",
         "imapclient", "python-hl7", "rsa", "tinydb", "voluptuous", "zxcvbn"]
#: builtins whose use in an assertion tests a magic method
MAGIC_BUILTINS = {"repr": "__repr__", "hash": "__hash__", "dir": "__dir__", "vars": "__dict__"}


def doc_paragraphs(task: str) -> list[str]:
    paras: list[str] = []
    for p in sorted((DATASET_ROOT / task / "docs").glob("*")):
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        # a bullet with its indented continuation is a paragraph; so is a blank-line block
        for block in re.split(r"\n\s*\n|\n(?=\s*[-*] )", text):
            b = " ".join(block.split())
            if b:
                paras.append(b)
    return paras


def _word_in(word: str, text: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", text) is not None


def _features(fn: ast.AST) -> tuple[set[str], set[str], set[str], set[str]]:
    """(attribute names asserted on, exception names, literals in assertions, magic names used)."""
    attrs: set[str] = set()
    excs: set[str] = set()
    lits: set[str] = set()
    magic: set[str] = set()
    assert_nodes: list[ast.AST] = []
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Assert):
            assert_nodes.append(sub.test)
        elif isinstance(sub, ast.Call):
            f = sub.func
            fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if fname.startswith("assert") and fname not in ("assertRaises", "assertRaisesRegex", "assertWarns"):
                assert_nodes.extend(sub.args)
            if fname in ("raises", "assertRaises", "assertRaisesRegex") and sub.args:
                a = sub.args[0]
                for e in (a.elts if isinstance(a, ast.Tuple) else [a]):
                    if isinstance(e, ast.Name):
                        excs.add(e.id)
                    elif isinstance(e, ast.Attribute):
                        excs.add(e.attr)
    # literals that also appear as call arguments or assignments outside assertions are test-side
    # inputs echoed back, not details the documents would have to state (spot check: D_FEATURE_WORDS)
    inputs: set[str] = set()
    assert_ids = {id(n) for a in assert_nodes for n in ast.walk(a)}
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Constant) and id(sub) not in assert_ids:
            v = sub.value
            if isinstance(v, str):
                inputs.add(v.strip())
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                inputs.add(str(v))
    for node in assert_nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute):
                attrs.add(sub.attr)
                if sub.attr.startswith("_"):
                    magic.add(sub.attr)
            elif isinstance(sub, ast.Constant):
                v = sub.value
                if isinstance(v, str) and len(v.strip()) >= 3:
                    lits.add(v.strip())
                elif isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) >= 2:
                    lits.add(str(v))
            elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in MAGIC_BUILTINS:
                magic.add(MAGIC_BUILTINS[sub.func.id])
    return attrs, excs, {l for l in lits if l not in inputs}, magic


def label_task(task: str) -> dict:
    attr_file = json.loads((ATTRIBUTION_ROOT / f"{task}.json").read_text(encoding="utf-8"))
    attribution = attr_file.get("attribution") or {}
    symbols = attr_file.get("case_symbols") or {}
    case_ms = {c: m for m, cs in attribution.items() for c in cs}
    paras = doc_paragraphs(task)
    all_docs = " ".join(paras)
    tests_dir = heldout_dir(task)
    files = sorted({*tests_dir.rglob("test_*.py"), *tests_dir.rglob("*_test.py")})
    out: dict[str, dict] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        rel = str(path.relative_to(tests_dir))
        for fn in ast.walk(tree):
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test")):
                continue
            cid = f"{rel}::{fn.name}"
            if cid not in case_ms:
                continue
            syms = symbols.get(cid) or []
            if not syms:
                out[cid] = {"milestone": case_ms[cid], "label": "unknown", "depth": "unknown", "reason": "no_project_symbol", "symbols": [], "paragraphs": []}
                continue
            leaves = {s.split(".")[-1] for s in syms} | {s.split(".")[0] for s in syms}
            documented_syms = sorted(s for s in syms if _word_in(s.split(".")[-1], all_docs) and not s.split(".")[-1].startswith("_"))
            internal = sorted(s for s in syms if s.split(".")[-1].startswith("_") and not (s.split(".")[-1].startswith("__") and s.split(".")[-1].endswith("__")))
            attrs, excs, lits, magic = _features(fn)
            # a paragraph naming the symbol and its neighbours: a signature bullet and the sentence
            # describing the behaviour are often adjacent blocks
            idx = [i for i, p in enumerate(paras) if any(_word_in(l, p) for l in leaves if not l.startswith("_"))]
            window = sorted({k for i in idx for k in (i - 1, i, i + 1) if 0 <= k < len(paras)})
            rel_text = " ".join(paras[j] for j in window)
            # paragraphs (by index) that carry an assertion feature: the sentences the case rests on
            backing = [j for j in window if any(_word_in(a, paras[j]) for a in attrs if not a.startswith("_"))
                       or any(_word_in(e, paras[j]) for e in excs) or any(l in paras[j] for l in lits)]
            hit = {
                "attribute": any(_word_in(a, rel_text) for a in attrs if not a.startswith("_")),
                "exception": any(_word_in(e, rel_text) for e in excs),
                "literal": any(l in rel_text for l in lits),
            }
            undocumented_magic = sorted(m for m in magic if not _word_in(m, all_docs) and not _word_in(m.strip("_"), rel_text))
            undocumented_internal = [s for s in internal if not _word_in(s.split(".")[-1], all_docs)]
            # three levels (the 2026-10-07 spot check): the documents give the asserted detail, give the
            # behaviour only, or do not state it. Detail = exception classes and output-side literals.
            details = [("exception", e) for e in excs] + [("literal", l) for l in lits]
            found = [d for d in details if (_word_in(d[1], rel_text) if d[0] == "exception" else d[1] in rel_text)]
            attr_hit = any(_word_in(a, rel_text) for a in attrs if not a.startswith("_"))
            if undocumented_internal:
                depth, reason = "not_specified", "internal_attribute"
            elif undocumented_magic:
                depth, reason = "not_specified", "magic_method"
            elif not documented_syms or not rel_text:
                depth, reason = "not_specified", "symbol_not_in_documents"
            elif details and len(found) == len(details):
                depth, reason = "detail_specified", "all_details_in_documents"
            elif details:
                depth, reason = "behaviour_only", f"details_missing_{len(details) - len(found)}_of_{len(details)}"
            elif attr_hit:
                depth, reason = "detail_specified", "asserted_attribute_documented"
            else:
                depth, reason = "behaviour_only", "symbol_documented_assertion_not"
            label = "documented" if depth == "detail_specified" else "undocumented"
            sym_paras = [i for i in idx]
            out[cid] = {"milestone": case_ms[cid], "label": label, "depth": depth, "reason": reason,
                        "symbols": documented_syms or syms, "magic": undocumented_magic,
                        # paragraphs the case rests on: those carrying an assertion feature, else those naming its symbols
                        "paragraphs": (backing or sym_paras) if depth != "not_specified" else []}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    tasks = TRAIN if args.all else [args.task]
    for task in tasks:
        labels = label_task(task)
        text = json.dumps({"task": task, "labels": labels}, indent=1)
        assert_no_source_overlap(text, task)
        OUT_ROOT.mkdir(parents=True, exist_ok=True)
        (OUT_ROOT / f"{task}.json").write_text(text, encoding="utf-8")
        from collections import Counter
        c = Counter(v["depth"] for v in labels.values())
        print(f"{task:30s} {len(labels):4d} cases  detail {c['detail_specified']:4d}  behaviour-only {c['behaviour_only']:4d}  "
              f"not specified {c['not_specified']:4d}  unknown {c['unknown']:3d}")


if __name__ == "__main__":
    main()
