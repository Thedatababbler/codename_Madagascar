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


from orchestra.codeprojecteval import case_symbols as _cs  # noqa: E402
from orchestra.codeprojecteval.case_symbols import (  # noqa: E402
    SUBMODULES,
    _fixtures_of,
    _imports_of,
    _project_symbols,
    class_self_attrs,
)


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


# --- relevance (joint experiment, 2026-10-08) ---------------------------------------------------
# A case is relevant to milestone k when (1) it uses at least one module in k's focus paths and
# (2) every project module it uses belongs to milestone k or an earlier one: at a first run the
# later milestones' modules do not exist, so their cases fail under any design and only add noise.


def _module_of_import(node_module: str, name: str, package: str, submodules: set[str], resolve) -> str:
    """Path-like module for an imported name: ``pkg/mod.py``, ``pkg/sub`` (package) or ``pkg/__init__.py``."""
    if node_module and node_module != package:
        return node_module.replace(".", "/") + ".py"
    if name in submodules:
        return f"{package}/{name}.py"
    return resolve(name) or f"{package}/__init__.py"


def _imports(tree: ast.AST, packages: set[str], submodules: set[str], resolve) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in packages:
            pkg = (node.module or "").split(".")[0]
            for a in node.names:
                if a.name != "*":
                    out[a.asname or a.name] = _module_of_import(node.module or "", a.name, pkg, submodules, resolve)
        elif isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in packages:
                    out[(a.asname or a.name).split(".")[0]] = (a.name.replace(".", "/") + ".py") if "." in a.name else f"{root}/__init__.py"
    return out


def _used_names(fn: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}


def case_modules(task: str, packages: list[str]) -> dict[str, set[str]]:
    """``file::test`` -> the project modules the case (and its fixtures, incl. conftest) uses."""
    from orchestra.codeprojecteval.public_symbols import derive_public_symbols, load_docs, parse_directory_tree

    docs = load_docs(DATASET_ROOT / task / "docs")
    inv = derive_public_symbols(docs)
    tree_paths = [t for text in docs.values() for t in parse_directory_tree(text)]
    pk = set(packages)
    subs = {t.rstrip("/").rsplit("/", 1)[-1].removesuffix(".py") for t in tree_paths
            if (t.endswith(".py") or t.endswith("/")) and not t.rsplit("/", 1)[-1].startswith("__")} - pk

    def resolve(name: str) -> str:
        m = inv.module_of(name)
        if not m:
            return ""
        # the documents' tree may be rooted at the repository name: keep the package-relative tail
        parts = m.split("/")
        for i, part in enumerate(parts):
            if part in pk:
                return "/".join(parts[i:])
        return f"{packages[0]}/" + parts[-1]

    tests_dir = heldout_dir(task)
    fixture_mods: dict[str, set[str]] = {}
    for c in sorted(tests_dir.rglob("conftest.py"), key=lambda q: len(q.parts)):
        try:
            tree = ast.parse(c.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        imp = _imports(tree, pk, subs, resolve)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fixture_mods[fn.name] = {imp[n] for n in _used_names(fn) if n in imp}
    out: dict[str, set[str]] = {}
    for path in _test_files(tests_dir):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        imp = _imports(tree, pk, subs, resolve)
        local_fix = dict(fixture_mods)
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for _ in range(2):
            for fn in fns:
                if not fn.name.startswith("test"):
                    mods = {imp[n] for n in _used_names(fn) if n in imp}
                    for a in fn.args.args:
                        mods |= local_fix.get(a.arg, set())
                    local_fix[fn.name] = mods
        # module-level helpers referenced by a test count through their names (unittest bases too)
        rel = str(path.relative_to(tests_dir))
        # unittest classes: a method also uses what its class's non-test methods (setUp, helpers) use
        class_used: dict[int, set[str]] = {}
        for c in tree.body:
            if isinstance(c, ast.ClassDef):
                shared = set()
                for m in c.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("test"):
                        shared |= _used_names(m)
                for m in c.body:
                    class_used[id(m)] = shared
        for fn in fns:
            if not fn.name.startswith("test"):
                continue
            used = _used_names(fn) | class_used.get(id(fn), set())
            mods = {imp[n] for n in used if n in imp}
            for a in fn.args.args:
                mods |= local_fix.get(a.arg, set())
            for n in used:
                mods |= local_fix.get(n, set()) if n in local_fix and n not in imp else set()
            out[f"{rel}::{fn.name}"] = mods
    return out


def relevant_cases(task: str, plan_path: Path, packages: list[str]) -> dict[str, list[str]]:
    from orchestra.codeprojecteval.public_symbols import _under

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    order = [m["milestone_id"] for m in plan["milestones"]]
    # src-layout tasks (NL2Repo) list focus paths as src/<pkg>/...; imports resolve to <pkg>/...
    focus = {m["milestone_id"]: [f[4:] if str(f).startswith("src/") else f for f in (m.get("focus_paths") or [])]
             for m in plan["milestones"]}

    def forms(module: str) -> list[str]:
        base = module.removesuffix(".py")
        return [module, base, base + "/__init__.py"]

    def owns(mid: str, module: str) -> bool:
        return any(_under(x, f) for x in forms(module) for f in focus[mid])

    def owner_index(module: str) -> int | None:
        hits = [i for i, mid in enumerate(order) if owns(mid, module)]
        return min(hits) if hits else None

    mods = case_modules(task, packages)
    out: dict[str, list[str]] = {mid: [] for mid in order}
    for case, ms in mods.items():
        if not ms:
            continue
        idx = {m: owner_index(m) for m in ms}
        for k, mid in enumerate(order):
            uses_own = any(owns(mid, m) for m in ms)
            all_earlier = all(i is not None and i <= k for i in idx.values())
            if uses_own and all_earlier:
                out[mid].append(case)
    return {mid: sorted(v) for mid, v in out.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    data = attribute(args.task, args.plan)
    cfg = json.loads((DATASET_ROOT / args.task / "config.json").read_text(encoding="utf-8"))
    packages = [str(cfg.get("source_code") or args.task).removeprefix("src/").split("/")[0]]
    syms = case_symbols(args.task, packages)
    relevant = relevant_cases(args.task, args.plan, packages)
    text = json.dumps({"task_id": args.task, "attribution": data, "case_symbols": syms, "relevant": relevant}, indent=2)
    assert_no_source_overlap(text, args.task)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print("wrote", args.out, {k: len(v) for k, v in data.items()})


if __name__ == "__main__":
    main()
