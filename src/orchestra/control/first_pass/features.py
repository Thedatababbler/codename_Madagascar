"""Milestone features for the first-pass compatibility table (spec §4.2).

Everything is computed deterministically from the planner's output and the
design documents; nothing here calls a model. The "relevant" part of the
documents for a milestone is the set of paragraphs that mention one of its
focus files (by stem) plus the milestone's own text (objective, rationale,
acceptance criteria).
"""

from __future__ import annotations

import re
from typing import Any

_EXC_RE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Warning))\b")
#: exceptions named without the conventional suffix: "raises UnsupportedFormat", "InvalidDimensions is raised"
_RAISES_RE = re.compile(r"\braises?\s+(?:an?\s+)?`?([A-Z][A-Za-z0-9]+)`?|`?([A-Z][A-Za-z0-9]+)`?\s+(?:is|are|gets)\s+raised")
_CLASS_RE = re.compile(r"\b(?:class\s+)?([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
_IDENT_RE = re.compile(r"`?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\(`?|`([A-Z][A-Za-z0-9_]+)`")
_TRANSITION = ("split", "merge", "evict", "reopen", "flush", "rollback", "transition", "persist",
               "rebalanc", "overflow", "invalidat", "commit", "checkpoint", "compact", "promote", "demote")
_FOUNDATION = ("exception", "error class", "base class", "abstract", "protocol", "registry", "data structure",
               "shared contract", "contracts", "row", "entry", "serializer", "utility", "utils")


def _milestone_text(milestone: Any) -> str:
    acc = getattr(milestone, "acceptance", None)
    parts = [
        str(getattr(milestone, "title", "") or ""),
        str(getattr(milestone, "objective", "") or ""),
        str(getattr(milestone, "risk_rationale", "") or ""),
        " ".join(getattr(acc, "criteria", None) or []),
        " ".join(getattr(acc, "corner_cases", None) or []),
    ]
    return " ".join(parts)


def _stems(focus_paths: list[str]) -> list[str]:
    out = []
    for p in focus_paths:
        name = str(p).rstrip("/").rsplit("/", 1)[-1]
        stem = name[:-3] if name.endswith(".py") else name
        if stem and stem not in ("__init__", "src"):
            out.append(stem)
    return out


def relevant_docs(docs_text: str, focus_paths: list[str]) -> str:
    """Paragraphs of the documents that mention one of the milestone's focus stems."""
    stems = _stems(focus_paths)
    if not stems or not docs_text:
        return ""
    paras = re.split(r"\n\s*\n", docs_text)
    keep = [p for p in paras if any(re.search(rf"\b{re.escape(s)}\b", p) for s in stems)]
    return "\n\n".join(keep)


def milestone_kind(milestone: Any, index: int, total: int) -> str:
    if index == total - 1 or str(getattr(milestone, "gate_level", "")) == "integration":
        return "integration"
    text = _milestone_text(milestone).lower()
    if index == 0 and any(tok in text for tok in _FOUNDATION):
        return "foundation"
    if any(tok in text for tok in ("exception", "base class", "registry", "protocol", "shared contract")):
        return "foundation"
    return "middle"


def milestone_features(milestone: Any, *, index: int, total: int, docs_text: str = "") -> dict[str, Any]:
    focus = list(getattr(milestone, "focus_paths", None) or [])
    own = _milestone_text(milestone)
    relevant = relevant_docs(docs_text, focus)
    scope = own + "\n" + relevant
    exceptions = set(_EXC_RE.findall(scope))
    for a, b in _RAISES_RE.findall(scope):
        exceptions.add(a or b)
    classes = {c for c in _CLASS_RE.findall(scope) if c not in exceptions}
    transitions = sum(scope.lower().count(tok) for tok in _TRANSITION)
    idents: set[str] = set()
    for a, b in _IDENT_RE.findall(scope):
        name = a or b
        if name and not name[0].isdigit():
            idents.add(name.split(".")[-1])
    public_symbols = {s for s in idents | classes if docs_text and s in docs_text}
    return {
        "kind": milestone_kind(milestone, index, total),
        "n_focus_files": len(focus),
        "n_public_symbols": len(public_symbols),
        "n_documented_exceptions": len(exceptions),
        "n_state_transitions": transitions,
        "n_public_classes": len(classes),
        "dep_depth": index,
    }


__all__ = ["foundation_strict", "milestone_features", "milestone_kind", "relevant_docs"]


def foundation_strict(plan_milestones: list[dict], index: int, docs: dict[str, str] | None = None,
                      *, owned_symbols: set[str] | None = None, min_referenced: int = 2) -> tuple[bool, list[str]]:
    """The joint experiment's foundation definition (2026-10-07), separate from ``milestone_kind``
    (which the frozen v1 tables keep using).

    A milestone is a foundation when it is first or second in the dependency chain and at least
    ``min_referenced`` public types / exceptions / registries it owns (CamelCase names, or names
    containing "registry"; all-caps constants excluded) are named in a later milestone's objective
    or acceptance criteria. Returns (is_foundation, the referenced names).
    """
    m = plan_milestones[index]
    by = {x["milestone_id"]: x for x in plan_milestones}

    def depth(mid: str, seen: frozenset = frozenset()) -> int:
        deps = [d for d in by.get(mid, {}).get("depends_on") or [] if d in by and d not in seen]
        return 0 if not deps else 1 + max(depth(d, seen | {mid}) for d in deps)

    if depth(m["milestone_id"]) > 1 and index > 1:
        return False, []
    if owned_symbols is None:
        from orchestra.codeprojecteval.public_symbols import derive_public_symbols
        owned_symbols = derive_public_symbols(docs or {}).owned_by(m.get("focus_paths") or [])
    types = {s for s in owned_symbols if "." not in s
             and ((s[:1].isupper() and any(c.islower() for c in s)) or "registry" in s.lower())}
    later = " ".join(" ".join([x.get("objective", ""), " ".join((x.get("acceptance") or {}).get("criteria") or [])])
                     for x in plan_milestones[index + 1:])
    referenced = sorted(s for s in types if re.search(rf"\b{re.escape(s)}\b", later))
    return len(referenced) >= min_referenced, referenced
