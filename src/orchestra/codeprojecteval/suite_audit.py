"""Mechanical audit of an authored suite against the fixed rules (spec §2.4).

Pure functions over the suite's source and the design documents; nothing here
runs tests or opens the held-out directory. The audit produces a list of
violations that the coverage step hands back to the author before custody
freezes the suite, and a list of *soft* cases (error paths the documents
promise without naming the exception class -- the user's rule of 2026-10-04).

Rules:

* ``citation``: every test carries at least one citation comment
  (``# PRD/Architecture/UML/Directory/Design/README ... "sentence"`` or
  ``# DOC: "sentence"``) and every cited sentence, normalised for whitespace
  and punctuation, is a substring of the documents.
* ``private``: no attribute whose name starts with ``_`` on anything (names
  the documents themselves use are exempt), never ``__dict__`` /
  ``__slots__`` / ``vars()``.
* ``error_path``: a ``pytest.raises(X)`` needs a citation that names ``X``;
  a citation that promises an error without a class allows only
  ``pytest.raises(Exception)`` and makes the case soft; no error in the
  citation, no ``raises`` at all.
* ``shim``: no ``sys.modules`` / ``sys.path`` edits, ``importlib.reload``,
  ``importorskip``, import fallbacks, monkeypatching of the project, or a
  suite module named like a project package.
* ``symbol``: every project symbol the suite imports or calls through the
  package is in the allowed set (the lenient public-symbol set), when one is
  given.
* ``import``: module top imports only ``pytest`` and the standard library
  every supported interpreter has.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

CITATION_RE = re.compile(
    r"#\s*(?:PRD|Architecture|UML|Directory|Design|README|DOC)\b[^\"\n]*\"(?P<quote>[^\"\n]{8,})\"", re.I
)
CITATION_PREFIX_RE = re.compile(r"#\s*(?:PRD|Architecture|UML|Directory|Design|README|DOC)\b", re.I)
EXCEPTION_NAME_RE = re.compile(r"\b([A-Z]\w*(?:Error|Exception|Warning|Exit|Interrupt|Invalid|Failure))\b|\b(KeyError|ValueError|TypeError|StopIteration|Invalid)\b")
ERROR_WORDS_RE = re.compile(r"\b(rais\w*|error\w*|exception\w*|invalid\w*|fail\w*|reject\w*|refus\w*|throw\w*|not (?:allowed|permitted|supported))\b", re.I)
#: stdlib modules that are not on every supported interpreter (added late or removed)
VERSION_DEPENDENT_STDLIB = frozenset({
    "tomllib", "zoneinfo", "graphlib", "distutils", "imp", "asynchat", "asyncore", "smtpd", "cgi", "cgitb",
    "crypt", "nntplib", "pipes", "sndhdr", "spwd", "sunau", "telnetlib", "uu", "xdrlib", "msilib", "nis",
    "ossaudiodev", "audioop", "aifc", "chunk", "mailcap", "imghdr", "lib2to3", "typing_extensions",
})
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))
_ALWAYS_ALLOWED_TOP = frozenset({"pytest", "__future__"})


@dataclass(frozen=True)
class Violation:
    rule: str          # citation | private | error_path | shim | symbol | import
    file: str
    case: str          # test function name, "" for module level
    line: int
    detail: str

    def to_dict(self) -> dict:
        return {"rule": self.rule, "file": self.file, "case": self.case, "line": self.line, "detail": self.detail}


@dataclass
class AuditResult:
    cases: int = 0
    cited: int = 0
    timeout_marks: int = 0
    violations: list[Violation] = field(default_factory=list)
    soft_cases: list[str] = field(default_factory=list)     # "file::test" whose error path is unnamed
    citations: dict[str, list[str]] = field(default_factory=dict)  # "file::test" -> normalised quotes

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def cited_ratio(self) -> float:
        return round(self.cited / self.cases, 3) if self.cases else 0.0

    def by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.violations:
            out[v.rule] = out.get(v.rule, 0) + 1
        return dict(sorted(out.items()))

    def to_dict(self) -> dict:
        return {"cases": self.cases, "cited": self.cited, "cited_ratio": self.cited_ratio, "timeout_marks": self.timeout_marks,
                "violations": [v.to_dict() for v in self.violations], "soft_cases": list(self.soft_cases), "by_rule": self.by_rule()}

    def to_author_text(self) -> str:
        """The list handed back to the author: one line per violation, grouped by test."""
        if not self.violations:
            return ""
        lines = ["The suite breaks these fixed rules; fix every item and change nothing else:", ""]
        groups: dict[str, list[Violation]] = {}
        for v in self.violations:
            groups.setdefault(f"{v.file}::{v.case}" if v.case else v.file, []).append(v)
        for key, vs in groups.items():
            lines.append(f"- `{key}`")
            for v in vs:
                lines.append(f"    - [{v.rule}] line {v.line}: {v.detail}")
        return "\n".join(lines)


def normalise(text: str) -> str:
    """Lower-case, drop punctuation and backticks, collapse whitespace."""
    t = text.lower().replace("`", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def _docs_text(docs: Mapping[str, str]) -> str:
    return normalise(" ".join(docs.values()))


def _comment_lines_above(lines: list[str], def_line: int) -> list[str]:
    """Comments directly above a def (decorators in between are skipped)."""
    out = []
    j = def_line - 2  # 0-based index of the line above the def
    while j >= 0 and lines[j].strip().startswith("@"):
        j -= 1
    while j >= 0 and lines[j].strip().startswith("#"):
        out.append(lines[j])
        j -= 1
    return out


def _comment_lines_in(lines: list[str], node: ast.AST) -> list[str]:
    end = getattr(node, "end_lineno", node.lineno)
    return [ln for ln in lines[node.lineno - 1 : end] if ln.strip().startswith("#")]


def _root_name(node: ast.AST) -> str:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if not isinstance(node, ast.Call) else node.func
    return node.id if isinstance(node, ast.Name) else ""


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _exception_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Tuple):
        return [n for e in node.elts for n in _exception_names(e)]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Name):
        return [node.id]
    return []


def _is_pytest_raises(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == "raises" and _root_name(call.func) == "pytest"


def audit_file(path: Path, docs: Mapping[str, str], *, packages: Iterable[str], allowed_symbols: set[str] | None = None,
               docs_norm: str | None = None) -> AuditResult:
    res = AuditResult()
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    fname = path.name
    pkgs = {p for p in packages if p}
    docs_norm = _docs_text(docs) if docs_norm is None else docs_norm
    docs_raw = " ".join(docs.values())
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        res.violations.append(Violation("syntax", fname, "", exc.lineno or 0, f"file does not parse: {exc.msg}"))
        return res

    # suite module named like a project package
    if path.stem in pkgs or path.stem.split(".")[0] in pkgs:
        res.violations.append(Violation("shim", fname, "", 1, f"suite module named like the project package {path.stem!r}"))

    # module-top imports
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for mod in mods:
                root = mod.split(".")[0]
                if root in _ALWAYS_ALLOWED_TOP:
                    continue
                if root in pkgs:
                    res.violations.append(Violation("import", fname, "", node.lineno, f"module-top import of the project ({mod}); import it inside the test"))
                elif root in VERSION_DEPENDENT_STDLIB:
                    res.violations.append(Violation("import", fname, "", node.lineno, f"module-top import of {mod}, not on every supported interpreter"))
                elif root not in _STDLIB:
                    res.violations.append(Violation("import", fname, "", node.lineno, f"module-top import of a third-party module ({mod})"))
        if isinstance(node, ast.Try):
            for h in node.handlers:
                for n in _exception_names(h.type) if h.type is not None else []:
                    if n in ("ImportError", "ModuleNotFoundError"):
                        res.violations.append(Violation("shim", fname, "", node.lineno, "import fallback (try/except ImportError) at module top"))

    res.timeout_marks += src.count("mark.timeout(")
    # module names the suite imports from the project (``from pkg.mod import X`` / ``import pkg.mod``)
    module_stems: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in pkgs:
            module_stems.update((n.module or "").split("."))
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in pkgs:
                    module_stems.update(a.name.split("."))
    # names the suite defines itself (helpers, stub classes and their methods, fixtures) are not
    # the project's private representation, whatever they are called
    own_names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    own_names.add(t.id)
                elif isinstance(t, ast.Attribute):
                    own_names.add(t.attr)  # self._x = ... inside a stub class

    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in funcs:
        is_test = fn.name.startswith("test")
        case = fn.name if is_test else f"<{fn.name}>"
        key = f"{fname}::{fn.name}"
        quotes: list[str] = []
        raw_quotes: list[str] = []
        if is_test:
            res.cases += 1
            comments = _comment_lines_above(lines, fn.lineno) + _comment_lines_in(lines, fn)
            prefixed = [c for c in comments if CITATION_PREFIX_RE.search(c)]
            for c in comments:
                for m in CITATION_RE.finditer(c):
                    raw_quotes.append(m.group("quote"))
                    quotes.append(normalise(m.group("quote")))
            if not prefixed:
                res.violations.append(Violation("citation", fname, fn.name, fn.lineno, "no citation comment above or inside the test"))
            else:
                if not quotes:
                    res.violations.append(Violation("citation", fname, fn.name, fn.lineno, "citation comment without a quoted sentence"))
                bad = [q for q in quotes if q not in docs_norm]
                if bad:
                    for q in bad[:3]:
                        res.violations.append(Violation("citation", fname, fn.name, fn.lineno, f"cited sentence is not in the documents: \"{q[:80]}\""))
                elif quotes:
                    res.cited += 1
            res.citations[key] = quotes

        for node in ast.walk(fn):
            # private representation
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                a = node.attr
                if a in ("__dict__", "__slots__"):
                    res.violations.append(Violation("private", fname, case, node.lineno, f"accesses {a}"))
                elif not (a.startswith("__") and a.endswith("__")):
                    if a not in docs_raw and a not in own_names:
                        res.violations.append(Violation("private", fname, case, node.lineno, f"accesses private attribute .{a}"))
            if isinstance(node, ast.Call):
                fn_name = _dotted(node.func)
                if fn_name == "vars" and node.args:
                    res.violations.append(Violation("private", fname, case, node.lineno, "vars() reads __dict__"))
                if fn_name in ("getattr", "setattr", "hasattr", "delattr") and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                        and isinstance(node.args[1].value, str) and node.args[1].value.startswith("_") and not node.args[1].value.startswith("__") \
                        and node.args[1].value not in docs_raw and node.args[1].value not in own_names:
                    res.violations.append(Violation("private", fname, case, node.lineno, f"{fn_name}(..., {node.args[1].value!r}) reaches a private name"))
                # shims
                if fn_name in ("importlib.reload", "pytest.importorskip", "__import__", "imp.reload"):
                    res.violations.append(Violation("shim", fname, case, node.lineno, f"{fn_name}() is a shim"))
                if fn_name in ("sys.path.insert", "sys.path.append", "sys.modules.pop", "sys.modules.setdefault", "sys.modules.update"):
                    res.violations.append(Violation("shim", fname, case, node.lineno, f"{fn_name}() edits the interpreter's import state"))
                if fn_name.endswith((".setattr", ".setitem", ".delattr", ".delitem")) and node.args:
                    target = node.args[0]
                    tname = _dotted(target) if not isinstance(target, ast.Constant) else str(target.value)
                    if tname.split(".")[0] in pkgs:
                        res.violations.append(Violation("shim", fname, case, node.lineno, f"monkeypatches the project ({tname})"))
                if fn_name in ("mock.patch", "unittest.mock.patch", "patch", "mock.patch.object") and node.args and isinstance(node.args[0], ast.Constant) \
                        and str(node.args[0].value).split(".")[0] in pkgs:
                    res.violations.append(Violation("shim", fname, case, node.lineno, f"patches the project ({node.args[0].value})"))
                # project symbols called through the package
                if allowed_symbols is not None and fn_name and fn_name.split(".")[0] in pkgs and "." in fn_name:
                    parts = fn_name.split(".")
                    leaf = parts[-1]
                    # pkg.func / pkg.mod.func / pkg.Class / pkg.mod.Class.method are checkable; a chain
                    # through an attribute the documents do not name (an instance, __version__) is not
                    middle = parts[1:-1]
                    checkable = not any(m.startswith("_") for m in parts) and all(
                        m in allowed_symbols or m in module_stems for m in middle)
                    if checkable and leaf not in allowed_symbols and fn_name not in allowed_symbols \
                            and not (middle and f"{middle[-1]}.{leaf}" in allowed_symbols):
                        res.violations.append(Violation("symbol", fname, case, node.lineno, f"calls {fn_name}, which the documents do not list"))
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
                targets = node.targets if isinstance(node, ast.Assign) else ([node.target] if isinstance(node, ast.AugAssign) else node.targets)
                for t in targets:
                    d = _dotted(t.value) if isinstance(t, ast.Subscript) else _dotted(t)
                    if d in ("sys.modules", "sys.path"):
                        res.violations.append(Violation("shim", fname, case, node.lineno, f"writes {d}"))
            if isinstance(node, ast.Try):
                for h in node.handlers:
                    for n in _exception_names(h.type) if h.type is not None else []:
                        if n in ("ImportError", "ModuleNotFoundError"):
                            res.violations.append(Violation("shim", fname, case, node.lineno, "import fallback (try/except ImportError)"))
            if isinstance(node, ast.ImportFrom) and allowed_symbols is not None and (node.module or "").split(".")[0] in pkgs:
                for a in node.names:
                    if a.name != "*" and a.name not in allowed_symbols and not a.name.startswith("_"):
                        res.violations.append(Violation("symbol", fname, case, node.lineno, f"imports {node.module}.{a.name}, which the documents do not list"))
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in pkgs and not is_test and fn.name not in ("fixture",):
                        pass  # imports inside fixtures/helpers are fine

        # error paths
        if is_test:
            raises = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _is_pytest_raises(n)]
            if raises:
                cited_text = " ".join(raw_quotes)
                cited_classes = {m.group(1) or m.group(2) for m in EXCEPTION_NAME_RE.finditer(cited_text)}
                for call in raises:
                    names = _exception_names(call.args[0]) if call.args else []
                    # a class the cited sentence spells out counts whatever it is called
                    cited_classes |= {n for n in names if re.search(rf"\b{re.escape(n)}\b", cited_text)}
                    if cited_classes:
                        missing = [n for n in names if n not in cited_classes and n not in cited_text]
                        if missing:
                            res.violations.append(Violation("error_path", fname, fn.name, call.lineno,
                                                            f"pytest.raises({', '.join(missing)}) but the cited sentence names {sorted(cited_classes)}"))
                    elif ERROR_WORDS_RE.search(cited_text):
                        if all(n in ("Exception", "BaseException") for n in names):
                            if key not in res.soft_cases:
                                res.soft_cases.append(key)
                        else:
                            res.violations.append(Violation("error_path", fname, fn.name, call.lineno,
                                                            f"pytest.raises({', '.join(names)}) but the cited sentence names no exception class; use pytest.raises(Exception)"))
                    else:
                        res.violations.append(Violation("error_path", fname, fn.name, call.lineno,
                                                        "pytest.raises in a test whose cited sentence mentions no error"))
    return res


def audit_suite(suite_dir: str | Path, docs: Mapping[str, str], *, packages: Iterable[str],
                allowed_symbols: set[str] | None = None) -> AuditResult:
    """Audit every ``*.py`` under ``suite_dir`` (recursively) and merge the results."""
    root = Path(suite_dir)
    total = AuditResult()
    docs_norm = _docs_text(docs)
    pkgs = list(packages)
    for p in sorted(root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        r = audit_file(p, docs, packages=pkgs, allowed_symbols=allowed_symbols, docs_norm=docs_norm)
        total.cases += r.cases
        total.cited += r.cited
        total.timeout_marks += r.timeout_marks
        total.violations.extend(r.violations)
        total.soft_cases.extend(r.soft_cases)
        total.citations.update(r.citations)
    # package-shaped directories inside the suite
    for d in root.rglob("*"):
        if d.is_dir() and d.name in set(pkgs):
            total.violations.append(Violation("shim", str(d.relative_to(root)), "", 0, f"directory named like the project package {d.name!r}"))
    total.violations.sort(key=lambda v: (v.file, v.line, v.rule))
    return total


__all__ = ["AuditResult", "CITATION_RE", "VERSION_DEPENDENT_STDLIB", "Violation", "audit_file", "audit_suite", "normalise"]
