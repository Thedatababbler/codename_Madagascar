"""Checks on evolver proposals (self-evolution spec §9.4).

A proposal is a playbook row or a first-pass entry in the §3.1 / §4.3 row
format. It is accepted only when every role it names exists in the role
pool, every template exists in the template library, the action and error
classes are legal, a first-pass entry cites its ``source_rows``, and the
instruction text contains no identifier that belongs to a training task
(task names, module names, case names). Rejections carry their reasons so
the cycle report can show them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
ROLES_DIR = ROOT / "configs" / "roles"
TEMPLATES_DIR = ROOT / "configs" / "subgraph_templates"

ERROR_CLASSES = ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9")
ACTIONS = ("S", "T", "R", "B", "N")
READ_ONLY_ROLES = ("contract_critic", "spec_auditor", "behaviour_critic")
F_ACTION_KINDS = ("add_reviewer", "instruction", "template", "budget", "chain")
FEATURES = (
    "kind", "index", "total", "n_focus_files", "n_documented_exceptions", "n_transitions", "n_public_symbols",
    "n_classes", "dependency_depth", "has_state_machine", "has_io", "n_dependents",
)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{2,}")
_GENERIC = {
    "the", "and", "for", "with", "that", "this", "test", "tests", "error", "errors", "class", "classes", "module",
    "modules", "function", "functions", "before", "after", "when", "then", "each", "every", "from", "into", "not",
    "only", "must", "should", "never", "always", "case", "cases", "spec", "suite", "implementer", "reviewer",
    "improver", "fixer", "builder", "author", "critic", "auditor", "repairer", "python", "pytest", "import", "raise",
    "return", "value", "values", "type", "types", "name", "names", "list", "dict", "str", "int", "float", "bool",
    "none", "true", "false", "self", "args", "kwargs", "path", "paths", "file", "files", "docs", "document",
}


def available_roles(directory: Path = ROLES_DIR) -> frozenset[str]:
    return frozenset(p.stem for p in Path(directory).glob("*.yaml"))


def available_templates(directory: Path = TEMPLATES_DIR) -> frozenset[str]:
    return frozenset(p.stem for p in Path(directory).glob("*.yaml"))


def training_identifiers(
    *, task_ids: Iterable[str] = (), milestone_ids: Iterable[str] = (), case_names: Iterable[str] = (),
    focus_paths: Iterable[str] = (),
) -> frozenset[str]:
    """Words that would tie an instruction to a training task: task, milestone and module names, case names."""
    words: set[str] = set()
    for task in task_ids:
        words.update(_split(task))
    for mid in milestone_ids:
        words.add(str(mid).lower())
    for case in case_names:
        body = str(case).split("::", 1)[-1]
        for part in body.split("::"):
            name = part.split("[", 1)[0].lower()
            words.add(name)
            if name.startswith("test_"):
                words.add(name[5:])
        head = str(case).split("::", 1)[0]
        stem = Path(head).stem.lower()
        if stem.startswith("test_"):
            stem = stem[5:]
        words.update(_split(stem))
    for fp in focus_paths:
        stem = Path(str(fp)).stem.lower()
        words.update(_split(stem))
        words.add(stem)
    return frozenset(w for w in words if len(w) >= 3 and w not in _GENERIC and not w.startswith("__"))


def _split(text: str) -> set[str]:
    text = str(text).lower()
    parts = {text}
    for p in re.split(r"[^a-z0-9]+", text):
        if p:
            parts.add(p)
    return parts


@dataclass
class ValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def leaked_identifiers(text: str | None, identifiers: frozenset[str]) -> list[str]:
    if not text:
        return []
    found = []
    for tok in set(_WORD_RE.findall(text)):
        low = tok.lower()
        if low in identifiers:
            found.append(tok)
    return sorted(found)


def validate_row(
    row: Mapping[str, Any], *, roles: frozenset[str], templates: frozenset[str], identifiers: frozenset[str],
    existing_ids: Iterable[str] = (),
) -> ValidationResult:
    reasons: list[str] = []
    rid = str(row.get("row_id") or "")
    if not re.fullmatch(r"[A-Z*][A-Z0-9*]*-[A-Z][0-9]+[a-z]?", rid):
        reasons.append(f"row_id {rid!r} is not of the form <class>-<action><n>")
    if rid in set(existing_ids):
        reasons.append(f"row_id {rid!r} already exists")
    if str(row.get("table") or "repair") != "repair":
        reasons.append("only repair-table rows may be proposed as rows")
    classes = [str(c) for c in (row.get("error_classes") or ())]
    bad = [c for c in classes if c != "*" and c not in ERROR_CLASSES]
    if not classes or bad:
        reasons.append(f"error_classes must be within {ERROR_CLASSES} or '*', got {classes}")
    action = str(row.get("action") or "")
    if action not in ACTIONS:
        reasons.append(f"action {action!r} not in {ACTIONS}")
    target = row.get("target_template")
    if action == "T" and not target:
        reasons.append("a T row needs target_template")
    if target and str(target) not in templates:
        reasons.append(f"unknown template {target!r}")
    if action != "T" and target:
        reasons.append("only T rows may set target_template")
    for edit in row.get("slot_edits") or ():
        role = str((edit or {}).get("role") or "")
        if role not in roles:
            reasons.append(f"unknown role {role!r}")
        if (edit or {}).get("read_only") is False and role in READ_ONLY_ROLES:
            reasons.append(f"{role} is read-only and cannot be a writer")
        if (edit or {}).get("read_only") is True and role not in READ_ONLY_ROLES:
            reasons.append(f"{role} is a writer; read_only slots must use a critic/auditor role")
    if action == "S" and not (row.get("instruction") or "").strip():
        reasons.append("an S row needs instruction text")
    if action != "S" and (row.get("instruction") or "").strip():
        reasons.append("only S rows carry instruction text")
    if action == "B" and not row.get("budget_delta"):
        reasons.append("a B row needs budget_delta")
    leaks = leaked_identifiers(str(row.get("instruction") or "") + " " + str(row.get("intent") or ""), identifiers)
    if leaks:
        reasons.append(f"instruction mentions training-task identifiers: {leaks}")
    if not str(row.get("intent") or "").strip():
        reasons.append("intent is required")
    if not str(row.get("targets") or row.get("origin") or "").strip():
        reasons.append("the proposal must say which unresolved cluster or source row it targets")
    return ValidationResult(ok=not reasons, reasons=reasons)


def validate_f_entry(
    entry: Mapping[str, Any], *, roles: frozenset[str], templates: frozenset[str], identifiers: frozenset[str],
    repair_row_ids: Iterable[str], existing_ids: Iterable[str] = (), instructions: Mapping[str, str] | None = None,
) -> ValidationResult:
    reasons: list[str] = []
    eid = str(entry.get("entry_id") or "")
    if not re.fullmatch(r"F[0-9]+[a-z]?", eid):
        reasons.append(f"entry_id {eid!r} is not of the form F<n>")
    if eid in set(existing_ids):
        reasons.append(f"entry_id {eid!r} already exists")
    sources = [str(s) for s in (entry.get("source_rows") or ())]
    known = set(repair_row_ids)
    if not sources:
        reasons.append("a first-pass entry must cite source_rows")
    for s in sources:
        if s not in known:
            reasons.append(f"source row {s!r} is not in the repair table")
    triggers = list(entry.get("triggers") or ())
    if not triggers:
        reasons.append("at least one trigger is required")
    for t in triggers:
        feat = str((t or {}).get("feature") or "")
        if feat not in FEATURES:
            reasons.append(f"unknown feature {feat!r}")
        if str((t or {}).get("op") or "") not in ("==", "!=", ">=", "<=", ">", "<"):
            reasons.append(f"bad trigger operator {(t or {}).get('op')!r}")
    classes = [str(c) for c in (entry.get("predicted_error_classes") or ())]
    if not classes or any(c not in ERROR_CLASSES for c in classes):
        reasons.append(f"predicted_error_classes must be within {ERROR_CLASSES}, got {classes}")
    actions = list(entry.get("actions") or ())
    if not actions:
        reasons.append("at least one action is required")
    for a in actions:
        kind = str((a or {}).get("kind") or "")
        if kind not in F_ACTION_KINDS:
            reasons.append(f"unknown action kind {kind!r}")
        elif kind == "add_reviewer":
            role = str(a.get("role") or "")
            if role not in roles:
                reasons.append(f"unknown role {role!r}")
            elif role not in READ_ONLY_ROLES:
                reasons.append(f"add_reviewer needs a read-only role, got {role!r}")
        elif kind == "template" and str(a.get("template_id") or a.get("template") or "") not in templates:
            reasons.append(f"unknown template {a.get('template_id') or a.get('template')!r}")
        elif kind == "instruction":
            text = str(a.get("text") or (instructions or {}).get(str(a.get("file") or ""), ""))
            if not text.strip():
                reasons.append("instruction action without text")
            leaks = leaked_identifiers(text, identifiers)
            if leaks:
                reasons.append(f"instruction mentions training-task identifiers: {leaks}")
        elif kind == "budget" and not (a.get("steps") or a.get("seconds")):
            reasons.append("budget action needs steps or seconds")
    leaks = leaked_identifiers(str(entry.get("intent") or ""), identifiers)
    if leaks:
        reasons.append(f"intent mentions training-task identifiers: {leaks}")
    if str(entry.get("state") or "candidate") != "candidate":
        reasons.append("proposed entries start as candidate")
    return ValidationResult(ok=not reasons, reasons=reasons)


__all__ = [
    "ACTIONS", "ERROR_CLASSES", "FEATURES", "F_ACTION_KINDS", "READ_ONLY_ROLES", "ValidationResult",
    "available_roles", "available_templates", "leaked_identifiers", "training_identifiers", "validate_f_entry",
    "validate_row",
]
