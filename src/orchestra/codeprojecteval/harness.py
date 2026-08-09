"""Runner-owned milestone acceptance harness for CodeProjectEval.

Unlike RealBench, the acceptance signal here is not invented: the dataset ships a
visible ``check_tests`` suite intended for development-time feedback, and holds
back ``unit_tests`` for scoring. A milestone gate therefore runs real tests.

Isolation invariant carries over from RealBench: the check script, its manifest
and the per-milestone contract JSON live outside the agent workspace and are
addressed by absolute path, so an agent can neither read nor rewrite its own gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orchestra.codeprojecteval.dataset import CpeTask
from orchestra.realbench.public_harness import parse_expected_modules

CHECK_SCRIPT_NAME = "adamas_cpe_check.py"
CHECK_MANIFEST_NAME = "adamas_cpe_harness.json"
CONTRACTS_SUFFIX = ".contracts.json"


@dataclass(frozen=True)
class CpeHarnessManifest:
    levels: dict[str, str]
    expected_modules: list[str]
    top_level_packages: list[str]
    check_tests_dir: str
    source_dir: str
    check_tests_digest: dict[str, str] = field(default_factory=dict)
    script_path: str = ""
    manifest_path: str = ""
    env_python: str = ""
    command: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _digest_check_tests(task: CpeTask) -> dict[str, str]:
    """Hash the visible suite so a milestone cannot pass by rewriting it."""
    root = task.repo_root / task.check_tests
    digest: dict[str, str] = {}
    if not root.is_dir():
        return digest
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            relative = path.relative_to(task.repo_root).as_posix()
            digest[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def expected_modules(task: CpeTask) -> list[str]:
    """Import paths the repository must provide, per its declared tree.

    Some trees are rooted at the distribution name rather than the package
    (``djangorestframework-simplejwt/`` holding what imports as
    ``rest_framework_simplejwt``), so the root is taken from ``config.json``.
    """
    modules = parse_expected_modules(task.directory_tree)
    if not modules or not task.source_dir:
        return modules
    roots = {mod.split(".", 1)[0] for mod in modules}
    if roots != {task.source_dir} and len(roots) == 1:
        stale = roots.pop()
        modules = [
            task.source_dir + mod[len(stale) :] if mod.startswith(stale) else mod
            for mod in modules
        ]
    return modules


def _top_level_packages(modules: list[str]) -> list[str]:
    roots: list[str] = []
    for mod in modules:
        root = mod.split(".", 1)[0]
        if root not in roots:
            roots.append(root)
    return roots


def _check_script_source() -> str:
    return '''#!/usr/bin/env python3
"""AdaMAS milestone acceptance check for CodeProjectEval (runner-owned).

Runs against the repository given by ``cwd``. The manifest, contracts and this
script are AdaMAS-owned files kept outside that repository. Held-out
``unit_tests`` are never referenced here; only the dataset's visible
``check_tests`` are executed.
"""

from __future__ import annotations

import argparse
import compileall
import hashlib
import importlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path


def _load_json(path):
    if path is None or not Path(path).is_file():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _missing_third_party(exc, root, packages):
    """Name of an absent dependency this repository does not own."""
    if not isinstance(exc, ModuleNotFoundError) or not exc.name:
        return ""
    top = exc.name.split(".", 1)[0]
    if top in packages:
        return ""
    if (root / top).is_dir() or (root / (top + ".py")).is_file():
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
    parser.add_argument("--tests", default="", help="pytest node ids or paths")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    root = Path.cwd()
    manifest = _load_json(args.manifest)
    if not manifest:
        print("FAIL: missing manifest " + str(args.manifest), file=sys.stderr)
        return 2
    modules = list(manifest.get("expected_modules") or [])
    packages = list(manifest.get("top_level_packages") or [])
    check_dir = str(manifest.get("check_tests_dir") or "check_tests")

    targets = [root / p for p in packages if (root / p).exists()]
    targets += [root / (p + ".py") for p in packages if (root / (p + ".py")).is_file()]
    if not targets:
        print(
            "FAIL: none of the declared packages exist yet: " + ", ".join(packages),
            file=sys.stderr,
        )
        return 1
    for target in targets:
        ok = (
            compileall.compile_dir(str(target), quiet=1)
            if target.is_dir()
            else compileall.compile_file(str(target), quiet=1)
        )
        if not ok:
            print("FAIL: compileall failed under " + str(target), file=sys.stderr)
            return 1
    print("OK compileall level=" + args.level)

    if args.level == "discovery":
        return 0

    sys.path.insert(0, str(root))
    skipped_deps = set()
    failed_imports = []
    for mod in modules:
        try:
            importlib.import_module(mod)
        except Exception as exc:  # noqa: BLE001
            dep = _missing_third_party(exc, root, packages)
            if dep:
                skipped_deps.add(dep)
                continue
            failed_imports.append(mod + ": " + type(exc).__name__ + ": " + str(exc))
    if failed_imports:
        print("FAIL imports:", file=sys.stderr)
        for item in failed_imports[:40]:
            print("  - " + item, file=sys.stderr)
        return 1
    print("OK imports count=" + str(len(modules)))

    contracts = _load_json(args.contracts) if args.contracts else {}
    failed_contracts = []
    for check in list(contracts.get("checks") or []):
        levels = check.get("required_levels") or ["integration"]
        if args.level not in levels:
            continue
        ctype = check.get("type")
        try:
            if ctype == "module_file_exists":
                if not (root / str(check.get("path") or "")).exists():
                    failed_contracts.append("missing file " + str(check.get("path")))
            elif ctype == "import":
                importlib.import_module(str(check["module"]))
            elif ctype in ("export", "callable_or_class"):
                loaded = importlib.import_module(str(check["module"]))
                sym = str(check["symbol"])
                if not hasattr(loaded, sym):
                    failed_contracts.append(
                        "missing " + str(check["module"]) + "." + sym
                    )
                elif ctype == "callable_or_class":
                    obj = getattr(loaded, sym)
                    if not (inspect.isclass(obj) or callable(obj)):
                        failed_contracts.append(
                            "not callable/class " + str(check["module"]) + "." + sym
                        )
            elif ctype == "export_any":
                sym = str(check["symbol"])
                mods = [str(m) for m in (check.get("modules") or [])]
                hosts = []
                for mod in mods:
                    try:
                        hosts.append(importlib.import_module(mod))
                    except Exception:  # noqa: BLE001
                        continue
                if hosts and not any(hasattr(obj, sym) for obj in hosts):
                    failed_contracts.append(
                        "missing " + sym + " from any of " + ", ".join(mods)
                    )
        except Exception as exc:  # noqa: BLE001
            dep = _missing_third_party(exc, root, packages)
            if dep:
                skipped_deps.add(dep)
                continue
            failed_contracts.append(
                str(ctype) + ": " + type(exc).__name__ + ": " + str(exc)
            )
    if failed_contracts:
        print("FAIL milestone contracts:", file=sys.stderr)
        for item in failed_contracts[:40]:
            print("  - " + item, file=sys.stderr)
        return 1
    if contracts:
        print("OK milestone contracts level=" + args.level)
    if skipped_deps:
        print("SKIP unavailable dependencies: " + ", ".join(sorted(skipped_deps)))

    if args.level != "integration":
        return 0

    # The visible suite is evidence, not workspace material: an agent that edits
    # it can make any implementation pass.
    tampered = []
    for relative, expected in (manifest.get("check_tests_digest") or {}).items():
        path = root / relative
        if not path.is_file():
            tampered.append(relative + " (deleted)")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            tampered.append(relative + " (modified)")
    if tampered:
        print("FAIL: visible check_tests were altered:", file=sys.stderr)
        for item in tampered[:20]:
            print("  - " + item, file=sys.stderr)
        return 1

    selection = [t for t in args.tests.split(",") if t.strip()] or [check_dir]
    if not any((root / t.split("::", 1)[0]).exists() for t in selection):
        print("FAIL: no visible tests found at " + ", ".join(selection), file=sys.stderr)
        return 1
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        # Repositories bolt coverage thresholds, mypy and pycodestyle onto
        # pytest; those judge style, not whether the milestone works.
        "-o",
        "addopts=",
        *selection,
    ]
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("FAIL check_tests: timed out", file=sys.stderr)
        return 1
    tail = (proc.stdout or "")[-4000:]
    if proc.returncode != 0:
        print("FAIL check_tests:", file=sys.stderr)
        print(tail, file=sys.stderr)
        print((proc.stderr or "")[-1500:], file=sys.stderr)
        return 1
    print("OK check_tests")
    print(tail[-500:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def materialize_check_harness(
    task: CpeTask,
    *,
    harness_dir: Path,
    env_python: Path | None = None,
) -> CpeHarnessManifest:
    """Write the runner-owned check script and manifest outside the workspace."""
    out = Path(harness_dir)
    out.mkdir(parents=True, exist_ok=True)
    modules = expected_modules(task)
    packages = _top_level_packages(modules)
    if task.source_dir and task.source_dir not in packages:
        packages.insert(0, task.source_dir)

    script_path = out / CHECK_SCRIPT_NAME
    script_path.write_text(_check_script_source(), encoding="utf-8")
    python = Path(env_python or task.env_python)
    manifest = CpeHarnessManifest(
        levels={
            "discovery": "compileall",
            "implementation": "compileall+imports+contracts",
            "integration": "compileall+imports+contracts+check_tests",
        },
        expected_modules=modules,
        top_level_packages=packages,
        check_tests_dir=task.check_tests,
        source_dir=task.source_dir,
        check_tests_digest=_digest_check_tests(task),
        script_path=str(script_path),
        manifest_path=str(out / CHECK_MANIFEST_NAME),
        env_python=str(python),
    )
    manifest = CpeHarnessManifest(
        **{
            **manifest.to_dict(),
            "command": check_command(
                harness_dir=out, level="integration", env_python=python
            ),
        }
    )
    (out / CHECK_MANIFEST_NAME).write_text(
        json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
    )
    return manifest


def check_command(
    *,
    harness_dir: Path,
    level: str,
    env_python: Path,
    contracts_path: Path | None = None,
    tests: list[str] | None = None,
) -> list[str]:
    """Absolute harness command for one milestone level.

    The repository under test is whatever ``cwd`` the harness runs in, and the
    interpreter is the repository's own environment so third-party imports
    resolve the way they will during scoring.
    """
    out = Path(harness_dir)
    command = [
        str(env_python),
        str(out / CHECK_SCRIPT_NAME),
        "--manifest",
        str(out / CHECK_MANIFEST_NAME),
        "--level",
        level,
    ]
    if contracts_path is not None:
        command += ["--contracts", str(contracts_path)]
    if tests:
        command += ["--tests", ",".join(tests)]
    return command


def contracts_path_for(harness_dir: Path, milestone_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", milestone_id) or "milestone"
    return Path(harness_dir) / f"{safe}{CONTRACTS_SUFFIX}"


def build_deterministic_contracts(
    task: CpeTask,
    *,
    role: str,
    focus_paths: list[str] | None = None,
    milestone_id: str | None = None,
    extra_checks: list[dict[str, Any]] | None = None,
    acceptance_criteria: list[str] | None = None,
    corner_cases: list[str] | None = None,
) -> dict[str, Any]:
    """Baseline checks derived from the dataset's declared directory tree."""
    modules = expected_modules(task)
    focus = [str(p).replace("\\", "/").strip("/") for p in (focus_paths or []) if p]
    scoped = modules
    if focus:
        narrowed = [
            mod
            for mod in modules
            if any(
                mod == f
                or mod.startswith(f + ".")
                or mod.replace(".", "/").startswith(f.rstrip("/") + "/")
                or mod.replace(".", "/") + ".py" == f
                for f in focus
            )
        ]
        scoped = narrowed or modules

    checks: list[dict[str, Any]] = []
    for pkg in _top_level_packages(modules)[:8]:
        is_package = any(mod.startswith(pkg + ".") for mod in modules)
        checks.append(
            {
                "type": "module_file_exists",
                "path": f"{pkg}/__init__.py" if is_package else f"{pkg}.py",
                "required_levels": ["discovery", "implementation", "integration"],
            }
        )
    for mod in scoped[:60]:
        checks.append(
            {
                "type": "import",
                "module": mod,
                "required_levels": ["implementation", "integration"],
            }
        )
    for check in extra_checks or []:
        if isinstance(check, dict) and check.get("type"):
            checks.append(check)

    return {
        "schema_version": "1.0",
        "dataset": "CodeProjectEval",
        "milestone_id": milestone_id or role,
        "role": role,
        "focus_paths": focus,
        "acceptance_criteria": list(acceptance_criteria or []),
        "corner_cases": list(corner_cases or []),
        "checks": checks,
    }
