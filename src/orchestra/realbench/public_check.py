#!/usr/bin/env python3
"""AdaMAS public contract check for RealBench (runner-owned; no hidden tests).

Runs against a repository given by ``--root`` (default: cwd). The manifest and
milestone contracts are AdaMAS-owned files kept outside that repository.

The structural stages (compile, import, UML/contracts) still decide the exit
code. An authored ``spec_tests/`` suite, when present, is graded into the
score line and never gates: it moves the score, not the exit code. Held-out
``proj_with_test`` fixtures are never referenced here.
"""

from __future__ import annotations

import argparse
import ast
import compileall
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def _load_json(path):
    if path is None or not Path(path).is_file():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _export_candidates(modules, name):
    """Modules that may host the symbols UML declares under ``name``."""
    if name in modules:
        return [name]
    return [mod for mod in modules if mod.rsplit(".", 1)[-1] == name]


def _missing_third_party(exc, root, packages):
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


# How far through the level's stages a milestone got. A bare pass/fail makes
# every failing attempt look identical: an implementation that compiled and
# imported everything but failed two authored tests would score exactly what
# an empty repository scores.
STAGE_WEIGHTS = {
    "discovery": {"compile": 1.0},
    "implementation": {"compile": 0.3, "imports": 0.4, "contracts": 0.3},
    "integration": {"compile": 0.2, "imports": 0.3, "contracts": 0.5},
}

# Used instead of the above when a frozen specification suite is available to
# grade. Kept as a separate table rather than renormalising the one above: the
# structural stages saturate, so a level that can measure behaviour has to give
# behaviour most of the weight, and a milestone with no authored suite must keep
# scoring exactly as it did before this existed.
SPEC_STAGE_WEIGHTS = {
    "implementation": {
        "compile": 0.15,
        "imports": 0.25,
        "contracts": 0.2,
        "spec_tests": 0.4,
    },
    "integration": {
        "compile": 0.15,
        "imports": 0.2,
        "contracts": 0.25,
        "spec_tests": 0.4,
    },
}


class Progress:
    """Records each stage's passing ratio and prints them for the runner."""

    def __init__(self, level, spec_tests=False):
        self.level = level
        self.spec_tests = bool(spec_tests) and level in SPEC_STAGE_WEIGHTS
        self.stages = []

    @property
    def weights(self):
        if self.spec_tests:
            return SPEC_STAGE_WEIGHTS[self.level]
        return STAGE_WEIGHTS.get(self.level, STAGE_WEIGHTS["integration"])

    def drop_spec_stage(self):
        """Score as if no suite existed, once it turns out none can be graded.

        An unreadable or entirely vacuous suite must not be a zero on a 0.4
        weight: that would rank every design in the milestone below one that
        never authored tests at all.
        """
        self.spec_tests = False
        self.stages = [e for e in self.stages if e["stage"] != "spec_tests"]

    def record(self, stage, passed_units, total_units, failed_tests=None):
        if stage not in self.weights:
            return
        entry = {
            "stage": stage,
            "passed_units": int(passed_units),
            "total_units": int(total_units),
        }
        if failed_tests:
            entry["failed_tests"] = sorted({str(t) for t in failed_tests})
        self.stages.append(entry)

    def score(self):
        weights = self.weights
        total = 0.0
        for entry in self.stages:
            units = entry["total_units"]
            ratio = entry["passed_units"] / units if units else 0.0
            total += weights.get(entry["stage"], 0.0) * ratio
        return round(min(1.0, total), 6)

    def emit(self):
        weights = self.weights
        for entry in self.stages:
            entry["weight"] = weights.get(entry["stage"], 0.0)
        reached = [e["stage"] for e in self.stages if e["passed_units"]]
        print(
            "ADAMAS_HARNESS_SCORE "
            + json.dumps(
                {
                    "score": self.score(),
                    "level": self.level,
                    "stages": self.stages,
                    "furthest_stage": reached[-1] if reached else "",
                }
            )
        )


def _pytest_counts(output):
    """(passed, total, ran) from pytest's summary line, or zeros if unreadable."""
    passed = failed = errors = 0
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if " passed" not in line and " failed" not in line and " error" not in line:
            continue
        for count, word in re.findall(r"(\d+)\s+(passed|failed|errors?|xfailed)", line):
            if word == "passed":
                passed = int(count)
            elif word == "failed":
                failed = int(count)
            elif word.startswith("error"):
                errors = int(count)
        if passed or failed or errors:
            return passed, passed + failed + errors, passed + failed
    return 0, 0, 0


def _has_tests(directory):
    path = Path(directory)
    if not path.is_dir():
        return False
    return any(
        p.is_file() and "__pycache__" not in p.parts for p in path.rglob("test_*.py")
    )


def _failed_test_ids(output):
    ids = set()
    for line in (output or "").splitlines():
        line = line.strip()
        for prefix in ("FAILED ", "ERROR "):
            if not line.startswith(prefix):
                continue
            rest = line[len(prefix) :].strip()
            if not rest:
                continue
            ids.add(rest.split(" - ", 1)[0].strip())
    return ids


def _run_pytest(cwd, target, *, timeout, import_root=None):
    """Run one suite: (passed, total, ran, tail, failed_ids). Never raises."""
    env = dict(os.environ)
    roots = [str(import_root or cwd)]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([p for p in roots + [existing] if p])
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "--continue-on-collection-errors",
        "-rfE",
        str(target),
    ]
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 0, 0, 0, "timed out", set()
    passed, total, ran = _pytest_counts(proc.stdout)
    return (
        passed,
        total,
        ran,
        (proc.stdout or "")[-2000:],
        _failed_test_ids(proc.stdout),
    )


def _freeze_spec_suite(root, source_rel, frozen_dir):
    """Move the authored suite out of the workspace, once, and keep it out."""
    frozen = Path(frozen_dir)
    source = Path(root) / source_rel
    if not frozen.is_dir():
        if not _has_tests(source):
            return False
        staging = frozen.parent / (frozen.name + ".staging")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(
            source, staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        try:
            os.replace(str(staging), str(frozen))
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
        if not frozen.is_dir():
            return False
    if source.exists() and source.resolve() != frozen.resolve():
        shutil.rmtree(source, ignore_errors=True)
    return True


def _looks_like_test_class(node):
    if node.name.startswith("Test"):
        return True
    return any("TestCase" in ast.dump(base) for base in node.bases)


def _authored_case_count(frozen_dir):
    total = 0
    for path in sorted(Path(frozen_dir).rglob("*.py")):
        if path.name == "conftest.py" or "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test"):
                    total += 1
            elif isinstance(node, ast.ClassDef) and _looks_like_test_class(node):
                total += len(
                    [
                        item
                        for item in node.body
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name.startswith("test")
                    ]
                )
    return total


def _pinned_case_total(frozen_dir, collected):
    frozen = Path(frozen_dir)
    cache = frozen.parent / (frozen.name + ".size.json")
    pinned = 0
    if cache.is_file():
        try:
            pinned = int(json.loads(cache.read_text(encoding="utf-8"))["pinned"])
        except Exception:  # noqa: BLE001
            pinned = 0
    static = _authored_case_count(frozen)
    updated = max(pinned, static, int(collected or 0))
    if updated != pinned:
        try:
            cache.write_text(
                json.dumps({"pinned": updated, "static": static}), encoding="utf-8"
            )
        except OSError:
            pass
    return updated


def _vacuous_count(frozen_dir, pristine_repo, timeout):
    """How many authored tests pass with no implementation present."""
    frozen = Path(frozen_dir)
    cache = frozen.parent / (frozen.name + ".baseline.json")
    if cache.is_file():
        try:
            return int(json.loads(cache.read_text(encoding="utf-8"))["vacuous"])
        except Exception:  # noqa: BLE001
            pass
    if not pristine_repo or not Path(pristine_repo).is_dir():
        return 0
    scratch = frozen.parent / (frozen.name + ".baseline_repo")
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        shutil.copytree(
            pristine_repo,
            scratch,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )
    except OSError:
        return 0
    passed, total, _ran, _tail, _failed = _run_pytest(scratch, frozen, timeout=timeout)
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        cache.write_text(
            json.dumps({"vacuous": passed, "collected": total}), encoding="utf-8"
        )
    except OSError:
        pass
    return passed


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
    parser.add_argument(
        "--spec-tests",
        default="",
        help="runner-owned directory holding this milestone's frozen authored suite",
    )
    parser.add_argument(
        "--take-custody",
        action="store_true",
        help="only move the authored suite out of the workspace, then exit 0",
    )
    parser.add_argument("--timeout", type=int, default=900)
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
    spec_dir_rel = str(manifest.get("spec_tests_dir") or "spec_tests")

    if args.take_custody:
        if not args.spec_tests:
            print("FAIL: --take-custody requires --spec-tests", file=sys.stderr)
            return 2
        if _freeze_spec_suite(root, spec_dir_rel, args.spec_tests):
            kept = len(
                [p for p in Path(args.spec_tests).rglob("*.py") if p.name != "conftest.py"]
            )
            print(
                "CUSTODY "
                + spec_dir_rel
                + " -> "
                + str(args.spec_tests)
                + " ("
                + str(kept)
                + " file(s)); the workspace copy is gone"
            )
        else:
            print("NOTE no authored suite at " + spec_dir_rel + "; behaviour ungraded")
        return 0

    spec_frozen = (
        Path(args.spec_tests)
        if args.spec_tests and _freeze_spec_suite(root, spec_dir_rel, args.spec_tests)
        else None
    )
    if args.spec_tests and spec_frozen is None:
        print("NOTE no authored suite at " + spec_dir_rel + "; behaviour ungraded")
    progress = Progress(args.level, spec_tests=spec_frozen is not None)
    graded_spec = [False]

    def finish(code):
        """Grade the authored suite, then emit. Called on every exit path.

        Deliberately runs even when an earlier stage already failed: a design
        that got two contracts wrong but implemented most of the documented
        behaviour has to score above one that implemented none.
        """
        if spec_frozen is not None and not graded_spec[0] and progress.spec_tests:
            graded_spec[0] = True
            vacuous = _vacuous_count(
                spec_frozen, manifest.get("pristine_repo") or "", args.timeout
            )
            passed, collected, ran, _tail, failed = _run_pytest(
                root, spec_frozen, timeout=args.timeout
            )
            total = _pinned_case_total(spec_frozen, collected)
            gradable = total - vacuous
            if gradable <= 0:
                print(
                    "SKIP spec_tests: nothing gradable "
                    + "(collected "
                    + str(collected)
                    + ", vacuous "
                    + str(vacuous)
                    + ")"
                )
                progress.drop_spec_stage()
            else:
                progress.record(
                    "spec_tests", max(0, passed - vacuous), gradable, failed
                )
                # Counts only. This stdout becomes the failure feedback attached
                # to the next candidate's prompt, so printing the pytest tail
                # would hand an implementer the assertions of a suite it is
                # deliberately not allowed to read.
                uncollected = max(0, total - ran)
                print(
                    "SPEC spec_tests "
                    + str(max(0, passed - vacuous))
                    + "/"
                    + str(gradable)
                    + " (vacuous "
                    + str(vacuous)
                    + " excluded"
                    + ("; " + str(uncollected) + " not collected" if uncollected else "")
                    + ")"
                )
        progress.emit()
        return code

    targets = [root / p for p in packages if (root / p).exists()]
    targets += [root / f"{p}.py" for p in packages if (root / f"{p}.py").is_file()]
    if not targets:
        targets = [root]
    compiled = 0
    for target in targets:
        if target.is_dir():
            ok = compileall.compile_dir(str(target), quiet=1)
        else:
            ok = compileall.compile_file(str(target), quiet=1)
        if ok:
            compiled += 1
        else:
            print(f"FAIL: compileall failed under {target}", file=sys.stderr)
    progress.record("compile", compiled, len(targets))
    if compiled != len(targets):
        return finish(1)
    print(f"OK compileall level={args.level}")

    sys.path.insert(0, str(root))
    skipped_deps: set[str] = set()

    failed_contracts: list[str] = []
    attempted_contracts = 0
    contracts = _load_json(Path(args.contracts) if args.contracts else None)
    if contracts:
        for check in list(contracts.get("checks") or []):
            levels = check.get("required_levels") or ["integration"]
            if args.level not in levels:
                continue
            ctype = check.get("type")
            attempted_contracts += 1
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
            progress.record(
                "contracts",
                max(0, attempted_contracts - len(failed_contracts)),
                attempted_contracts or 1,
            )
            return finish(1)
        print(f"OK milestone contracts level={args.level}")

    if args.level == "discovery":
        return finish(0)

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
    attempted = max(0, len(modules) - len(unverified))
    progress.record("imports", max(0, attempted - len(failed_imports)), attempted)
    if failed_imports:
        print("FAIL imports:", file=sys.stderr)
        for item in failed_imports[:40]:
            print(f"  - {item}", file=sys.stderr)
        return finish(1)
    print(f"OK imports count={len(modules) - len(unverified)}")
    if skipped_deps:
        print(
            "SKIP unavailable dependencies: "
            + ", ".join(sorted(skipped_deps))
            + f" (unverified modules: {len(unverified)})"
        )

    if args.level == "implementation":
        progress.record(
            "contracts",
            max(0, attempted_contracts - len(failed_contracts))
            if attempted_contracts
            else 1,
            attempted_contracts or 1,
        )
        return finish(0)

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
                missing.append(" or ".join(f"{mod}.{sym}" for mod, _ in loaded))
    uml_attempted = sum(len(wanted) for wanted in exports.values())
    attempted_contracts += uml_attempted
    failed_contracts.extend(missing)
    if missing:
        print("FAIL missing UML exports:", file=sys.stderr)
        for item in missing[:40]:
            print(f"  - {item}", file=sys.stderr)
        progress.record(
            "contracts",
            max(0, attempted_contracts - len(failed_contracts)),
            attempted_contracts or 1,
        )
        return finish(1)
    print(f"OK export checks missing=0 considered_modules={len(modules)}")
    progress.record(
        "contracts",
        max(0, attempted_contracts - len(failed_contracts))
        if attempted_contracts
        else 1,
        attempted_contracts or 1,
    )
    return finish(0)


if __name__ == "__main__":
    raise SystemExit(main())
