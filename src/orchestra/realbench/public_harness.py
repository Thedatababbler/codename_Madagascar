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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PUBLIC_CHECK_SCRIPT_NAME = "adamas_public_check.py"
PUBLIC_HARNESS_MANIFEST_NAME = "adamas_public_harness.json"


@dataclass(frozen=True)
class PublicHarnessManifest:
    command: list[str]
    levels: dict[str, str]
    expected_modules: list[str]
    expected_exports: dict[str, list[str]]
    top_level_packages: list[str]
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
    return '''#!/usr/bin/env python3
"""AdaMAS public contract check for RealBench (runner-owned; no hidden tests).

Runs against a repository given by ``--root`` (default: cwd). The manifest and
milestone contracts are AdaMAS-owned files kept outside that repository.
"""

from __future__ import annotations

import argparse
import compileall
import importlib
import inspect
import json
import sys
from pathlib import Path


def _load_json(path: Path | None) -> dict:
    if path is None or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _export_candidates(modules: list[str], name: str) -> list[str]:
    """Modules that may host the symbols UML declares under ``name``."""
    if name in modules:
        return [name]
    return [mod for mod in modules if mod.rsplit(".", 1)[-1] == name]


def _missing_third_party(exc: BaseException, root: Path, packages: list[str]) -> str:
    """Return the name of an absent dependency this repository does not own.

    The evaluation environment installs the project's third-party requirements;
    this harness environment may not. Failing a milestone because ``pyproj`` is
    absent would punish the agent for something it cannot install.
    """
    if not isinstance(exc, ModuleNotFoundError) or not exc.name:
        return ""
    top = exc.name.split(".", 1)[0]
    if top in packages:
        return ""
    if (root / top).is_dir() or (root / f"{top}.py").is_file():
        return ""
    return top


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--level",
        choices=("discovery", "implementation", "integration"),
        default="integration",
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--contracts", default="")
    parser.add_argument("--root", default="")
    args = parser.parse_args()

    root = Path(args.root).resolve() if args.root else Path.cwd()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"FAIL: missing manifest {manifest_path}", file=sys.stderr)
        return 2
    manifest = _load_json(manifest_path)
    modules = list(manifest.get("expected_modules") or [])
    exports = dict(manifest.get("expected_exports") or {})
    packages = list(manifest.get("top_level_packages") or [])

    targets = [root / p for p in packages if (root / p).exists()]
    targets += [root / f"{p}.py" for p in packages if (root / f"{p}.py").is_file()]
    if not targets:
        targets = [root]
    for target in targets:
        if target.is_dir():
            ok = compileall.compile_dir(str(target), quiet=1)
        else:
            ok = compileall.compile_file(str(target), quiet=1)
        if not ok:
            print(f"FAIL: compileall failed under {target}", file=sys.stderr)
            return 1
    print(f"OK compileall level={args.level}")

    sys.path.insert(0, str(root))

    skipped_deps: set[str] = set()

    contracts = _load_json(Path(args.contracts) if args.contracts else None)
    if contracts:
        failed_contracts: list[str] = []
        for check in list(contracts.get("checks") or []):
            levels = check.get("required_levels") or ["integration"]
            if args.level not in levels:
                continue
            ctype = check.get("type")
            try:
                if ctype == "module_file_exists":
                    if not (root / str(check.get("path") or "")).exists():
                        failed_contracts.append(f"missing file {check.get('path')}")
                elif ctype == "import":
                    importlib.import_module(str(check["module"]))
                elif ctype == "export_any":
                    sym = str(check["symbol"])
                    mods = [str(m) for m in (check.get("modules") or [])]
                    hosts = []
                    for mod in mods:
                        try:
                            hosts.append(importlib.import_module(mod))
                        except Exception as exc:  # noqa: BLE001
                            dep = _missing_third_party(exc, root, packages)
                            if dep:
                                skipped_deps.add(dep)
                            continue
                    if hosts and not any(hasattr(obj, sym) for obj in hosts):
                        failed_contracts.append(
                            f"missing {sym} from any of {', '.join(mods)}"
                        )
                elif ctype in {"export", "callable_or_class"}:
                    loaded = importlib.import_module(str(check["module"]))
                    sym = str(check["symbol"])
                    if not hasattr(loaded, sym):
                        failed_contracts.append(f"missing {check['module']}.{sym}")
                    elif ctype == "callable_or_class":
                        obj = getattr(loaded, sym)
                        if not (inspect.isclass(obj) or callable(obj)):
                            failed_contracts.append(
                                f"not callable/class {check['module']}.{sym}"
                            )
            except Exception as exc:  # noqa: BLE001
                dep = _missing_third_party(exc, root, packages)
                if dep:
                    skipped_deps.add(dep)
                    continue
                failed_contracts.append(
                    f"{ctype}:{check}: {type(exc).__name__}: {exc}"
                )
        if failed_contracts:
            print("FAIL milestone contracts:", file=sys.stderr)
            for item in failed_contracts[:40]:
                print(f"  - {item}", file=sys.stderr)
            return 1
        print(f"OK milestone contracts level={args.level}")

    if args.level == "discovery":
        return 0

    failed_imports: list[str] = []
    unverified: list[str] = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001 — surface import contract failures
            dep = _missing_third_party(exc, root, packages)
            if dep:
                skipped_deps.add(dep)
                unverified.append(mod)
                continue
            failed_imports.append(f"{mod}: {type(exc).__name__}: {exc}")
    if failed_imports:
        print("FAIL imports:", file=sys.stderr)
        for item in failed_imports[:40]:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"OK imports count={len(modules) - len(unverified)}")
    if skipped_deps:
        print(
            "SKIP unavailable dependencies: "
            + ", ".join(sorted(skipped_deps))
            + f" (unverified modules: {len(unverified)})"
        )

    if args.level == "implementation":
        return 0

    # Integration: require UML-exported symbols when resolvable. A UML name is a
    # bare basename, so it may map onto several modules; hosting the symbol in
    # one of them satisfies the design.
    missing: list[str] = []
    for name, wanted in exports.items():
        candidates = _export_candidates(modules, name)
        loaded = []
        for mod in candidates:
            try:
                loaded.append((mod, importlib.import_module(mod)))
            except Exception:  # noqa: BLE001 — import failures reported above
                continue
        if not loaded:
            continue
        for sym in wanted:
            if not any(hasattr(obj, sym) for _, obj in loaded):
                missing.append(
                    " or ".join(f"{mod}.{sym}" for mod, _ in loaded)
                )
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


def public_check_command(
    *,
    harness_dir: Path,
    level: str,
    contracts_path: Path | None = None,
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

    manifest = PublicHarnessManifest(
        command=public_check_command(harness_dir=out, level="integration"),
        levels={
            "discovery": "compileall",
            "implementation": "compileall+imports",
            "integration": "compileall+imports+uml_exports",
        },
        expected_modules=modules,
        expected_exports=exports,
        top_level_packages=packages,
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
