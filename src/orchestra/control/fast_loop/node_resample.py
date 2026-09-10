"""Node-level best-of-N resampling from a frozen prefix (v1).

Design: docs/node_resample_design.md. This module holds the pure parts --
which nodes wrote what (from the cumulative per-node change artifacts), the
v1 blame rule (ownership of the file each persistent failure lands in),
the suffix graph with injected upstream artifacts, consensus selection over
per-test outcome vectors, and the explainability record. The controller
does the forking, running and grading.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestra.control.fast_loop.persistence import failure_key
from orchestra.control.fast_loop.repair_evidence import resolve_failure
from orchestra.ir.graph import OrchestraGraph

SPEC_DIR = "spec_tests"


# --------------------------------------------------------------------------
# writer steps: which agent node changed what, from cumulative snapshots
# --------------------------------------------------------------------------

@dataclass
class WriterStep:
    node_id: str
    artifact_id: str
    patch: str                      # cumulative diff against the milestone base
    files: set[str] = field(default_factory=set)   # files this node changed (vs previous step)
    is_author: bool = False         # every changed file lives under spec_tests/


def file_sections(patch: str) -> dict[str, str]:
    """Per-file diff text of a unified diff, keyed by the b/ path."""
    out: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for line in (patch or "").splitlines(keepends=True):
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            if cur is not None:
                out[cur] = "".join(buf)
            cur, buf = m.group(2), [line]
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "".join(buf)
    return out


def topological_order(graph: OrchestraGraph) -> list[str]:
    ids = [n.node_id for n in graph.nodes]
    incoming = {i: set() for i in ids}
    for e in graph.edges:
        if e.destination_node in incoming and e.source_node in incoming:
            incoming[e.destination_node].add(e.source_node)
    order: list[str] = []
    done: set[str] = set()
    while len(order) < len(ids):
        ready = [i for i in ids if i not in done and incoming[i] <= done]
        if not ready:  # cycle guard: fall back to declaration order
            ready = [i for i in ids if i not in done][:1]
        order.extend(ready)
        done.update(ready)
    return order


def writer_steps(graph: OrchestraGraph, changes: dict[str, tuple[str, str]]) -> list[WriterStep]:
    """``changes``: node_id -> (artifact_id, cumulative patch). Steps in graph order,
    keeping only nodes whose cumulative state differs from the previous one."""
    steps: list[WriterStep] = []
    prev_sections: dict[str, str] = {}
    for node_id in topological_order(graph):
        if node_id not in changes:
            continue
        artifact_id, patch = changes[node_id]
        sections = file_sections(patch)
        changed = {f for f, text in sections.items() if prev_sections.get(f) != text}
        changed |= {f for f in prev_sections if f not in sections}   # file reverted/removed
        changed = {f for f in changed if "__pycache__" not in f and not f.endswith(".pyc")}
        if not changed:
            continue
        steps.append(WriterStep(node_id=node_id, artifact_id=artifact_id, patch=patch, files=changed,
                                is_author=all(f.startswith(SPEC_DIR + "/") for f in changed)))
        prev_sections = sections
    return steps


# --------------------------------------------------------------------------
# traceback frames: which repository file each persistent failure lands in
# --------------------------------------------------------------------------

def traceback_frames(repo: Path, failures: list[str], python: str, timeout: int = 900) -> dict[str, str]:
    """failure id -> deepest repository-relative .py frame, by running the
    persistent tests once against ``repo`` (the incumbent's state)."""
    resolved = [(f, resolve_failure(repo, f)) for f in failures]
    resolved = [(f, r) for f, r in resolved if r is not None]
    if not resolved:
        return {}
    root = resolved[0][1][0]
    holder = Path(repo).parent / "blame_evidence"
    shutil.rmtree(holder, ignore_errors=True)
    (holder / SPEC_DIR).mkdir(parents=True)
    for p in root.rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            dst = holder / SPEC_DIR / p.relative_to(root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
    node_ids = [f"{holder / SPEC_DIR / rel}::{tid}" for _, (_, rel, tid) in resolved]
    env = {"PYTHONPATH": ":".join(p for p in [str(holder), str(repo), str(Path(repo) / "src") if (Path(repo) / "src").is_dir() else ""] if p),
           "PATH": f"{Path(python).parent}:/usr/bin:/bin", "HOME": str(repo)}
    try:
        proc = subprocess.run([python, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "-o", "addopts=",
                               "--tb=short", "-rN", *node_ids], cwd=str(repo), env=env, capture_output=True, text=True, timeout=timeout)
        out = proc.stdout
    except subprocess.TimeoutExpired:
        return {}
    frames: dict[str, str] = {}
    current: str | None = None
    repo_s = str(Path(repo).resolve())
    for line in out.splitlines():
        m = re.match(r"^_{3,} (.+?) _{3,}$", line)
        if m:
            current = m.group(1).strip()
            continue
        m = re.match(r"^(?:E\s+)?(?:File \")?([^\s\"]+\.py)\"?:(\d+):", line)
        if m and current:
            path = m.group(1)
            abs_p = str((Path(repo) / path).resolve()) if not path.startswith("/") else path
            if abs_p.startswith(repo_s) and SPEC_DIR not in abs_p and "blame_evidence" not in abs_p:
                rel = str(Path(abs_p).relative_to(repo_s))
                for f, (_, _, tid) in resolved:
                    if current.split("[")[0] == tid.split("[")[0] or tid in current:
                        frames[f] = rel  # last repo frame seen = deepest
    return frames


# --------------------------------------------------------------------------
# blame v1
# --------------------------------------------------------------------------

@dataclass
class Blame:
    node_id: str | None
    position: str          # "first" | "last" | "middle" | "author" | "none"
    reason: str
    owners: dict[str, str] = field(default_factory=dict)   # failure -> node


def blame_v1(persistent: list[str], frames: dict[str, str], steps: list[WriterStep],
             suite_collected_zero: bool = False) -> Blame:
    """Each persistent failure is owned by the last writer of the file it lands in;
    the earliest owner is blamed. Suite that collects nothing -> the author."""
    code_steps = [s for s in steps if not s.is_author]
    author = next((s.node_id for s in steps if s.is_author), None)
    if persistent and all("::" not in f for f in persistent):
        return Blame(author, "author", "suite collects nothing on the task interpreter (the failing unit is the file); re-author, do not resample")
    if suite_collected_zero:
        return Blame(author, "author", f"every gradable case fails in every sample ({len(persistent)} persistent, no sample above 0.0): suite-level cause; re-author, do not resample")
    if not code_steps:
        return Blame(None, "none", "no editing node produced a change")
    owners: dict[str, str] = {}
    for f in persistent:
        file = frames.get(f)
        owner = None
        if file:
            for s in code_steps:            # last writer wins
                if file in s.files:
                    owner = s.node_id
        owners[f] = owner or code_steps[-1].node_id   # unowned (missing feature) -> last writer
    if not owners:
        return Blame(None, "none", "no persistent failure to attribute")
    order = [s.node_id for s in code_steps]
    counts = Counter(owners.values())
    earliest = min(counts, key=lambda n: order.index(n))
    position = "first" if earliest == order[0] else ("last" if earliest == order[-1] else "middle")
    if earliest == order[0] == order[-1]:
        position = "first"
    reason = f"{counts[earliest]}/{len(owners)} persistent failures land in files last written by {earliest} ({position} editing node)"
    return Blame(earliest, position, reason, owners)


# --------------------------------------------------------------------------
# suffix graph: drop the ancestors of k, inject their outputs as initial slots
# --------------------------------------------------------------------------

def ancestors(graph: OrchestraGraph, node_id: str) -> set[str]:
    parents: dict[str, set[str]] = {}
    for e in graph.edges:
        parents.setdefault(e.destination_node, set()).add(e.source_node)
    seen: set[str] = set()
    stack = [node_id]
    while stack:
        n = stack.pop()
        for p in parents.get(n, ()):
            if p not in seen:
                seen.add(p)
                stack.append(p)
    return seen


def suffix_graph(graph: OrchestraGraph, node_id: str, node_outputs: dict[str, dict[str, str]]) -> tuple[OrchestraGraph, dict[str, str], list[str]]:
    """(graph without k's ancestors, injected {slot: artifact_id}, dropped node ids)."""
    dropped = ancestors(graph, node_id)
    injected: dict[str, str] = {}
    kept_nodes = [n for n in graph.nodes if n.node_id not in dropped]
    kept_ids = {n.node_id for n in kept_nodes}
    slot_types = {n.node_id: dict(n.input_slots) for n in graph.nodes}
    kept_edges = []
    initial_slots = dict(graph.initial_artifact_slots)
    for e in graph.edges:
        if e.source_node in dropped and e.destination_node in kept_ids:
            art = (node_outputs.get(e.source_node) or {}).get(e.source_output)
            if not art:
                raise ValueError(f"no recorded output {e.source_node}.{e.source_output} to inject into {e.destination_node}.{e.destination_input}")
            prev = injected.get(e.destination_input)
            if prev and prev != art:
                raise ValueError(f"slot name collision on injection: {e.destination_input}")
            injected[e.destination_input] = art
            initial_slots[e.destination_input] = slot_types[e.destination_node].get(e.destination_input, "")
        elif e.source_node in kept_ids and e.destination_node in kept_ids:
            kept_edges.append(e)
    new = graph.model_copy(update={"nodes": kept_nodes, "edges": kept_edges, "initial_artifact_slots": initial_slots,
                                   "graph_id": f"{graph.graph_id}__from_{node_id}"})
    return new, injected, sorted(dropped)


# --------------------------------------------------------------------------
# consensus selection
# --------------------------------------------------------------------------

@dataclass
class Sample:
    index: int
    gate_passed: bool
    harness_score: float | None
    behaviour_score: float | None
    failed: set[str]


def consensus_select(samples: list[Sample], epsilon: float = 0.02) -> tuple[Sample | None, float, set[str], str]:
    """Among gate-passing samples: highest behaviour score; within epsilon of
    the best, the one closest to the majority per-test outcome; then the
    smallest failing set. Returns (chosen, agreement, consensus_failed, note)."""
    ok = [s for s in samples if s.gate_passed and s.behaviour_score is not None]
    if not ok:
        return None, 0.0, set(), "no sample passed the gate"
    universe = set().union(*(s.failed for s in ok))
    consensus = {t for t in universe if sum(1 for s in ok if t in s.failed) * 2 > len(ok)}
    def dist(s: Sample) -> int:
        return len(s.failed ^ consensus)
    best = max(s.behaviour_score for s in ok)
    top = [s for s in ok if s.behaviour_score >= best - epsilon]
    chosen = min(top, key=lambda s: (dist(s), len(s.failed), s.index))
    agreement = 1.0 - (sum(dist(s) for s in ok) / (len(ok) * max(1, len(universe)))) if universe else 1.0
    note = f"{len(ok)}/{len(samples)} passed the gate; best {best:.3f}; chosen sample {chosen.index} at distance {dist(chosen)} from consensus; agreement {agreement:.2f}"
    return chosen, agreement, consensus, note


# --------------------------------------------------------------------------
# explainability record
# --------------------------------------------------------------------------

def write_record(batch_task_dir: Path, milestone: str, candidate_id: str, payload: dict[str, Any]) -> Path:
    out = Path(batch_task_dir) / "fast_loop" / "node_resample" / milestone
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{candidate_id}.json"
    payload = {"schema": "node_resample_record/v1", "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **payload}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    jsonl = Path(batch_task_dir).parent.parent / "node_resample_records.jsonl"
    try:
        with jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass
    return path


# --------------------------------------------------------------------------
# v2: symbol ownership, targeted feedback, short-circuit, re-author graph
# --------------------------------------------------------------------------

def repo_symbol_index(repo: Path) -> dict[str, str]:
    """top-level class/function name -> repository-relative file (first definition wins)."""
    index: dict[str, str] = {}
    for py in sorted(Path(repo).rglob("*.py")):
        parts = py.relative_to(repo).parts
        if any(x in parts for x in ("unit_tests", "check_tests", SPEC_DIR, "__pycache__", "blame_evidence", "repair_evidence", ".git")):
            continue
        try:
            tree = ast.parse(py.read_text(errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(node.name, str(py.relative_to(repo)))
    return index


def symbols_in_test(test_file: Path, test_name: str) -> set[str]:
    """Identifiers a test body refers to (names and attribute heads), e.g. GitWildMatchPattern."""
    try:
        tree = ast.parse(Path(test_file).read_text(errors="replace"))
    except (OSError, SyntaxError):
        return set()
    base = test_name.split("[", 1)[0].split("::")[-1]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == base:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    out.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    out.add(sub.attr)
    return out


def symbol_frames(repo: Path, failures: list[str]) -> dict[str, str]:
    """failure -> file that defines a symbol the test refers to (assertion-only failures
    have no repository frame; the symbol under test still names the owner)."""
    index = repo_symbol_index(repo)
    out: dict[str, str] = {}
    for f in failures:
        r = resolve_failure(Path(repo), f)
        if r is None:
            continue
        root, rel, tid = r
        names = symbols_in_test(root / rel, tid)
        hits = [index[n] for n in names if n in index]
        if hits:
            out[f] = Counter(hits).most_common(1)[0][0]
    return out


def targeted_feedback(persistent: list[str], evidence_relpath: str = "../repair_evidence") -> str:
    listed = "\n".join(f"- {failure_key(n).split('::', 1)[-1]}" for n in persistent)
    return (
        "The acceptance gate passed, but these behaviours fail in every independent attempt at this "
        f"milestone ({len(persistent)}); they are systematic, not flaky, and they are this run's target:\n{listed}\n"
        f"The tests for them, their output on the previous attempt, and the exact command to run them are beside "
        f"this repository in {evidence_relpath}/ (start with README.md). Run them before and after each change; "
        "they are copies of a sealed suite, so editing them changes nothing. Do not delete spec_tests, check_tests "
        "or documented public symbols."
    )


def should_stop(samples: list["Sample"], base_score: float | None) -> str | None:
    """Stop early when two gate-passing samples already agree exactly and neither beats the base."""
    ok = [s for s in samples if s.gate_passed and s.behaviour_score is not None]
    if len(ok) < 2:
        return None
    if all(s.failed == ok[0].failed for s in ok) and (base_score is None or max(s.behaviour_score for s in ok) <= base_score):
        return f"{len(ok)} samples identical (agreement 1.0) with no gain over {base_score}; stopping early"
    return None


def reauthor_graph(graph: OrchestraGraph, author_node: str, spec_dir: str, feedback: str) -> OrchestraGraph:
    """The full graph with the author told why the suite is condemned and every harness node
    pointed at a candidate-specific frozen-suite directory."""
    nodes = []
    for n in graph.nodes:
        if n.node_id == author_node:
            nodes.append(n.model_copy(update={"prompt_feedback": ((getattr(n, "prompt_feedback", "") or "") + "\n\n" + feedback).strip()}))
        elif getattr(n, "node_kind", "") == "harness" and getattr(n, "command", None):
            cmd = list(n.command)
            if "--spec-tests" in cmd:
                cmd[cmd.index("--spec-tests") + 1] = spec_dir
            nodes.append(n.model_copy(update={"command": cmd}))
        else:
            nodes.append(n)
    return graph.model_copy(update={"nodes": nodes, "graph_id": f"{graph.graph_id}__reauthor"})


def frozen_spec_dir(graph: OrchestraGraph) -> str | None:
    for n in graph.nodes:
        cmd = getattr(n, "command", None)
        if cmd and "--spec-tests" in cmd:
            return str(cmd[list(cmd).index("--spec-tests") + 1])
    return None


def condemned_suite_feedback(persistent: list[str], samples: int) -> str:
    listed = "\n".join(f"- {failure_key(n).split('::', 1)[-1]}" for n in persistent[:40])
    return (
        f"Your previous suite for this milestone failed on every one of {samples} independent implementations -- "
        f"{len(persistent)} cases, all of them, in every attempt:\n{listed}\n"
        "When every implementation fails every case, the common cause is in the suite, not the code: a fixture or "
        "import the documents do not promise, a wrong shared assumption, a construction the documents describe "
        "differently. Re-author the suite from the documents. Every test must quote the sentence it enforces; "
        "import project symbols inside each test; keep the depth and breadth of the mandate."
    )


def with_spec_dir(graph: OrchestraGraph, spec_dir: str) -> OrchestraGraph:
    """Every harness node pointed at `spec_dir` (one frozen-suite dir per re-author sample)."""
    nodes = []
    for n in graph.nodes:
        cmd = list(getattr(n, "command", None) or [])
        if cmd and "--spec-tests" in cmd:
            cmd[cmd.index("--spec-tests") + 1] = spec_dir
            nodes.append(n.model_copy(update={"command": cmd}))
        else:
            nodes.append(n)
    return graph.model_copy(update={"nodes": nodes})


def gradable_count(spec_dir: str) -> int | None:
    """collected - vacuous from the harness baseline beside a frozen suite, if written."""
    p = Path(str(spec_dir) + ".baseline.json")
    try:
        d = json.loads(p.read_text())
        return max(0, int(d.get("collected") or 0) - int(d.get("vacuous") or 0))
    except (OSError, ValueError, TypeError):
        return None


FLAKY_SHARE = 2 / 3


def owner_share(targets: list[str], frames: dict[str, str], steps: list[WriterStep]) -> tuple[str | None, float, dict[str, str]]:
    """The editing node owning most of `targets` (last writer of the file each
    lands in; unowned -> last writer) and its share of them."""
    code_steps = [s for s in steps if not s.is_author]
    if not code_steps or not targets:
        return None, 0.0, {}
    owners: dict[str, str] = {}
    for f in targets:
        file = frames.get(f)
        owner = None
        if file:
            for s in code_steps:
                if file in s.files:
                    owner = s.node_id
        owners[f] = owner or code_steps[-1].node_id
    counts = Counter(owners.values())
    node, n = counts.most_common(1)[0]
    return node, n / len(owners), owners


def node_position(node_id: str, steps: list[WriterStep]) -> str:
    order = [s.node_id for s in steps if not s.is_author]
    if not order or node_id not in order:
        return "none"
    if node_id == order[0]:
        return "first"
    if node_id == order[-1]:
        return "last"
    return "middle"


def flaky_feedback(flaky: list[str], evidence_relpath: str = "../repair_evidence") -> str:
    listed = "\n".join(f"- {failure_key(n).split('::', 1)[-1]}" for n in flaky)
    return (
        f"The acceptance gate passed. These behaviours ({len(flaky)}) pass in some independent attempts at this "
        f"milestone and fail in others -- they are unstable, and making them hold every time is this run's "
        f"target:\n{listed}\n"
        f"The tests for them, their last output, and the exact command to run them are beside this repository "
        f"in {evidence_relpath}/ (start with README.md). Run them before and after each change; they are copies "
        "of a sealed suite, so editing them changes nothing. Keep everything that already passes. Do not delete "
        "spec_tests, check_tests or documented public symbols."
    )
