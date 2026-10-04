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


def author_prompt(default_prompt: str, *, env: dict | None = None) -> str:
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
    return assemble_prompt(load_core_rules(), render_rules(doc.rendered()), inventory)


__all__ = ["CORE_RULES_PATH", "INVENTORY_ENV", "RULES_DOC_ENV", "assemble_prompt", "author_prompt", "load_core_rules"]
