"""The project names a test case reaches, from its AST (names only).

Shared by the sealed held-out attribution (``scripts/sealed/heldout_attribution.py``) and
the evaluation of authored suites (``scripts/author_eval.py``): imported names, ``Name.attr``
on them, ``x = Cls(); x.attr``, the same through fixtures (module and conftest), unittest
``self.x = Cls()`` across modules, and ``from pkg import submodule``.
"""

from __future__ import annotations

import ast
from pathlib import Path

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
        # chained call on a class: Store().get(k) reaches Store.get
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Call):
            cls = _call_class(sub.value, imported)
            if cls:
                names.add(f"{cls}.{sub.attr}")
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




def suite_case_symbols(suite_dir: str | Path, packages: list[str], submodules: set[str] | None = None,
                       key_prefix: str = "") -> dict[str, list[str]]:
    """``<prefix><file>::<test>`` (``::Class::test`` for methods) -> names, for a suite directory."""
    root = Path(suite_dir)
    pk = set(packages)
    SUBMODULES.clear()
    SUBMODULES.update((submodules or set()) - pk)
    conftests: dict[Path, dict[str, tuple[set[str], str]]] = {}
    for c in sorted(root.rglob("conftest.py"), key=lambda q: len(q.parts)):
        try:
            tree = ast.parse(c.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        conftests[c.parent] = _fixtures_of(tree, _imports_of(tree, pk), pk, conftests.get(c.parent.parent, {}))
    global_attrs: dict[str, dict[str, str]] = {}
    files = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    trees = {}
    for p in files:
        try:
            trees[p] = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            global_attrs.update(class_self_attrs(trees[p], pk))
        except (OSError, SyntaxError):
            continue
    out: dict[str, list[str]] = {}
    for p, tree in trees.items():
        if p.name == "conftest.py":
            continue
        inherited: dict[str, tuple[set[str], str]] = {}
        for d in reversed(p.parents):
            inherited.update(conftests.get(d, {}))
        owner = {id(m): c.name for c in tree.body if isinstance(c, ast.ClassDef) for m in c.body}
        names_by_fn = _project_symbols(tree, pk, inherited, global_attrs)
        rel = p.relative_to(root).as_posix()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                key = f"{key_prefix}{rel}::{owner[id(fn)]}::{fn.name}" if id(fn) in owner else f"{key_prefix}{rel}::{fn.name}"
                out[key] = names_by_fn.get(fn.name, [])
    return out
