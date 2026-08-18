"""How much of a held-out suite the design documents can actually specify.

The held-out ``unit_tests`` are the original project's own tests, so they import
the original project's internal names. `pyreverse` class diagrams cannot express
module-level constants, so names like ``bplustree.const.ENDIAN`` appear nowhere
in the PRD, the UML or the architecture document — yet a test module importing
one dies at collection and, without ``--continue-on-collection-errors``, takes
the whole session with it.

Treating that as a score for the generated code would be wrong, so the reachable
fraction is computed up front and reported alongside the raw number.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from orchestra.codeprojecteval.dataset import CpeTask
from orchestra.codeprojecteval.harness import expected_modules
from orchestra.codeprojecteval.shared_cache import get_or_compute

# Python convention rather than project-specific knowledge: an implementer is
# expected to provide `__version__` without being told.
CONVENTIONAL_NAMES = frozenset(
    {"__version__", "__all__", "__author__", "__doc__", "__main__"}
)


@dataclass(frozen=True)
class CeilingReport:
    task_id: str
    tests_total: int
    tests_reachable: int
    tests_blocked: int
    blocked_modules: list[str] = field(default_factory=list)
    undocumented_names: list[str] = field(default_factory=list)
    modules: list[dict] = field(default_factory=list)
    # Modules whose size came from counting `def test_*` because pytest never
    # reported them. Parametrisation makes that a large undercount, so any total
    # carrying estimates cannot be used as a denominator.
    estimated_modules: list[str] = field(default_factory=list)

    @property
    def estimated(self) -> bool:
        return bool(self.estimated_modules)

    @property
    def reachable_ceiling(self) -> float:
        return (
            round(self.tests_reachable / self.tests_total, 4)
            if self.tests_total
            else 0.0
        )

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "tests_total": self.tests_total,
            "tests_reachable": self.tests_reachable,
            "tests_blocked": self.tests_blocked,
            "reachable_ceiling": self.reachable_ceiling,
            "blocked_modules": list(self.blocked_modules),
            "undocumented_names": list(self.undocumented_names),
            "modules": list(self.modules),
            "estimated_modules": list(self.estimated_modules),
        }


def denominator_faults(report: CeilingReport, *, passed: int) -> list[str]:
    """Reasons this report's total cannot be used as a scoring denominator.

    A pass rate above 1 is not a good score, it is proof the denominator is wrong.
    Returning the reasons lets a caller report the raw counts and withhold the
    rate, which is recoverable, instead of publishing a number like 2.54.
    """
    faults: list[str] = []
    if not report.tests_total:
        faults.append("suite size unknown")
    if report.estimated:
        faults.append(f"{len(report.estimated_modules)} module(s) counted statically")
    if report.tests_total and passed > report.tests_total:
        faults.append(f"passed {passed} of a claimed {report.tests_total}")
    return faults


def reachable_is_unsound(report: CeilingReport, *, passed: int) -> bool:
    """Whether the measurement contradicts the reachability estimate.

    Reachability is a heuristic over which names the design documents mention, and
    it is too pessimistic on some tasks — pyjwt passes around 220 tests against a
    claimed 105 reachable. Where that happens the estimate, not the run, is wrong.
    """
    return bool(report.tests_reachable and passed > report.tests_reachable)


def _design_text(task: CpeTask) -> str:
    parts = [
        task.read(task.prd_path, limit=200000),
        task.read(task.architecture_path, limit=200000),
        task.directory_tree,
    ]
    parts.extend(task.read(p, limit=200000) for p in task.uml_paths)
    return "\n".join(parts)


def _imported_own_names(path: Path, packages: set[str]) -> set[str]:
    """Names a test module imports from the repository under test."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".", 1)[0] in packages:
                names.update(alias.name for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in packages:
                    names.add(alias.name.rsplit(".", 1)[-1])
    return names


def _count_tests(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return 0
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test")
    )


def collected_counts(
    task: CpeTask, *, python: Path, cache_path: Path | None = None
) -> dict[str, int]:
    """Test cases pytest actually collects per module, from the reference repo.

    Counting ``def test_*`` statically undercounts badly: one parametrised
    function expands into dozens of cases, which would put a measured pass rate
    above 1.0. The reference implementation is the only place these can be
    counted honestly, and the result is cached because it never changes.
    """
    def _modules() -> list[str]:
        """Every held-out module the dataset ships, collected or not.

        A module pytest reports nothing for — a helper base class, a version
        check that collects zero cases — used to be absent from the counts
        entirely, which is indistinguishable from a module nobody pinned. The
        ceiling then counted it statically and withheld the pass rate for both
        imapclient and simpy (EXP-20260811-01). Zero is a count; say so.
        """
        root = task.repo_root / task.unit_tests
        found = {
            path.relative_to(task.repo_root).as_posix()
            for pattern in ("test_*.py", "*_test.py")
            for path in root.rglob(pattern)
        }
        return sorted(found)

    def _collect() -> dict[str, int]:
        proc = subprocess.run(
            [
                str(python), "-m", "pytest", task.unit_tests, "--collect-only", "-q",
                "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
                "--continue-on-collection-errors",
            ],
            cwd=task.repo_root,
            env={
                "PYTHONPATH": str(task.repo_root),
                "PATH": f"{Path(python).parent}:/usr/bin:/bin:/usr/local/bin",
                "HOME": str(task.repo_root),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        counts: dict[str, int] = dict.fromkeys(_modules(), 0)
        for line in (proc.stdout or "").splitlines():
            node = line.strip()
            if "::" not in node or not node.endswith(tuple("]") + tuple(")")) and "::" not in node:
                continue
            module = node.split("::", 1)[0]
            if not module.endswith(".py"):
                continue
            counts[module] = counts.get(module, 0) + 1
        return counts

    return get_or_compute(
        Path(cache_path) if cache_path else None, task.task_id, _collect
    )


def analyze_ceiling(task: CpeTask, *, collected: dict[str, int] | None = None) -> CeilingReport:
    """Bound the held-out score achievable from the documents alone."""
    modules = expected_modules(task)
    packages = {mod.split(".", 1)[0] for mod in modules} | {task.source_dir}
    documented = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _design_text(task)))
    documented |= CONVENTIONAL_NAMES

    unit_root = task.repo_root / task.unit_tests
    per_module: list[dict] = []
    blocked_modules: list[str] = []
    estimated_modules: list[str] = []
    undocumented: set[str] = set()
    blocked = reachable = 0
    seen: set[Path] = set()
    for path in [
        *sorted(unit_root.rglob("test_*.py")),
        *sorted(unit_root.rglob("*_test.py")),
    ]:
        if path in seen:
            continue
        seen.add(path)
        imported = _imported_own_names(path, packages)
        missing = sorted(name for name in imported if name not in documented)
        relative = path.relative_to(task.repo_root).as_posix()
        # Prefer what pytest really collects; parametrisation makes the static
        # function count a large undercount. Falling back silently is what let
        # bplustree report a denominator of 59 in some runs and 356 in others —
        # a 6x drift on the same suite — so the fallback is now recorded.
        count = (collected or {}).get(relative)
        if count is None:
            count = _count_tests(path)
            estimated_modules.append(relative)
        if missing:
            blocked += count
            blocked_modules.append(relative)
            undocumented.update(missing)
        else:
            reachable += count
        per_module.append(
            {
                "module": relative,
                "tests": count,
                "undocumented_imports": missing,
            }
        )

    return CeilingReport(
        task_id=task.task_id,
        tests_total=blocked + reachable,
        tests_reachable=reachable,
        tests_blocked=blocked,
        blocked_modules=blocked_modules,
        undocumented_names=sorted(undocumented),
        modules=per_module,
        estimated_modules=estimated_modules,
    )
