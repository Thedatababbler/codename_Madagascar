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
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from orchestra.codeprojecteval.dataset import CpeTask
from orchestra.codeprojecteval.harness import expected_modules

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
        }


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
    cache: dict[str, dict[str, int]] = {}
    if cache_path and Path(cache_path).is_file():
        cache = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        if task.task_id in cache:
            return cache[task.task_id]

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
    counts: dict[str, int] = {}
    for line in (proc.stdout or "").splitlines():
        node = line.strip()
        if "::" not in node or not node.endswith(tuple("]") + tuple(")")) and "::" not in node:
            continue
        module = node.split("::", 1)[0]
        if not module.endswith(".py"):
            continue
        counts[module] = counts.get(module, 0) + 1

    if cache_path:
        cache[task.task_id] = counts
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps(cache, indent=2), encoding="utf-8")
    return counts


def analyze_ceiling(task: CpeTask, *, collected: dict[str, int] | None = None) -> CeilingReport:
    """Bound the held-out score achievable from the documents alone."""
    modules = expected_modules(task)
    packages = {mod.split(".", 1)[0] for mod in modules} | {task.source_dir}
    documented = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _design_text(task)))
    documented |= CONVENTIONAL_NAMES

    unit_root = task.repo_root / task.unit_tests
    per_module: list[dict] = []
    blocked_modules: list[str] = []
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
        # function count a large undercount.
        count = (collected or {}).get(relative)
        if count is None:
            count = _count_tests(path)
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
    )
