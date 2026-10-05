#!/usr/bin/env python3
"""Attribute held-out cases to milestones (self-evolution spec §5.4), by symbols only.

For one task: every ``unit_tests`` case is mapped to the milestone whose focus
paths contain the module the case imports or the public symbol it calls.
The output is a JSON file of case-id sets per milestone and nothing else --
no pass / fail, no source. It is the only place outside
``scripts/eval_codeprojecteval.py`` that opens the held-out directory, and
it never runs it.

    uv run python scripts/sealed/heldout_attribution.py --task tinydb --plan configs/datasets/cpe_feature_plans/tinydb.plan.json \\
        --out outputs/evolution/sealed/attribution/tinydb.json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import DATASET_ROOT, assert_no_source_overlap, heldout_dir  # noqa: E402


def _stems(paths: list[str]) -> set[str]:
    out = set()
    for p in paths:
        name = str(p).rstrip("/").rsplit("/", 1)[-1]
        stem = name[:-3] if name.endswith(".py") else name
        if stem and stem != "__init__":
            out.add(stem)
        parts = [x for x in str(p).replace(".py", "").split("/") if x and x not in ("src",)]
        if parts:
            out.add(".".join(parts))
    return out


def _case_symbols(tree: ast.AST) -> dict[str, set[str]]:
    """test id -> module stems and attribute heads it refers to."""
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
            for a in node.names:
                imports.add(f"{node.module}.{a.name}")
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            names = set(imports)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
            out[node.name] = names
    return out


def _test_files(tests_dir: Path) -> list[Path]:
    """Every module pytest collects by default: ``test_*.py`` and ``*_test.py``.

    Until 2026-10-05 only ``test_*.py`` was read, which left zxcvbn (all ``*_test.py``)
    with no attributed case at all and dropped imapclient's ``imapclient_test.py``."""
    return sorted({*tests_dir.rglob("test_*.py"), *tests_dir.rglob("*_test.py")})


#: submodule names of the project package (``from pkg import submodule``), set per task
SUBMODULES: set[str] = set()


def _imports_of(tree: ast.AST, packages: set[str]) -> dict[str, str]:
    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in packages:
            for a in node.names:
                if a.name == "*":
                    continue
                # ``from pkg import submodule``: attributes on it are the package's own names
                imported[a.asname or a.name] = next(iter(packages)) if a.name in SUBMODULES else a.name
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in packages:
                    imported[(a.asname or a.name).split(".")[0]] = a.name.split(".")[0]
    return imported


def _call_class(node: ast.AST, imported: dict[str, str]) -> str:
    """``Cls(...)`` / ``pkg.Cls(...)`` -> the imported class name, else ''."""
    if isinstance(node, ast.Call):
        f = node.func
        name = ""
        if isinstance(f, ast.Name) and f.id in imported:
            name = imported[f.id]
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in imported:
            name = f.attr
        # only a class call makes an instance; ``rv = send_file(...)`` is a return value, not a send_file
        if name[:1].isupper():
            return name
    return ""


def _function_symbols(fn: ast.AST, imported: dict[str, str], packages: set[str],
                      fixtures: dict[str, tuple[set[str], str]],
                      self_attrs: dict[str, str] | None = None) -> tuple[set[str], str]:
    """(names reached, class of what the function returns/yields) for one function."""
    instances: dict[str, str] = {}
    args = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    names: set[str] = set()
    for a in args:  # fixture parameters
        if a in fixtures:
            fsyms, fcls = fixtures[a]
            names |= fsyms
            if fcls:
                instances[a] = fcls
    returned = ""
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Assign):
            cls = _call_class(sub.value, imported)
            if cls:
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        instances[t.id] = cls
        if isinstance(sub, (ast.Return, ast.Yield)) and sub.value is not None:
            returned = _call_class(sub.value, imported) or (instances.get(sub.value.id, "") if isinstance(sub.value, ast.Name) else "") or returned
        if isinstance(sub, ast.withitem) and sub.optional_vars is not None and isinstance(sub.optional_vars, ast.Name):
            cls = _call_class(sub.context_expr, imported)
            if cls:
                instances[sub.optional_vars.id] = cls
    for sub in ast.walk(fn):
        # unittest style: self.client = IMAPClient(...) in setUp, self.client.m(...) in the test
        if self_attrs and isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Attribute) \
                and isinstance(sub.value.value, ast.Name) and sub.value.value.id == "self" and sub.value.attr in self_attrs:
            names.add(f"{self_attrs[sub.value.attr]}.{sub.attr}")
        if self_attrs and isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self" \
                and sub.attr in self_attrs:
            names.add(self_attrs[sub.attr])
        if isinstance(sub, ast.Name) and sub.id in imported:
            if imported[sub.id] not in packages:  # the bare package / submodule reference is not a symbol
                names.add(imported[sub.id])
        elif isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
            base = sub.value.id
            if base in imported:
                root = imported[base]
                names.add(sub.attr if root in packages else f"{root}.{sub.attr}")
            elif base in instances:
                names.add(f"{instances[base]}.{sub.attr}")
    return names, returned


def _fixtures_of(tree: ast.AST, imported: dict[str, str], packages: set[str],
                 inherited: dict[str, tuple[set[str], str]] | None = None) -> dict[str, tuple[set[str], str]]:
    out = dict(inherited or {})
    fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("test")]
    for _ in range(2):  # fixtures that use fixtures
        for fn in fns:
            out[fn.name] = _function_symbols(fn, imported, packages, out)
    return out


def class_self_attrs(tree: ast.AST, packages: set[str]) -> dict[str, dict[str, str]]:
    """class name -> {self attribute: project class assigned to it} for one module."""
    imported = _imports_of(tree, packages)
    out: dict[str, dict[str, str]] = {}
    for c in ast.walk(tree):
        if isinstance(c, ast.ClassDef):
            attrs: dict[str, str] = {}
            for sub in ast.walk(c):
                if isinstance(sub, ast.Assign):
                    cls = _call_class(sub.value, imported)
                    for t in sub.targets:
                        if cls and isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                            attrs[t.attr] = cls
            out[c.name] = attrs
    return out


def _project_symbols(tree: ast.AST, packages: set[str],
                     conftest: dict[str, tuple[set[str], str]] | None = None,
                     global_self_attrs: dict[str, dict[str, str]] | None = None) -> dict[str, list[str]]:
    """test id -> the project's names the test reaches: imported names, ``Name.attr`` on them,
    ``x.attr`` where ``x = Cls(...)``, and the same through fixture parameters. Names only."""
    imported = _imports_of(tree, packages)
    fixtures = _fixtures_of(tree, imported, packages, conftest)
    out: dict[str, list[str]] = {}
    classes = [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef)]
    self_attrs_of: dict[str, dict[str, str]] = {}
    by_name = {c.name: c for c in classes}
    for c in classes:
        attrs: dict[str, str] = {}
        for sub in ast.walk(c):
            if isinstance(sub, ast.Assign):
                cls = _call_class(sub.value, imported)
                for t in sub.targets:
                    if cls and isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                        attrs[t.attr] = cls
        self_attrs_of[c.name] = attrs
    known = {**(global_self_attrs or {}), **self_attrs_of}
    for _ in range(3):  # base classes, in this module or another test module (resolved by name)
        for c in classes:
            for b in c.bases:
                bname = b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else "")
                if bname in known and bname != c.name:
                    self_attrs_of[c.name] = {**known[bname], **self_attrs_of[c.name]}
                    known[c.name] = self_attrs_of[c.name]
    owner: dict[int, str] = {}
    for c in classes:
        for item in c.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner[id(item)] = c.name
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
            attrs = self_attrs_of.get(owner.get(id(fn), ""), {})
            out[fn.name] = sorted(_function_symbols(fn, imported, packages, fixtures, attrs)[0])
    del by_name
    return out


def case_symbols(task: str, packages: list[str]) -> dict[str, list[str]]:
    """``file::test`` -> the project's public-or-private names it reaches (names only)."""
    tests_dir = heldout_dir(task)
    pk = set(packages)
    # submodule names, from the documents' directory tree (documents only)
    from orchestra.codeprojecteval.public_symbols import load_docs, parse_directory_tree
    tree_paths = [t for text in load_docs(DATASET_ROOT / task / "docs").values() for t in parse_directory_tree(text)]
    SUBMODULES.clear()
    SUBMODULES.update(t.rstrip("/").rsplit("/", 1)[-1].removesuffix(".py") for t in tree_paths
                      if (t.endswith(".py") or t.endswith("/")) and not t.rsplit("/", 1)[-1].startswith("__"))
    SUBMODULES.difference_update(pk)
    conftests: dict[Path, dict[str, tuple[set[str], str]]] = {}
    for c in sorted(tests_dir.rglob("conftest.py"), key=lambda q: len(q.parts)):
        try:
            tree = ast.parse(c.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        parent = conftests.get(c.parent.parent, {})
        conftests[c.parent] = _fixtures_of(tree, _imports_of(tree, pk), pk, parent)
    out: dict[str, list[str]] = {}
    global_attrs: dict[str, dict[str, str]] = {}
    for path in sorted(tests_dir.rglob("*.py")):
        try:
            global_attrs.update(class_self_attrs(ast.parse(path.read_text(encoding="utf-8", errors="replace")), pk))
        except (OSError, SyntaxError):
            continue
    for path in _test_files(tests_dir):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        inherited: dict[str, tuple[set[str], str]] = {}
        for d in reversed(path.parents):
            inherited.update(conftests.get(d, {}))
        rel = str(path.relative_to(tests_dir))
        for name, syms in _project_symbols(tree, pk, inherited, global_attrs).items():
            out[f"{rel}::{name}"] = syms
    return out


def attribute(task: str, plan_path: Path) -> dict[str, list[str]]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    focus = {m["milestone_id"]: _stems(m.get("focus_paths") or []) for m in plan["milestones"]}
    tests_dir = heldout_dir(task)
    result: dict[str, list[str]] = defaultdict(list)
    for path in _test_files(tests_dir):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        rel = str(path.relative_to(tests_dir))
        for test_name, names in _case_symbols(tree).items():
            best, best_hits = "", 0
            for mid, stems in focus.items():
                hits = sum(1 for n in names if n in stems or any(n.startswith(s + ".") or n.endswith("." + s) for s in stems))
                if hits > best_hits:
                    best, best_hits = mid, hits
            if best:
                result[best].append(f"{rel}::{test_name}")
    return {mid: sorted(cases) for mid, cases in result.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    data = attribute(args.task, args.plan)
    cfg = json.loads((DATASET_ROOT / args.task / "config.json").read_text(encoding="utf-8"))
    packages = [str(cfg.get("source_code") or args.task).split("/")[0]]
    syms = case_symbols(args.task, packages)
    text = json.dumps({"task_id": args.task, "attribution": data, "case_symbols": syms}, indent=2)
    assert_no_source_overlap(text, args.task)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print("wrote", args.out, {k: len(v) for k, v in data.items()})


if __name__ == "__main__":
    main()
