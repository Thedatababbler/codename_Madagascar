"""Author memory (memory spec §3.3).

Categories come from the program: the kinds of the milestone's behaviour
inventory (main path, boundary, state transition, error path, integration,
protocol). A milestone without an inventory gets one extracted first, and a
failed extraction stops the run. When the author bank has no recallable rule
at all, nothing can be recalled and no inventory is needed.
"""

from __future__ import annotations

import json
from typing import Any

from orchestra.memory.assemble import parse_tags, rules_block
from orchestra.memory.recall import recall
from orchestra.memory.runtime import MemoryRun
from orchestra.memory.store import RECALLABLE

KIND_CATEGORY = {
    "main_path": "AU-MAIN_PATH", "boundary": "AU-BOUNDARY", "state_transition": "AU-STATE_TRANSITION",
    "error_path": "AU-ERROR_PATH", "integration": "AU-INTEGRATION", "protocol": "AU-PROTOCOL",
}


class AuthorMemoryError(RuntimeError):
    """The milestone's inventory could not be produced; the run stops."""


def _inventory(milestone: Any, run: MemoryRun, env: dict) -> Any:
    from orchestra.control.author.assemble import milestone_inventory

    try:
        inv = milestone_inventory(milestone, env=env)
        if inv is not None:
            return inv
        from orchestra.codeprojecteval.behaviour_inventory import extract_inventory
        from orchestra.codeprojecteval.public_symbols import load_docs
        from orchestra.memory.validate import _dataset_root

        task = str(run.data.get("task_id") or "")
        acceptance = getattr(milestone, "acceptance", None)
        return extract_inventory(
            task=task, milestone_id=milestone.milestone_id, objective=milestone.objective,
            criteria=list(getattr(acceptance, "criteria", None) or []), focus_paths=list(milestone.focus_paths or []),
            docs=load_docs(_dataset_root(task) / task / "docs"), out_dir=run.run_dir / "memory_inventory",
        )
    except Exception as exc:  # noqa: BLE001
        raise AuthorMemoryError(f"behaviour inventory for {milestone.milestone_id} failed: {exc}") from exc


def author_memory_block(milestone: Any, *, env: dict) -> str:
    run = MemoryRun.current()
    if run is None:
        return ""
    view = run.view()
    rules = view.entries("author", "rules")
    if not any(r.get("state") in RECALLABLE for r in rules):
        run.record("author", {"categories": [], "recalled": [], "note": "no recallable author rule; inventory not needed"},
                   key=milestone.milestone_id)
        return ""
    inv = _inventory(milestone, run, env)
    items = list(getattr(inv, "items", None) or [])
    if not items:
        raise AuthorMemoryError(f"behaviour inventory for {milestone.milestone_id} is empty")
    kinds = sorted({str(getattr(i, "kind", "")) for i in items})
    cats = [KIND_CATEGORY[k] for k in kinds if k in KIND_CATEGORY]
    limit = int((run.cfg.get("recall") or {}).get("max_rules", 5))
    got = recall(rules, categories=cats, features={}, domain_tags=[], limit=limit, id_field="rule_id")
    block = rules_block(got)
    run.record("author", {"categories": cats, "inventory_items": len(items), "recalled": [r["rule_id"] for r in got],
                          "tags": parse_tags(block)}, key=milestone.milestone_id)
    return block


def register_author_contract(contract_id: str, milestone_id: str, system_prompt: str) -> None:
    run = MemoryRun.current()
    if run is None:
        return
    tags = parse_tags(system_prompt)
    run.register_contract(contract_id, "author", tags, milestone=milestone_id, role="test_author")
    p = run.run_dir / "memory_delivery.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    entry = (data.get("author") or {}).get(milestone_id) or {}
    run.record("author", {**entry, "contract": contract_id}, key=milestone_id)


__all__ = ["AuthorMemoryError", "KIND_CATEGORY", "author_memory_block", "register_author_contract"]
