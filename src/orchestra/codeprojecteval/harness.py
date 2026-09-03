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

from orchestra.codeprojecteval.dataset import CpeTask, build_agent_workspace
from orchestra.realbench.public_harness import parse_expected_modules

CHECK_SCRIPT_NAME = "adamas_cpe_check.py"
CHECK_MANIFEST_NAME = "adamas_cpe_harness.json"
CONTRACTS_SUFFIX = ".contracts.json"
SPEC_TESTS_SUFFIX = ".spec_tests"
# Where a ``test_author`` writes inside the workspace. Deliberately not the
# dataset's own ``check_tests``, which is digest-guarded evidence.
SPEC_TESTS_DIRNAME = "spec_tests"


@dataclass(frozen=True)
class CpeHarnessManifest:
    levels: dict[str, str]
    expected_modules: list[str]
    top_level_packages: list[str]
    check_tests_dir: str
    source_dir: str
    check_tests_digest: dict[str, str] = field(default_factory=dict)
    # Where a test_author writes, and the repository as it shipped. The second is
    # what makes a vacuous authored test detectable: anything passing against it
    # passes without an implementation.
    spec_tests_dir: str = "spec_tests"
    pristine_repo: str = ""
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
import ast
import compileall
import hashlib
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


# How far through the level's stages a milestone got, as a number a tuning loop
# can climb. A bare pass/fail makes every failing attempt look identical: an
# implementation that compiled and imported everything but failed two tests
# would score exactly what an empty repository scores.
STAGE_WEIGHTS = {
    "discovery": {"compile": 1.0},
    "implementation": {
        "compile": 0.25,
        "imports": 0.3,
        "cross_imports": 0.15,
        "contracts": 0.3,
    },
    "integration": {
        "compile": 0.1,
        "imports": 0.2,
        "cross_imports": 0.15,
        "contracts": 0.15,
        "tests": 0.4,
    },
}

# Used instead of the above when a frozen specification suite is available to
# grade. Kept as a separate table rather than renormalising the one above: the
# structural stages saturate, so a level that can measure behaviour has to give
# behaviour most of the weight, and a milestone with no authored suite must keep
# scoring exactly as it did before this existed.
SPEC_STAGE_WEIGHTS = {
    "implementation": {
        "compile": 0.15,
        "imports": 0.2,
        "cross_imports": 0.1,
        "contracts": 0.15,
        "spec_tests": 0.4,
    },
    "integration": {
        "compile": 0.1,
        "imports": 0.15,
        "cross_imports": 0.1,
        "contracts": 0.15,
        "tests": 0.25,
        "spec_tests": 0.25,
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
        # Which tests failed, not just how many. A tuning loop comparing designs
        # against one frozen suite needs to know which tests *moved*: a test every
        # candidate fails and a test every candidate passes both carry a constant,
        # and a constant cannot rank anything while still consuming the score's
        # range. Only behavioural stages report this; the structural stages have no
        # per-test identity to report.
        if failed_tests:
            entry["failed_tests"] = sorted({str(t) for t in failed_tests})
        self.stages.append(entry)

    def score(self):
        """Weighted progress in [0, 1]; 1.0 only when every stage fully passed.

        Stages the run never reached contribute nothing, so stopping early at a
        cheap stage scores below getting through it and failing a later one.
        """
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
    """(passed, total, ran) from pytest's summary line, or zeros if unreadable.

    ``ran`` excludes errors because an error is usually a module that could not
    be imported, which stands for however many cases it holds rather than one.

    Parsing text is unpleasant, but the alternative is requiring a report plugin
    inside every one of the dataset's virtual environments.
    """
    passed = failed = errors = 0
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if " passed" not in line and " failed" not in line and " error" not in line:
            continue
        for count, word in re.findall(r"(\\d+)\\s+(passed|failed|errors?|xfailed)", line):
            if word == "passed":
                passed = int(count)
            elif word == "failed":
                failed = int(count)
            elif word.startswith("error"):
                errors = int(count)
        if passed or failed or errors:
            return passed, passed + failed + errors, passed + failed
    return 0, 0, 0


def _module_constants(root, packages):
    """Module-level ALL_CAPS assignments per module, by AST.

    Reported, not scored: the gate cannot know which constants a held-out
    consumer will want, but a milestone that froze a substrate and published
    no constants at all is a shape worth seeing in the log -- on bplustree the
    contract asked for "TreeConf and core constants", pinned only the class,
    and the missing ENDIAN cost 337 of 356 held-out cases (EXP-20260903-02).
    """
    import ast as _ast

    surface = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in {"check_tests", "unit_tests", "docs", "__pycache__", ".git"}
               for part in rel.parts) or rel.parts[0] not in packages:
            continue
        try:
            tree = _ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            continue
        names = [
            t.id
            for node in tree.body
            if isinstance(node, _ast.Assign)
            for t in node.targets
            if isinstance(t, _ast.Name) and t.id.isupper()
        ]
        if names:
            surface[".".join(rel.with_suffix("").parts)] = sorted(names)
    return surface


def _internal_from_imports(root, packages):
    """Every ``from <own package> import name`` in the repository, by AST.

    Static on purpose: a module that imports a name its own package never
    defines is a defect whether or not any test happens to exercise the line,
    and one such name can take a whole test module out at collection time --
    on bplustree a missing ``ENDIAN`` turned 356 held-out cases into 19
    (EXP-20260903-02). Reads the source rather than importing, so a package
    whose import already failed still reports its dangling names.
    """
    import ast as _ast

    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in {"check_tests", "unit_tests", "docs", "__pycache__", ".git"}
               for part in rel.parts):
            continue
        try:
            tree = _ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            continue  # compile stage owns this failure
        here = ".".join(rel.with_suffix("").parts)
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.ImportFrom):
                continue
            module = node.module or ""
            if node.level:  # relative import -> resolve against this module
                base = here.rsplit(".", node.level)[0] if node.level <= here.count(".") + 1 else ""
                module = f"{base}.{module}".strip(".") if base or module else ""
            if not module or module.split(".")[0] not in packages:
                continue
            for alias in node.names:
                if alias.name != "*":
                    found.append((str(rel), module, alias.name))
    return found


def _check_cross_imports(root, packages):
    """(checked, dangling) for internal ``from X import Y`` pairs."""
    import importlib as _il

    pairs = _internal_from_imports(root, packages)
    dangling = []
    cache = {}
    for rel, module, name in pairs:
        if module not in cache:
            try:
                cache[module] = _il.import_module(module)
            except Exception as exc:  # noqa: BLE001
                cache[module] = exc
        target = cache[module]
        if isinstance(target, Exception):
            continue  # the imports stage already scored this module
        if not hasattr(target, name):
            dangling.append(rel + ": from " + module + " import " + name)
    return len(pairs), dangling


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


def _has_tests(directory):
    path = Path(directory)
    if not path.is_dir():
        return False
    return any(
        p.is_file() and "__pycache__" not in p.parts for p in path.rglob("test_*.py")
    )


def _failed_test_ids(output):
    """Node ids pytest reported as failed or errored, from its short summary.

    ``-rfE`` prints one line per failure as ``FAILED path::Class::test - reason``
    and one per collection error as ``ERROR path``. A collection error has no node
    id, so the file stands in for it: what matters is that the same unrunnable file
    is identified the same way for every candidate.
    """
    ids = set()
    for line in (output or "").splitlines():
        line = line.strip()
        for prefix in ("FAILED ", "ERROR "):
            if not line.startswith(prefix):
                continue
            rest = line[len(prefix):].strip()
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
        # Repositories bolt coverage thresholds, mypy and pycodestyle onto
        # pytest; those judge style, not whether the milestone works.
        "-o",
        "addopts=",
        # Without this pytest aborts the whole session on the first module it
        # cannot import, which is the normal state of a half-built milestone: one
        # unimportable file would report zero for every other file's tests and
        # flatten the gradient this stage exists to provide.
        "--continue-on-collection-errors",
        # Name the failures. Also improves the feedback tail, since the short
        # summary lands at the end of the output where the tail is taken from.
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
    """Move the authored suite out of the workspace, once, and keep it out.

    The copy lives beside the manifest, outside the repository, for two
    reasons. The first was tamper protection: every candidate in a fast-loop
    search grades against the suite the first attempt authored, and a candidate
    that rewrote the tests would be marking its own exam.

    The second is why the workspace copy is now deleted rather than restored. A
    suite the implementer can read and run is a suite it satisfies completely --
    the first two tasks to run this way scored 38/38, 29/29 and 51/51 -- which
    leaves the behavioural axis exactly as degenerate as the gate it was built
    to replace (EXP-20260811-01). Grading reads the frozen copy, so taking the
    suite away costs the score nothing. Custody normally happens between the
    author and the implementer; doing it here as well means a graph wired
    without that step still cannot leave the suite lying around.
    """
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
            # Another invocation won the race; its copy is the yardstick.
            shutil.rmtree(staging, ignore_errors=True)
        if not frozen.is_dir():
            return False
    if source.exists() and source.resolve() != frozen.resolve():
        shutil.rmtree(source, ignore_errors=True)
    return True


def _vacuous_count(frozen_dir, pristine_repo, timeout):
    """How many authored tests pass with no implementation present.

    A test that passes against the repository as it shipped measures nothing
    about the milestone, and a suite of them would hand every design a free
    1.0. The count is cached: it depends only on the frozen suite.
    """
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


def _looks_like_test_class(node):
    """Classes pytest collects: ``Test*`` by default, plus unittest subclasses."""
    if node.name.startswith("Test"):
        return True
    return any("TestCase" in ast.dump(base) for base in node.bases)


def _authored_case_count(frozen_dir):
    """Cases the author wrote, counted from source without importing anything.

    pytest reports a module it cannot import as a single error rather than as
    the cases it holds, so the collected total shrinks exactly when the code
    under test is broken -- dividing the worst work by the smallest
    denominator, and paying a candidate for breaking an import. Counting the
    source keeps the denominator a property of the suite.
    """
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
    """The suite's size, held fixed across every run that grades against it.

    Source counting cannot see cases a decorator multiplies, so a collection
    that found more than the source suggests raises the pin and is remembered
    beside the suite. The pin never falls: a later run that collects fewer has
    lost tests, and lost tests are failures, not a smaller exam.
    """
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
    args = parser.parse_args()

    root = Path.cwd()
    manifest = _load_json(args.manifest)
    if not manifest:
        print("FAIL: missing manifest " + str(args.manifest), file=sys.stderr)
        return 2
    modules = list(manifest.get("expected_modules") or [])
    packages = list(manifest.get("top_level_packages") or [])
    check_dir = str(manifest.get("check_tests_dir") or "check_tests")
    spec_dir_rel = str(manifest.get("spec_tests_dir") or "spec_tests")

    if args.take_custody:
        # Runs between the author and the implementer, and grades nothing: the
        # point is that the implementer starts from a workspace with no suite in
        # it. Always exits 0, because a milestone whose author produced nothing
        # must still be allowed to proceed -- ungraded, and saying so.
        if not args.spec_tests:
            print("FAIL: --take-custody requires --spec-tests", file=sys.stderr)
            return 2
        if _freeze_spec_suite(root, spec_dir_rel, args.spec_tests):
            kept = len(
                [p for p in Path(args.spec_tests).rglob("*.py") if p.name != "conftest.py"]
            )
            print(
                "CUSTODY " + spec_dir_rel + " -> " + str(args.spec_tests)
                + " (" + str(kept) + " file(s)); the workspace copy is gone"
            )
        else:
            print("NOTE no authored suite at " + spec_dir_rel + "; behaviour ungraded")
        return 0

    # Graded but never gating. The authored suite states behaviour the documents
    # only describe, so parts of it may be unsatisfiable or simply wrong; a
    # milestone that failed it must still be able to freeze and let the next one
    # proceed. It moves the score, not the exit code.
    spec_frozen = (
        Path(args.spec_tests)
        if args.spec_tests and _freeze_spec_suite(root, spec_dir_rel, args.spec_tests)
        else None
    )
    if args.spec_tests and spec_frozen is None:
        # Said out loud because the milestone still passes: a template that
        # authored a suite and arrives without one has no behavioural axis, and
        # the cause is upstream of the score.
        print("NOTE no authored suite at " + spec_dir_rel + "; behaviour ungraded")
    progress = Progress(args.level, spec_tests=spec_frozen is not None)
    graded_spec = [False]

    def finish(code):
        """Grade the authored suite, then emit. Called on every exit path.

        Deliberately runs even when an earlier stage already failed: a design
        that got two contracts wrong but implemented most of the documented
        behaviour has to score above one that implemented none, and that is the
        exact comparison the structural stages cannot make.
        """
        if spec_frozen is not None and not graded_spec[0] and progress.spec_tests:
            graded_spec[0] = True
            vacuous = _vacuous_count(
                spec_frozen, manifest.get("pristine_repo") or "", args.timeout
            )
            passed, collected, ran, tail, failed = _run_pytest(
                root, spec_frozen, timeout=args.timeout
            )
            total = _pinned_case_total(spec_frozen, collected)
            gradable = total - vacuous
            if gradable <= 0:
                print(
                    "SKIP spec_tests: nothing gradable "
                    + "(collected " + str(collected) + ", vacuous " + str(vacuous) + ")"
                )
                progress.drop_spec_stage()
            else:
                progress.record(
                    "spec_tests", max(0, passed - vacuous), gradable, failed
                )
                # Counts only. This stdout becomes the failure feedback attached
                # to the next candidate's prompt, so printing the pytest tail
                # would hand an implementer the assertions of a suite it is
                # deliberately not allowed to read. Failed test *ids* still
                # travel in the score marker, which the selector reads and no
                # prompt does.
                uncollected = max(0, total - ran)
                print(
                    "SPEC spec_tests "
                    + str(max(0, passed - vacuous)) + "/" + str(gradable)
                    + " (vacuous " + str(vacuous) + " excluded"
                    + ("; " + str(uncollected) + " not collected" if uncollected else "")
                    + ")"
                )
        progress.emit()
        return code

    targets = [root / p for p in packages if (root / p).exists()]
    targets += [root / (p + ".py") for p in packages if (root / (p + ".py")).is_file()]
    if not targets:
        print(
            "FAIL: none of the declared packages exist yet: " + ", ".join(packages),
            file=sys.stderr,
        )
        progress.record("compile", 0, max(1, len(packages)))
        return finish(1)
    compiled = 0
    for target in targets:
        ok = (
            compileall.compile_dir(str(target), quiet=1)
            if target.is_dir()
            else compileall.compile_file(str(target), quiet=1)
        )
        if ok:
            compiled += 1
        else:
            print("FAIL: compileall failed under " + str(target), file=sys.stderr)
    progress.record("compile", compiled, len(targets))
    if compiled != len(targets):
        return finish(1)
    print("OK compileall level=" + args.level)

    if args.level == "discovery":
        return finish(0)

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
    # Modules skipped for an absent third-party dependency are not the
    # repository's failure, so they leave the denominator rather than counting
    # against it.
    attempted = max(0, len(modules) - len(skipped_deps))
    progress.record("imports", max(0, attempted - len(failed_imports)), attempted)
    if failed_imports:
        print("FAIL imports:", file=sys.stderr)
        for item in failed_imports[:40]:
            print("  - " + item, file=sys.stderr)
        return finish(1)
    print("OK imports count=" + str(len(modules)))

    # A name a module imports from its own package but the package never
    # defines: importable in isolation, fatal at collection time for anything
    # downstream. Scored as its own stage so the gradient shows it rather than
    # letting a structurally complete repository read as sound.
    cross_total, dangling = _check_cross_imports(root, packages)
    # A repository whose modules never import from each other has nothing to
    # dangle and must not be penalised for it: zero of zero is a pass, not a
    # miss, and scoring it as a miss would silently cap every single-module
    # milestone below 1.0.
    progress.record(
        "cross_imports",
        max(0, cross_total - len(dangling)) if cross_total else 1,
        cross_total or 1,
    )
    if dangling:
        print("FAIL cross_imports:", file=sys.stderr)
        for item in dangling[:40]:
            print("  - " + item, file=sys.stderr)
        return finish(1)
    print("OK cross_imports count=" + str(cross_total))
    constants = _module_constants(root, packages)
    print("CONSTANT SURFACE " + json.dumps(constants, sort_keys=True)[:2000])

    contracts = _load_json(args.contracts) if args.contracts else {}
    failed_contracts = []
    attempted_contracts = 0
    for check in list(contracts.get("checks") or []):
        levels = check.get("required_levels") or ["integration"]
        if args.level not in levels:
            continue
        ctype = check.get("type")
        attempted_contracts += 1
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
    # A level with no contracts declared has nothing to fail, which is full
    # marks for that stage rather than a zero.
    progress.record(
        "contracts",
        max(0, attempted_contracts - len(failed_contracts)) if attempted_contracts else 1,
        attempted_contracts or 1,
    )
    if failed_contracts:
        print("FAIL milestone contracts:", file=sys.stderr)
        for item in failed_contracts[:40]:
            print("  - " + item, file=sys.stderr)
        return finish(1)
    if contracts:
        print("OK milestone contracts level=" + args.level)
    if skipped_deps:
        print("SKIP unavailable dependencies: " + ", ".join(sorted(skipped_deps)))

    if args.level != "integration":
        return finish(0)

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
        # Deliberately scored zero on tests rather than by ratio: an altered
        # suite is not partial progress, and a loop that could climb by editing
        # tests would learn to do exactly that.
        progress.record("tests", 0, 1)
        return finish(1)

    selection = [t for t in args.tests.split(",") if t.strip()] or [check_dir]
    if not any((root / t.split("::", 1)[0]).exists() for t in selection):
        print("FAIL: no visible tests found at " + ", ".join(selection), file=sys.stderr)
        progress.record("tests", 0, 1)
        return finish(1)
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
        # Name the failures, so this stage can serve as the behavioural axis on a
        # milestone that authored no suite of its own.
        "-rfE",
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
        progress.record("tests", 0, 1)
        return finish(1)
    tail = (proc.stdout or "")[-4000:]
    passed_tests, total_tests, _ran_tests = _pytest_counts(proc.stdout)
    progress.record(
        "tests", passed_tests, total_tests or 1, _failed_test_ids(proc.stdout)
    )
    if proc.returncode != 0:
        print("FAIL check_tests:", file=sys.stderr)
        print(tail, file=sys.stderr)
        print((proc.stderr or "")[-1500:], file=sys.stderr)
        return finish(1)
    print("OK check_tests")
    print(tail[-500:])
    return finish(0)


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
    # The baseline an authored test has to be measured against is the repository
    # as the agent received it — documents and the visible suite, no source.
    # Emphatically not ``task.repo_root``, which still holds the reference
    # implementation: every correct authored test would pass there and be written
    # off as vacuous.
    pristine = out / "pristine_repo"
    if not pristine.exists():
        build_agent_workspace(task, pristine)
    manifest = CpeHarnessManifest(
        levels={
            "discovery": "compileall",
            "implementation": "compileall+imports+contracts(+spec_tests, graded)",
            "integration": ("compileall+imports+contracts+check_tests(+spec_tests, graded)"),
        },
        expected_modules=modules,
        top_level_packages=packages,
        check_tests_dir=task.check_tests,
        source_dir=task.source_dir,
        check_tests_digest=_digest_check_tests(task),
        spec_tests_dir=SPEC_TESTS_DIRNAME,
        pristine_repo=str(pristine),
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
    spec_tests_path: Path | None = None,
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
    if spec_tests_path is not None:
        command += ["--spec-tests", str(spec_tests_path)]
    return command


def _safe_milestone(milestone_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", milestone_id) or "milestone"


def contracts_path_for(harness_dir: Path, milestone_id: str) -> Path:
    return Path(harness_dir) / f"{_safe_milestone(milestone_id)}{CONTRACTS_SUFFIX}"


def spec_tests_path_for(harness_dir: Path, milestone_id: str) -> Path:
    """Where this milestone's authored suite is frozen.

    Per milestone rather than per attempt: every fast-loop candidate is scored
    against the suite the first attempt wrote, which is the only way their
    quality scores mean the same thing.
    """
    return Path(harness_dir) / f"{_safe_milestone(milestone_id)}{SPEC_TESTS_SUFFIX}"


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
