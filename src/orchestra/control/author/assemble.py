"""Assemble the test author's prompt (spec §2.3).

Order: fixed rules + rendered rules document (active and trial entries) +
documented-behaviour inventory. With no rendered rules and no inventory the
result is the fixed rules alone, byte for byte the ``test_author.yaml``
prompt (stage B0's hard requirement).

The hook the run uses is an environment variable, like the repair trigger:
``ADAMAS_AUTHOR_RULES_DOC`` names the rules-document directory (or its
``current`` link); ``ADAMAS_AUTHOR_INVENTORY`` names a rendered inventory
text file for the milestone being compiled. Unset, ``author_prompt`` returns
the role prompt it was handed without reading any file.
"""

from __future__ import annotations

import os
from pathlib import Path

from .rules_doc import load_rules_doc, render_rules

CORE_RULES_PATH = Path("configs/roles/test_author/core_rules.md")
RULES_DOC_ENV = "ADAMAS_AUTHOR_RULES_DOC"
INVENTORY_ENV = "ADAMAS_AUTHOR_INVENTORY"
#: stage B3: the per-run inventory directory; set, the author prompt carries the milestone's
#: documented-behaviour inventory and the author node runs the audit / coverage / fix rounds
INVENTORY_DIR_ENV = "ADAMAS_AUTHOR_INVENTORY_DIR"
DOCS_DIR_ENV = "ADAMAS_AUTHOR_DOCS_DIR"
PACKAGES_ENV = "ADAMAS_AUTHOR_PACKAGES"
PLAN_FILE_ENV = "ADAMAS_AUTHOR_PLAN_FILE"
HARD_MIN_ENV = "ADAMAS_AUTHOR_HARD_MIN"
MAX_ROUNDS_ENV = "ADAMAS_AUTHOR_MAX_ROUNDS"
TASK_ENV = "ADAMAS_AUTHOR_TASK"


def load_core_rules(path: str | Path = CORE_RULES_PATH) -> str:
    return Path(path).read_text(encoding="utf-8")


def assemble_prompt(core_rules: str, rules_text: str = "", inventory_text: str = "") -> str:
    """Fixed rules, then the rules document, then the inventory; each part stripped."""
    parts = [core_rules.strip()]
    if rules_text and rules_text.strip():
        parts.append(rules_text.strip())
    if inventory_text and inventory_text.strip():
        parts.append(inventory_text.strip())
    return "\n\n".join(parts)


def milestone_inventory(milestone, *, env: dict | None = None):
    """The milestone's inventory (extracted once, then read from the run's inventory directory),
    or None when the inventory feature is off."""
    e = os.environ if env is None else env
    inv_dir = (e.get(INVENTORY_DIR_ENV) or "").strip()
    docs_dir = (e.get(DOCS_DIR_ENV) or "").strip()
    if not inv_dir or not docs_dir or milestone is None:
        return None
    from orchestra.codeprojecteval.behaviour_inventory import extract_inventory
    from orchestra.codeprojecteval.public_symbols import load_docs

    acceptance = getattr(milestone, "acceptance", None)
    criteria = list(getattr(acceptance, "criteria", None) or [])
    return extract_inventory(
        task=(e.get(TASK_ENV) or ""), milestone_id=milestone.milestone_id, objective=milestone.objective,
        criteria=criteria, focus_paths=list(milestone.focus_paths or []), docs=load_docs(docs_dir), out_dir=inv_dir,
    )


def record_author_contract(contract_id: str, milestone, *, env: dict | None = None) -> None:
    """``<inventory dir>/contracts/<contract id>.json`` -> the milestone; written only with the feature on."""
    e = os.environ if env is None else env
    inv_dir = (e.get(INVENTORY_DIR_ENV) or "").strip()
    if not inv_dir:
        return
    import json

    d = Path(inv_dir) / "contracts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{contract_id}.json").write_text(json.dumps({
        "contract_id": contract_id, "milestone_id": milestone.milestone_id,
        "focus_paths": list(milestone.focus_paths or []), "depends_on": list(milestone.depends_on or []),
    }), encoding="utf-8")


def author_prompt(default_prompt: str, *, env: dict | None = None, milestone=None) -> str:
    """``_author_prompt`` plus, with a memory run active, the author bank's recalled rules (memory spec §3.3)."""
    base = _author_prompt(default_prompt, env=env, milestone=milestone)
    e = os.environ if env is None else env
    if not (e.get("ADAMAS_MEMORY_RUN") or "").strip() or milestone is None:
        return base
    from orchestra.memory.author import author_memory_block

    block = author_memory_block(milestone, env=e)
    return f"{base.rstrip()}\n\n{block}" if block else base


def _author_prompt(default_prompt: str, *, env: dict | None = None, milestone=None) -> str:
    """The prompt the test_author role gets in this process.

    Without ``ADAMAS_AUTHOR_RULES_DOC`` in the environment this is
    ``default_prompt`` unchanged. With it, the fixed rules file replaces the
    yaml prompt and the rules document and inventory are appended.
    """
    e = os.environ if env is None else env
    rules_dir = (e.get(RULES_DOC_ENV) or "").strip()
    if not rules_dir:
        return default_prompt
    doc = load_rules_doc(rules_dir)
    inventory = ""
    inv_path = (e.get(INVENTORY_ENV) or "").strip()
    if inv_path:
        inventory = Path(inv_path).read_text(encoding="utf-8")
    inv = milestone_inventory(milestone, env=e)
    if inv is not None:
        from orchestra.codeprojecteval.behaviour_inventory import render_for_author

        inventory = "\n\n".join(t for t in (inventory, render_for_author(inv)) if t)
    return assemble_prompt(load_core_rules(), render_rules(doc.rendered()), inventory)


__all__ = ["CORE_RULES_PATH", "DOCS_DIR_ENV", "HARD_MIN_ENV", "INVENTORY_DIR_ENV", "INVENTORY_ENV", "MAX_ROUNDS_ENV", "PACKAGES_ENV", "PLAN_FILE_ENV", "RULES_DOC_ENV", "TASK_ENV", "milestone_inventory", "record_author_contract", "assemble_prompt", "author_prompt", "load_core_rules"]
