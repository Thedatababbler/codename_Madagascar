"""First-pass memory at plan time (memory spec §3.1) and the per-task start (§2.1, §6).

``start_task_memory`` pins the snapshot for the whole task, checks the
workspace holds nothing of the memory store and activates the run checks.
``apply_first_pass_memory`` runs, for every milestone:
- the judge (one model call);
- recall;
- assembly into the writer's ``memory_block``;
- a pattern's read-only role and budget, handed to the designer's existing
  edits.

It registers the writer's contract with the tags it must carry, and records
everything in memory_delivery.json.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from orchestra.memory.assemble import first_pass_block
from orchestra.memory.judge import judge
from orchestra.memory.recall import recall
from orchestra.memory.runtime import CANARY_RE, MemoryRun, memory_access_in
from orchestra.memory.store import BANKS, memory_root, pin
from orchestra.memory.validate import load_sources

DOCS_EXCERPT_CHARS = 12000
REQUIRE_RECALL_ENV = "ADAMAS_MEMORY_REQUIRE_RECALL"


class WorkspaceIsolationError(RuntimeError):
    """The agent workspace contains memory files."""


def assert_workspace_clean(workspace: Path, root: Path) -> None:
    """No memory store, bank file or canary anywhere in the workspace (memory spec §2.1)."""
    workspace = Path(workspace).resolve()
    root = Path(root).resolve()
    if workspace == root or root in workspace.parents or workspace in root.parents:
        raise WorkspaceIsolationError(f"workspace {workspace} overlaps the memory store {root}")
    if (workspace / "memory" / "VERSION").exists() or any((workspace / "memory" / b).is_dir() for b in BANKS):
        raise WorkspaceIsolationError(f"workspace {workspace} contains a memory store")
    bank_files = {p.read_bytes() for b in BANKS for p in (root / b).glob("*.yaml") if p.is_file() and p.stat().st_size > 64}
    for p in workspace.rglob("*"):
        if ".git" in p.parts or not p.is_file() or p.stat().st_size > 5_000_000:
            continue
        data = p.read_bytes()
        if CANARY_RE.search(data.decode("utf-8", errors="ignore")) or data in bank_files:
            raise WorkspaceIsolationError(f"workspace file {p} carries memory content")


def start_task_memory(*, run_dir: Path, source_repo: Path, cfg: dict[str, Any], task_id: str) -> MemoryRun:
    root = memory_root(cfg)
    assert_workspace_clean(source_repo, root)
    run = MemoryRun.activate(Path(run_dir), cfg, view=pin(root), task_id=task_id)
    run.record("workspace_isolation", {"workspace": str(source_repo), "checked": True})
    return run


def full_docs(task: Any) -> str:
    """Every design document of a task, uncut (the judge reads the paragraphs of its milestone)."""
    parts = []
    for rel in [getattr(task, "prd_path", ""), getattr(task, "architecture_path", ""), *list(getattr(task, "uml_paths", []) or [])]:
        if rel:
            try:
                parts.append(task.read(rel, limit=10_000_000))
            except Exception:  # noqa: BLE001
                continue
    return "\n\n".join(p for p in parts if p)


def milestone_text(m: Any) -> str:
    acc = getattr(m, "acceptance", None)
    crit = list(getattr(acc, "criteria", []) or [])
    corner = list(getattr(acc, "corner_cases", []) or [])
    lines = [f"Title: {m.title}", f"Objective: {m.objective}"]
    if m.focus_paths:
        lines.append("Files: " + ", ".join(m.focus_paths))
    if crit:
        lines.append("Acceptance criteria:\n" + "\n".join(f"- {c}" for c in crit))
    if corner:
        lines.append("Corner cases:\n" + "\n".join(f"- {c}" for c in corner))
    return "\n".join(lines)


def milestone_features(m: Any, *, index: int, total: int, docs: str) -> dict[str, Any]:
    from orchestra.control.first_pass.features import milestone_features as base

    f = dict(base(m, index=index, total=total, docs_text=docs))
    f["position_group"] = "first" if index == 0 else ("last" if index == total - 1 else "middle")
    return f


def source_tasks(entry: dict[str, Any], sources: dict[str, dict[str, Any]]) -> set[str]:
    return {str((sources.get(str(r)) or {}).get("task") or "") for r in ((entry.get("evidence") or {}).get("source_records") or [])} - {""}


def apply_first_pass_memory(draft: Any, *, docs: str, task_id: str, call: Callable[[str, str], str] | None = None,
                            run: MemoryRun | None = None, only: set[str] | None = None) -> Any:
    """The draft with each milestone's writer carrying its recalled memory (judge + recall + assembly).

    ``only`` limits the judge to those milestones (the others pass through
    unchanged), keeping every milestone's real position in the plan.
    """
    from orchestra.control.first_pass.designer import (
        EDITING_DEFAULT,
        _find,
        add_budget,
        add_reviewer,
    )
    from orchestra.realbench.subgraph_builder import contract_id_for

    run = run or MemoryRun.current()
    if run is None:
        return draft
    view = run.view()
    rc = (run.cfg.get("recall") or {})
    sources = load_sources(Path(run.data["memory_root"]))
    total = len(draft.milestones)
    out = []
    for i, m in enumerate(draft.milestones):
        if only is not None and m.milestone_id not in only:
            out.append(m)
            continue
        feats = milestone_features(m, index=i, total=total, docs=docs)
        from orchestra.control.first_pass.features import relevant_docs

        excerpt = relevant_docs(docs, list(m.focus_paths))[:DOCS_EXCERPT_CHARS]
        j = judge(view, "first_pass", milestone_text=milestone_text(m), docs_excerpt=excerpt,
                  max_categories=int(rc.get("max_categories", 3)), call=call)
        pits = recall(view.entries("first_pass", "pitfalls"), categories=j.category_ids, features=feats,
                      domain_tags=j.domain_tags, limit=int(rc.get("max_pitfalls", 5)), id_field="pitfall_id")
        pats = recall(view.entries("first_pass", "patterns"), categories=j.category_ids, features=feats,
                      domain_tags=j.domain_tags, limit=int(rc.get("max_patterns", 2)), id_field="pattern_id")
        notes, roles_added, budget = [], [], {"steps": 0, "seconds": 0}
        for p in pats:
            act = p.get("action") or {}
            if act.get("add_readonly_role"):
                role = str(act["add_readonly_role"])
                m, note = add_reviewer(m, role)
                notes.append(f"{p['pattern_id']}: {note}")
                if role not in [a.role for a in m.agents]:
                    # memory spec §4.2 item 3: the required role must be in the compiled subgraph
                    run.violation("role_missing", f"{p['pattern_id']} requires {role}, absent from {m.milestone_id}'s subgraph ({note})",
                                  milestone=m.milestone_id)
                roles_added.append(role)
            bd = act.get("budget_delta") or {}
            if bd:
                m, note = add_budget(m, int(bd.get("steps") or 0), int(bd.get("seconds") or 0))
                notes.append(f"{p['pattern_id']}: {note}")
                budget["steps"] += int(bd.get("steps") or 0)
                budget["seconds"] += int(bd.get("seconds") or 0)
        block = first_pass_block(pits, pats)
        writer = _find(m, EDITING_DEFAULT)
        contract = ""
        tags: list[str] = []
        if block and writer is None:
            raise RuntimeError(f"memory recalled for {m.milestone_id} but the milestone has no writer")
        if block:
            m = replace(m, agents=[replace(a, memory_block=block) if a is writer else a for a in m.agents])
            contract = contract_id_for(milestone_id=m.milestone_id, role_id=writer.role_id)
            from orchestra.memory.assemble import parse_tags

            tags = parse_tags(block)
            run.register_contract(contract, "first_pass", tags, milestone=m.milestone_id, role=writer.role)
        required = {x for x in (os.environ.get(REQUIRE_RECALL_ENV) or "").split(",") if x}
        if m.milestone_id in required and not block:
            # functional test T3: a run whose target milestone recalled nothing tests nothing; stop before any agent runs
            raise RuntimeError(f"{REQUIRE_RECALL_ENV}: {m.milestone_id} recalled nothing (judge chose {j.category_ids})")
        recalled_entries = pits + pats
        cross = [e.get("pitfall_id") or e.get("pattern_id") for e in recalled_entries
                 if source_tasks(e, sources) - {task_id}]
        run.record("first_pass", {
            **j.to_dict(), "features": feats,
            "recalled": {"pitfalls": [p["pitfall_id"] for p in pits], "patterns": [p["pattern_id"] for p in pats]},
            "tags": tags, "writer_contract": contract, "roles_added": roles_added, "budget_delta": budget,
            "notes": notes, "cross_task_entries": cross,
        }, key=m.milestone_id)
        out.append(m)
    return replace(draft, milestones=out)


__all__ = [
    "WorkspaceIsolationError", "apply_first_pass_memory", "assert_workspace_clean", "full_docs", "memory_access_in",
    "milestone_features", "milestone_text", "source_tasks", "start_task_memory",
]
