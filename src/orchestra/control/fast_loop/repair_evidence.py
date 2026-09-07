"""Put the yardstick in the repairer's hand -- and only the part it needs.

Custody deletes the authored suite from every workspace on purpose: an
implementer that can read and run its exam satisfies it completely, and the
behaviour axis collapses (EXP-20260811-01). The rule is right for the first
attempt and wrong for the repair. A continuation is told "these seven tests
fail in every attempt; fix them" and given nothing else -- no assertion, no
traceback, no way to run them -- and across three tasks and two models it
fixed none (bplustree 0/7, imapclient 0/9, once regressing ten tests).

This module stages, beside the continuation's repository, a copy of exactly
the persistent tests (each frozen file cut down to those tests plus the
helpers and fixtures they share), the output those tests produce on the
incumbent's code, and a README with the exact command. The rest of the exam
stays sealed, grading still reads the frozen copy, and editing the copies
changes nothing -- so the first attempt's blindness is kept where it earns
its keep and lifted where it only costs.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

EVIDENCE_DIRNAME = "repair_evidence"
_OUTPUT_CAP = 24_000


def _frozen_root(path: Path) -> Path | None:
    """The `<milestone>.spec_tests` ancestor of a frozen test file, if any."""
    for parent in (path, *path.parents):
        if parent.name.endswith(".spec_tests") and parent.is_dir():
            return parent
    return None


def _locate(repo: Path, file_part: str) -> Path | None:
    """The frozen file a gate-relative node id names, seen from any repo depth.

    The gate printed the path relative to the base workspace's repository; a
    continuation runs in a candidate repository several levels deeper, so the
    leading ``..`` segments are dropped and the remainder tried against each
    ancestor -- the first hit under a ``*.spec_tests`` directory wins.
    """
    if os.path.isabs(file_part):
        path = Path(file_part)
        return path if path.is_file() else None
    direct = (repo / file_part).resolve()
    if direct.is_file() and _frozen_root(direct):
        return direct
    parts = [p for p in Path(file_part).parts if p not in ("..", ".")]
    for ancestor in (repo.resolve(), *repo.resolve().parents):
        trial = ancestor.joinpath(*parts)
        if trial.is_file() and _frozen_root(trial):
            return trial
    return None


def resolve_failure(repo: Path, failure: str) -> tuple[Path, str, str] | None:
    """(frozen_root, file relative to it, test id) for one failure record.

    Failure records are node ids as the gate printed them, with the file part
    relative to the repository the gate ran in.
    """
    text = str(failure).strip()
    if "::" not in text:
        return None
    file_part, test_id = text.split("::", 1)
    candidate = _locate(Path(repo), file_part)
    if candidate is None:
        return None
    root = _frozen_root(candidate)
    if root is None:
        return None
    return root, str(candidate.relative_to(root)), test_id


def _delete_ranges(node: ast.AST) -> tuple[int, int]:
    start = node.lineno
    for deco in getattr(node, "decorator_list", []) or []:
        start = min(start, deco.lineno)
    return start, int(getattr(node, "end_lineno", node.lineno))


def strip_to_tests(source: str, keep: set[str]) -> str:
    """Drop every test the persistent set does not name; keep everything else.

    ``keep`` holds test ids as pytest prints them after the file part:
    ``test_x``, ``test_x[param]``, ``TestClass::test_y``. Deletion is by line
    range so comments, citations and formatting of what remains are untouched.
    """
    tree = ast.parse(source)
    wanted_funcs = {k.split("[", 1)[0] for k in keep if "::" not in k}
    wanted_methods: dict[str, set[str]] = defaultdict(set)
    for k in keep:
        if "::" in k:
            cls, meth = k.split("::", 1)
            wanted_methods[cls].add(meth.split("[", 1)[0])
    drop: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test") and node.name not in wanted_funcs:
                drop.append(_delete_ranges(node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            kept_any = False
            inner: list[tuple[int, int]] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    if item.name in wanted_methods.get(node.name, set()):
                        kept_any = True
                    else:
                        inner.append(_delete_ranges(item))
            if kept_any:
                drop.extend(inner)
            else:
                drop.append(_delete_ranges(node))
    if not drop:
        return source
    lines = source.splitlines(keepends=True)
    dead = set()
    for start, end in drop:
        dead.update(range(start, end + 1))
    return "".join(line for i, line in enumerate(lines, 1) if i not in dead)


def _env_python(frozen_root: Path) -> str:
    manifest = frozen_root.parent / "adamas_cpe_harness.json"
    try:
        python = json.loads(manifest.read_text(encoding="utf-8")).get("env_python")
        if python and Path(python).is_file():
            return str(python)
    except (OSError, ValueError):
        pass
    return sys.executable


def _timeout_flags(python: str) -> list[str]:
    probe = subprocess.run(
        [python, "-c", "import pytest_timeout"], capture_output=True, check=False
    )
    return ["--timeout=120", "--timeout-method=thread"] if probe.returncode == 0 else []


def _read_only(root: Path) -> None:
    for path in root.rglob("*"):
        try:
            if path.is_file():
                path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass


def build_repair_evidence(
    repo: Path,
    failures: list[str],
    *,
    env_python: str | None = None,
    timeout: int = 900,
) -> Path | None:
    """Stage the persistent tests and their current output beside ``repo``.

    Returns the evidence directory, or None when nothing could be staged.
    Never raises: a repair that runs without evidence is the old behaviour,
    not a failure.
    """
    try:
        return _build(Path(repo), [str(f) for f in failures], env_python=env_python, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- evidence is a help, never a gate
        logger.warning("repair evidence not staged: %s", exc)
        return None


def _build(repo: Path, failures: list[str], *, env_python: str | None, timeout: int) -> Path | None:
    resolved = [r for r in (resolve_failure(repo, f) for f in failures) if r is not None]
    if not resolved:
        return None
    frozen_root = resolved[0][0]
    by_file: dict[str, set[str]] = defaultdict(set)
    for root, rel, test_id in resolved:
        if root == frozen_root:
            by_file[rel].add(test_id)
    evidence = repo.parent / EVIDENCE_DIRNAME
    shutil.rmtree(evidence, ignore_errors=True)
    suite = evidence / "spec_tests"
    suite.mkdir(parents=True)
    for path in frozen_root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(frozen_root))
        target = suite / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.name.startswith("test") and path.suffix == ".py":
            if rel not in by_file:
                continue
            try:
                target.write_text(
                    strip_to_tests(path.read_text(encoding="utf-8"), by_file[rel]), encoding="utf-8"
                )
            except SyntaxError:
                shutil.copy2(path, target)
        else:
            shutil.copy2(path, target)
    node_ids = [f"{suite / rel}::{tid}" for rel, tids in by_file.items() for tid in sorted(tids)]
    python = env_python or _env_python(frozen_root)
    command = [
        python, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
        "-o", "addopts=", "--continue-on-collection-errors", "-rfE", "--tb=short",
        *_timeout_flags(python), *node_ids,
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [str(evidence), str(repo), env.get("PYTHONPATH", "")] if p
    )
    try:
        proc = subprocess.run(
            command, cwd=str(repo), env=env, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    except subprocess.TimeoutExpired:
        output = f"(the evidence run did not finish inside {timeout}s)"
    shown = " ".join(command[1:])
    listed = "\n".join(f"- `{n}`" for n in node_ids)
    (evidence / "README.md").write_text(
        "# Repair evidence\n\n"
        "These tests failed in every independent attempt at this milestone; the\n"
        "behaviours they check are what this continuation exists to fix. They are\n"
        "copies of the sealed suite, cut down to the persistent failures and the\n"
        "helpers they use. Grading reads the sealed copy, so editing these files\n"
        "changes nothing; run them, do not rewrite them.\n\n"
        f"Persistent failures ({len(node_ids)}):\n{listed}\n\n"
        "Run them from the repository root, before and after each change:\n\n"
        f"    {python} {shown}\n\n"
        f"with `PYTHONPATH={evidence}:{repo}`. Their output on the code as you\n"
        "received it is in `failures.md`. A fix is done when these pass and nothing\n"
        "else you can run has regressed.\n",
        encoding="utf-8",
    )
    (evidence / "failures.md").write_text(
        "# Output on the incumbent's code\n\n```\n" + output[-_OUTPUT_CAP:] + "\n```\n",
        encoding="utf-8",
    )
    _read_only(evidence)
    return evidence
