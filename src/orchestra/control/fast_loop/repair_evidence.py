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


#: most cases staged in one evidence directory; the rest are named in the README as not staged, never dropped silently
MAX_EVIDENCE_CASES = 60
MANIFEST = "evidence.json"


def build_repair_evidence(
    repo: Path,
    failures: list[str],
    *,
    env_python: str | None = None,
    timeout: int = 900,
    max_cases: int = MAX_EVIDENCE_CASES,
) -> Path | None:
    """Stage the persistent tests and their current output beside ``repo``.

    Returns the evidence directory, or None when nothing could be staged.
    Never raises: a repair that runs without evidence is the old behaviour,
    not a failure -- the delivery check (``verify_delivery``) is what refuses it.
    """
    try:
        return _build(Path(repo), [str(f) for f in failures], env_python=env_python, timeout=timeout,
                      max_cases=max_cases)
    except Exception as exc:  # noqa: BLE001 -- evidence is a help, never a gate
        logger.warning("repair evidence not staged: %s", exc)
        return None


def _suite_dirname(frozen_root: Path) -> str:
    return frozen_root.name[: -len(".spec_tests")] if frozen_root.name.endswith(".spec_tests") else frozen_root.name


def _junit_counts(xml: Path) -> dict[str, int] | None:
    """passed / failed / errors / skipped from the evidence run's junit report (the structured result)."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.parse(xml).getroot()
    except (OSError, ET.ParseError):
        return None
    out = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for case in root.iter("testcase"):
        if case.find("skipped") is not None:
            out["skipped"] += 1
        elif case.find("error") is not None and case.find("failure") is None:
            out["errors"] += 1
        elif case.find("failure") is not None:
            out["failed"] += 1
        else:
            out["passed"] += 1
    return out


def _build(repo: Path, failures: list[str], *, env_python: str | None, timeout: int, max_cases: int) -> Path | None:
    resolved: list[tuple[Path, str, str]] = []
    unresolved: list[str] = []
    for f in failures:
        r = resolve_failure(repo, f)
        (resolved.append(r) if r is not None else unresolved.append(f))
    if not resolved:
        return None
    # every frozen suite the failures come from (2026-10-15: only the first one's were staged, the rest dropped
    # silently -- 22 -> 10, 16 -> 11, 5 -> 2 in the sandbox baseline), in the order the failures name them
    order: list[Path] = []
    by_suite: dict[Path, dict[str, set[str]]] = {}
    seen: set[tuple[Path, str, str]] = set()
    for root, rel, tid in resolved:
        if (root, rel, tid) in seen:
            continue
        seen.add((root, rel, tid))
        if root not in by_suite:
            order.append(root)
            by_suite[root] = defaultdict(set)
        by_suite[root][rel].add(tid)
    requested = sum(len(t) for files in by_suite.values() for t in files.values())
    # the cap: keep the first ``max_cases`` in suite order, say what was left out
    kept: dict[Path, dict[str, set[str]]] = {}
    left_out: list[str] = []
    budget = max_cases
    for root in order:
        for rel in sorted(by_suite[root]):
            for tid in sorted(by_suite[root][rel]):
                if budget > 0:
                    kept.setdefault(root, defaultdict(set))[rel].add(tid)
                    budget -= 1
                else:
                    left_out.append(f"{_suite_dirname(root)}/{rel}::{tid}")
    evidence = repo.parent / EVIDENCE_DIRNAME
    shutil.rmtree(evidence, ignore_errors=True)
    python = env_python or _env_python(order[0])
    suites_info: list[dict] = []
    sections: list[str] = []
    commands: list[str] = []
    all_ids: list[str] = []
    for root in order:
        files = kept.get(root)
        if not files:
            continue
        # one holder per suite, the suite inside it as ``spec_tests/``: authored suites import their helpers as
        # ``spec_tests.<module>``, so each keeps its own package root and runs with its own holder on PYTHONPATH
        holder = evidence / _suite_dirname(root)
        suite = holder / "spec_tests"
        suite.mkdir(parents=True)
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(root))
            target = suite / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.name.startswith("test") and path.suffix == ".py":
                if rel not in files:
                    continue
                try:
                    target.write_text(strip_to_tests(path.read_text(encoding="utf-8"), files[rel]), encoding="utf-8")
                except SyntaxError:
                    shutil.copy2(path, target)
            else:
                shutil.copy2(path, target)
        node_ids = [f"{suite / rel}::{tid}" for rel, tids in sorted(files.items()) for tid in sorted(tids)]
        all_ids += node_ids
        output, counts, inner = _run_suite(repo, holder, node_ids, python, timeout)
        commands.append(f"PYTHONPATH={holder}{os.pathsep}{repo} {python} " + " ".join(inner[1:]))
        sections.append(f"## {_suite_dirname(root)} ({len(node_ids)} case(s))\n\n```\n{output[-_OUTPUT_CAP:]}\n```\n")
        suites_info.append({"suite": _suite_dirname(root), "frozen": str(root), "staged": len(node_ids), "counts": counts})
    staged = len(all_ids)
    manifest = {
        "requested": len(failures), "resolved": requested, "staged": staged,
        "truncated": len(left_out), "not_staged": left_out, "unresolved": unresolved, "suites": suites_info,
    }
    (evidence / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    listed = "\n".join(f"- `{n}`" for n in all_ids)
    gaps = ""
    if left_out:
        gaps += (f"\n**Truncated:** {len(left_out)} of {requested} persistent failures are not staged here (cap "
                 f"{max_cases}); they are listed in `{MANIFEST}` under `not_staged` and still count.\n")
    if unresolved:
        gaps += (f"\n**Not found:** {len(unresolved)} failure id(s) could not be traced to a frozen suite; listed in "
                 f"`{MANIFEST}` under `unresolved`.\n")
    run_lines = "\n".join(f"    {c}" for c in commands)
    (evidence / "README.md").write_text(
        "# Repair evidence\n\n"
        "These tests failed in every independent attempt at this milestone; the\n"
        "behaviours they check are what this continuation exists to fix. They are\n"
        "copies of the sealed suites, cut down to the persistent failures and the\n"
        "helpers they use, one directory per suite. Grading reads the sealed copy,\n"
        "so editing these files changes nothing; run them, do not rewrite them.\n\n"
        f"Persistent failures ({staged} staged of {requested}):\n{listed}\n{gaps}\n"
        "Run them from the repository root, before and after each change (one command per suite):\n\n"
        f"{run_lines}\n\n"
        "Their output on the code as you\n"
        "received it is in `failures.md`. A fix is done when these pass and nothing\n"
        "else you can run has regressed.\n",
        encoding="utf-8",
    )
    (evidence / "failures.md").write_text("# Output on the incumbent's code\n\n" + "\n".join(sections), encoding="utf-8")
    _read_only(evidence)
    return evidence


def _run_suite(repo: Path, holder: Path, node_ids: list[str], python: str,
               timeout: int) -> tuple[str, dict[str, int] | None, list[str]]:
    """One suite's evidence run: (output, junit counts or None, the command as the repairer runs it)."""
    import tempfile

    out_dir = Path(tempfile.mkdtemp(prefix="adamas-evidence-"))
    xml = out_dir / "report.xml"
    base = [
        python, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
        "-o", "addopts=", "--continue-on-collection-errors", "-rfE", "--tb=short",
        # rooted at the suite's own holder: pytest otherwise roots at the common ancestor of the repository and the
        # evidence and lists it, which the sandbox refuses ("ERROR collecting ." in every evidence run since 10-11)
        f"--rootdir={holder}", f"--confcutdir={holder}",
        *_timeout_flags(python),
    ]
    inner = [*base, *node_ids]
    command = [*base, "-o", "junit_family=xunit1", f"--junitxml={xml}", *node_ids]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in [str(holder), str(repo), env.get("PYTHONPATH", "")] if p)
    from orchestra.sandbox.policy import confine_command

    command, env, private = confine_command(command, cwd=repo, env=env, extra_write=[str(out_dir)])
    try:
        proc = subprocess.run(command, cwd=str(repo), env=env, capture_output=True, text=True, timeout=timeout, check=False)
        output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    except subprocess.TimeoutExpired:
        output = f"(the evidence run did not finish inside {timeout}s)"
    counts = _junit_counts(xml)
    shutil.rmtree(out_dir, ignore_errors=True)
    if private is not None:
        shutil.rmtree(private, ignore_errors=True)
    return output, counts, inner


def verify_delivery(repo: Path, evidence: Path | None, *, expected: int | None = None) -> dict:
    """The delivery check for one repairer's evidence (2026-10-15): it exists, is not empty, and can be read.

    ``ok`` only when the directory exists; it stages at least one test and its manifest; every staged case was
    collected by the evidence run (junit counts add up -- "found no collectors" fails this); and a process confined
    under the repairer's own sandbox policy (``agent_policy`` for ``repo``) can list and read every file in it.
    ``expected`` (the persistent cases handed over) must equal staged + truncated + unresolved, and no handed-over
    case may be unresolved (truncation is allowed only because the README and manifest state it).
    """
    problems: list[str] = []
    out: dict = {"evidence": str(evidence or ""), "ok": False, "problems": problems}
    if evidence is None or not Path(evidence).is_dir():
        problems.append("evidence directory missing")
        return out
    evidence = Path(evidence)
    try:
        manifest = json.loads((evidence / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        problems.append("evidence manifest missing or unreadable")
        return out
    tests = [p for p in evidence.rglob("spec_tests/**/test*.py") if "__pycache__" not in p.parts]
    if not tests or not manifest.get("staged"):
        problems.append("evidence directory stages no test")
    for s in manifest.get("suites") or []:
        c = s.get("counts")
        if c is None:
            problems.append(f"{s.get('suite')}: the evidence run left no structured result")
        elif c["passed"] + c["failed"] + c["errors"] + c["skipped"] < int(s.get("staged") or 0) or c["errors"] and not (c["passed"] or c["failed"]):
            problems.append(f"{s.get('suite')}: the evidence run collected {c} of {s.get('staged')} staged case(s)")
    if manifest.get("unresolved"):
        # a failure the evidence cannot trace to its frozen suite is a failure the repairer never sees
        problems.append(f"{len(manifest['unresolved'])} handed-over failure(s) not found in any frozen suite")
    if expected is not None:
        accounted = int(manifest.get("staged") or 0) + int(manifest.get("truncated") or 0) + len(manifest.get("unresolved") or [])
        if accounted < expected:
            problems.append(f"{expected} case(s) handed over, {accounted} accounted for in the evidence")
    readable = _readable_by_repairer(repo, evidence)
    out["readable"] = readable
    if readable is not True:
        problems.append(f"the repairer's sandbox cannot read the evidence: {readable}")
    out.update({"staged": manifest.get("staged"), "truncated": manifest.get("truncated"),
                "unresolved": len(manifest.get("unresolved") or [])})
    out["ok"] = not problems
    return out


_READ_PROBE = (
    "import os,sys\n"
    "n=0\n"
    "for d,_,fs in os.walk(sys.argv[1]):\n"
    "    for f in fs:\n"
    "        open(os.path.join(d,f),'rb').read(); n+=1\n"
    "print(n)\n"
    "sys.exit(0 if n else 3)\n"
)


def _readable_by_repairer(repo: Path, evidence: Path) -> bool | str:
    """True when a process under the repairer's sandbox policy reads every evidence file; else why not."""
    import tempfile

    from orchestra.sandbox import policy as SB

    if not SB.enforced():
        return True if os.access(evidence, os.R_OK) else "not readable"
    private = Path(tempfile.mkdtemp(prefix="adamas-evidence-probe-"))
    try:
        pol = SB.agent_policy(workspace=repo, private=private, venv=None)
        pol_path = SB.write_policy(pol, private / "policy.json")
        env = SB.launcher_env(SB.private_env(private))
        proc = subprocess.run([*SB.launcher(pol_path), "/usr/bin/python3", "-I", "-c", _READ_PROBE, str(evidence)],
                              env=env, capture_output=True, text=True, timeout=120, check=False)
        if proc.returncode == 0:
            return True
        return f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-200:]}"
    except Exception as exc:  # noqa: BLE001
        return f"probe failed: {exc}"
    finally:
        shutil.rmtree(private, ignore_errors=True)


DELIVERY_LOG = "repair_evidence_delivery.jsonl"


def record_evidence_delivery(run_dir: Path, candidate_id: str, check: dict) -> None:
    """One line per evidence hand-over in ``<run_dir>/repair_evidence_delivery.jsonl``; the delivery audit reads it."""
    try:
        with (Path(run_dir) / DELIVERY_LOG).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"candidate_id": candidate_id, **check}) + "\n")
    except OSError as exc:
        logger.warning("evidence delivery not recorded: %s", exc)


def relocate_evidence(dst: Path, *, src: Path, repo: Path) -> None:
    """An evidence directory copied beside another repository: its README and manifest name the new paths.

    The node resample builds evidence in a scratch workspace and copies it beside each sample; the copy's README
    still pointed at the scratch paths, which the sample's sandbox does not grant.
    """
    swaps = [(str(src), str(dst)), (str(src.parent / "repo"), str(repo))]
    for name in ("README.md", "failures.md", MANIFEST):
        p = dst / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        for old, new in swaps:
            text = text.replace(old, new)
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        p.write_text(text, encoding="utf-8")
    _read_only(dst)
