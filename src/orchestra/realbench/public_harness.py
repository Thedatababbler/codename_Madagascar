"""Materialize AdaMAS-owned *public* contract checks for RealBench workspaces.

These checks are derived only from public_design/ (tree + package UML). They
never read hidden ``proj_with_test`` fixtures. Hidden evaluation remains a
separate offline script.

Isolation invariant: harness assets (check script, manifest, milestone contract
JSON) are **runner-owned** and live outside the agent workspace, so the agent
repository contains dataset-derived files only. The harness runs with ``cwd``
set to the repository but every AdaMAS path it touches is absolute.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PUBLIC_CHECK_SCRIPT_NAME = "adamas_public_check.py"
PUBLIC_HARNESS_MANIFEST_NAME = "adamas_public_harness.json"
SPEC_TESTS_SUFFIX = ".spec_tests"
SPEC_TESTS_DIRNAME = "spec_tests"


@dataclass(frozen=True)
class PublicHarnessManifest:
    command: list[str]
    levels: dict[str, str]
    expected_modules: list[str]
    expected_exports: dict[str, list[str]]
    top_level_packages: list[str]
    spec_tests_dir: str = SPEC_TESTS_DIRNAME
    pristine_repo: str = ""
    script_path: str = ""
    manifest_path: str = ""
    source: str = "public_design/tree.txt+package.json"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_INDENT_SPACES = str.maketrans({"\xa0": " ", "\u202f": " ", "\u2007": " ", "\t": "    "})


def parse_expected_modules(tree_text: str) -> list[str]:
    """Return dotted module names for ``.py`` files under the public tree."""
    path_stack: list[str] = []
    modules: list[str] = []
    for raw in tree_text.splitlines():
        # `tree` output indents with non-breaking or narrow spaces depending on
        # locale; depth is counted in plain spaces, so normalise first or every
        # child collapses to the top level.
        line = raw.rstrip("\n").translate(_INDENT_SPACES)
        if not line.strip():
            continue
        match = re.match(r"^([\s│]*)(?:├──|└──)?\s*(.*)$", line)
        if not match:
            continue
        prefix, name = match.group(1), match.group(2).strip()
        if not name or name in {"│", "├──", "└──"}:
            continue
        depth = len(re.findall(r"(?: {4}|│   )", prefix))
        is_dir = name.endswith("/")
        name = name.rstrip("/")
        if depth == 0 and name in {"proj_clean", "proj_with_test", "."}:
            path_stack = []
            continue
        path_stack = path_stack[:depth]
        path_stack.append(name)
        if is_dir or "." not in name:
            continue
        if not name.endswith(".py"):
            continue
        parts = list(path_stack)
        if parts and parts[0] in {"proj_clean", "proj_with_test"}:
            parts = parts[1:]
        if not parts:
            continue
        if parts[-1] == "__init__.py":
            mod_parts = parts[:-1]
        else:
            mod_parts = parts[:-1] + [parts[-1][:-3]]
        if not mod_parts:
            continue
        modules.append(".".join(mod_parts))
    # Stable unique order.
    seen: set[str] = set()
    out: list[str] = []
    for mod in modules:
        if mod in seen:
            continue
        seen.add(mod)
        out.append(mod)
    return out


def parse_package_exports(package_json: dict[str, Any]) -> dict[str, list[str]]:
    """Map package/module names from UML package.json to exported symbols."""
    diagram = package_json.get("packageDiagram") or package_json
    packages = diagram.get("packages") if isinstance(diagram, dict) else None
    if not isinstance(packages, list):
        return {}
    out: dict[str, list[str]] = {}
    for item in packages:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        exports = item.get("exports") or []
        if not name or not isinstance(exports, list):
            continue
        symbols = [str(x) for x in exports if str(x).strip()]
        if symbols:
            out[name] = symbols
    return out


def load_public_design(
    workspace: Path,
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """Return (modules, exports, top_level_packages) parsed from public_design/."""
    tree_path = Path(workspace) / "public_design" / "tree.txt"
    package_path = Path(workspace) / "public_design" / "package.json"
    tree_text = (
        tree_path.read_text(encoding="utf-8", errors="ignore")
        if tree_path.is_file()
        else ""
    )
    package_json: dict[str, Any] = {}
    if package_path.is_file():
        try:
            raw = json.loads(package_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                package_json = raw
        except json.JSONDecodeError:
            package_json = {}
    modules = parse_expected_modules(tree_text)
    exports = parse_package_exports(package_json)
    return modules, exports, _top_level_packages(modules)


def export_candidates(modules: list[str], name: str) -> list[str]:
    """Modules that may host the symbols UML declares under ``name``.

    UML package names are bare basenames, so a repository with two ``abstract.py``
    files maps one export list onto both. Requiring the symbol in every candidate
    invents a contract the design never stated; hosting it in one is enough.
    """
    if name in modules:
        return [name]
    return [mod for mod in modules if mod.rsplit(".", 1)[-1] == name]


def _top_level_packages(modules: list[str]) -> list[str]:
    roots: list[str] = []
    seen: set[str] = set()
    for mod in modules:
        root = mod.split(".", 1)[0]
        if root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return roots


def _public_check_source() -> str:
    """The standalone check script, copied beside the manifest at materialize.

    Kept as its own module so the Progress / custody / spec_tests logic can be
    read and tested as ordinary Python rather than a string literal. The copy
    that runs against a repository still has no AdaMAS imports: the agent's
    environment is not required to have this package installed.
    """
    return Path(__file__).with_name("public_check.py").read_text(encoding="utf-8")


def _safe_milestone(milestone_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", milestone_id) or "milestone"


def spec_tests_path_for(harness_dir: Path, milestone_id: str) -> Path:
    """Where this milestone's authored suite is frozen.

    Per milestone rather than per attempt: every fast-loop candidate is scored
    against the suite the first attempt wrote, which is the only way their
    quality scores mean the same thing.
    """
    return Path(harness_dir) / f"{_safe_milestone(milestone_id)}{SPEC_TESTS_SUFFIX}"


def public_check_command(
    *,
    harness_dir: Path,
    level: str,
    contracts_path: Path | None = None,
    spec_tests_path: Path | None = None,
) -> list[str]:
    """Absolute harness command for one milestone level.

    The repository under test is whatever ``cwd`` the harness runs in (a forked
    subtask workspace or the commit staging copy), so no root is pinned here.
    """
    harness = Path(harness_dir)
    command = [
        "python",
        str(harness / PUBLIC_CHECK_SCRIPT_NAME),
        "--manifest",
        str(harness / PUBLIC_HARNESS_MANIFEST_NAME),
        "--level",
        level,
    ]
    if contracts_path is not None:
        command += ["--contracts", str(contracts_path)]
    if spec_tests_path is not None:
        command += ["--spec-tests", str(spec_tests_path)]
    return command


def materialize_public_harness(
    workspace: Path, *, harness_dir: Path
) -> PublicHarnessManifest:
    """Write runner-owned harness assets for ``workspace`` into ``harness_dir``.

    Nothing is written into the workspace: the agent repository keeps only
    dataset-derived files.
    """
    modules, exports, packages = load_public_design(Path(workspace))
    out = Path(harness_dir)
    out.mkdir(parents=True, exist_ok=True)
    check_path = out / PUBLIC_CHECK_SCRIPT_NAME
    manifest_path = out / PUBLIC_HARNESS_MANIFEST_NAME
    # The baseline an authored test has to be measured against is the repository
    # as the agent received it. Copied now, before anyone writes into it.
    pristine = out / "pristine_repo"
    if not pristine.exists():
        shutil.copytree(
            Path(workspace),
            pristine,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "spec_tests"),
        )

    manifest = PublicHarnessManifest(
        command=public_check_command(harness_dir=out, level="integration"),
        levels={
            "discovery": "compileall",
            "implementation": "compileall+imports+contracts(+spec_tests, graded)",
            "integration": "compileall+imports+uml_exports(+spec_tests, graded)",
        },
        expected_modules=modules,
        expected_exports=exports,
        top_level_packages=packages,
        spec_tests_dir=SPEC_TESTS_DIRNAME,
        pristine_repo=str(pristine),
        script_path=str(check_path),
        manifest_path=str(manifest_path),
    )

    check_path.write_text(_public_check_source(), encoding="utf-8")
    check_path.chmod(check_path.stat().st_mode | 0o111)
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
