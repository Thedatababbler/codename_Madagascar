"""Materialize AdaMAS-owned *public* contract checks for RealBench workspaces.

These checks are derived only from public_design/ (tree + package UML). They
never read hidden ``proj_with_test`` fixtures. Hidden evaluation remains a
separate offline script.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PUBLIC_CHECK_SCRIPT = "scripts/adamas_public_check.py"
PUBLIC_HARNESS_MANIFEST = "adamas_public_harness.json"
PUBLIC_TESTS_DIR = "tests_public"


@dataclass(frozen=True)
class PublicHarnessManifest:
    command: list[str]
    levels: dict[str, str]
    expected_modules: list[str]
    expected_exports: dict[str, list[str]]
    top_level_packages: list[str]
    source: str = "public_design/tree.txt+package.json"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_expected_modules(tree_text: str) -> list[str]:
    """Return dotted module names for ``.py`` files under the public tree."""
    path_stack: list[str] = []
    modules: list[str] = []
    for raw in tree_text.splitlines():
        line = raw.rstrip("\n")
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
    return '''#!/usr/bin/env python3
"""AdaMAS public contract check for RealBench (no hidden tests)."""

from __future__ import annotations

import argparse
import compileall
import importlib
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--level",
        choices=("discovery", "implementation", "integration"),
        default="integration",
    )
    args = parser.parse_args()
    root = Path.cwd()
    manifest_path = root / "adamas_public_harness.json"
    if not manifest_path.is_file():
        print("FAIL: missing adamas_public_harness.json", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    modules = list(manifest.get("expected_modules") or [])
    exports = dict(manifest.get("expected_exports") or {})
    packages = list(manifest.get("top_level_packages") or [])

    # Always: syntax check on expected package trees / modules.
    targets = [root / p for p in packages if (root / p).exists()]
    if not targets:
        targets = [root]
    for target in targets:
        ok = compileall.compile_dir(str(target), quiet=1) if target.is_dir() else compileall.compile_file(str(target), quiet=1)
        if not ok:
            print(f"FAIL: compileall failed under {target}", file=sys.stderr)
            return 1
    print(f"OK compileall level={args.level}")

    if args.level == "discovery":
        return 0

    sys.path.insert(0, str(root))
    failed_imports: list[str] = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 — surface import contract failures
            failed_imports.append(f"{mod}: {type(exc).__name__}: {exc}")
    if failed_imports:
        print("FAIL imports:", file=sys.stderr)
        for item in failed_imports[:40]:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"OK imports count={len(modules)}")

    if args.level == "implementation":
        return 0

    # Integration: require UML-exported symbols when resolvable.
    missing: list[str] = []
    for mod in modules:
        short = mod.rsplit(".", 1)[-1]
        wanted = exports.get(short) or exports.get(mod) or []
        if not wanted:
            continue
        try:
            loaded = importlib.import_module(mod)
        except Exception:
            continue
        for sym in wanted:
            if not hasattr(loaded, sym):
                missing.append(f"{mod}.{sym}")
    if missing:
        print("FAIL missing UML exports:", file=sys.stderr)
        for item in missing[:40]:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"OK export checks missing=0 considered_modules={len(modules)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _public_pytest_source() -> str:
    return '''"""Public contract tests generated from RealBench public_design only."""

from __future__ import annotations

import compileall
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "adamas_public_harness.json").read_text(encoding="utf-8"))
MODULES = list(MANIFEST.get("expected_modules") or [])
PACKAGES = list(MANIFEST.get("top_level_packages") or [])


def test_public_compileall() -> None:
    targets = [ROOT / p for p in PACKAGES if (ROOT / p).exists()] or [ROOT]
    for target in targets:
        if target.is_dir():
            assert compileall.compile_dir(str(target), quiet=1)
        else:
            assert compileall.compile_file(str(target), quiet=1)


@pytest.mark.parametrize("mod", MODULES)
def test_public_import(mod: str) -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    importlib.import_module(mod)
'''


def materialize_public_harness(workspace: Path) -> PublicHarnessManifest:
    """Write public harness assets into an existing public workspace."""
    tree_path = workspace / "public_design" / "tree.txt"
    package_path = workspace / "public_design" / "package.json"
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
    packages = _top_level_packages(modules)
    manifest = PublicHarnessManifest(
        command=["python", PUBLIC_CHECK_SCRIPT, "--level", "integration"],
        levels={
            "discovery": "compileall",
            "implementation": "compileall+imports",
            "integration": "compileall+imports+uml_exports",
        },
        expected_modules=modules,
        expected_exports=exports,
        top_level_packages=packages,
    )

    scripts = workspace / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    check_path = workspace / PUBLIC_CHECK_SCRIPT
    check_path.write_text(_public_check_source(), encoding="utf-8")
    check_path.chmod(check_path.stat().st_mode | 0o111)

    tests_public = workspace / PUBLIC_TESTS_DIR
    tests_public.mkdir(parents=True, exist_ok=True)
    (tests_public / "__init__.py").write_text("", encoding="utf-8")
    (tests_public / "test_public_imports.py").write_text(
        _public_pytest_source(), encoding="utf-8"
    )

    (workspace / PUBLIC_HARNESS_MANIFEST).write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Keep trusted marker for repository_test_harness gate.
    marker = workspace / ".adamas_trusted_harness"
    if not marker.exists():
        marker.write_text("trusted_fixture\n", encoding="utf-8")
    return manifest
