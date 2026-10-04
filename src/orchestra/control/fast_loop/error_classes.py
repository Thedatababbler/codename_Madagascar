"""Error classes E1-E9 for persistent failures (self-evolution spec §2.2.3).

Rules first, from what the gate and the frozen suite already show; a class
the rules cannot settle is left to the existing rule → LLM → default chain,
whose coarse ``budget`` / ``functional`` / ``design`` answer maps onto
E1 / E3 / E9. A suite author may pin a case's class with
``@pytest.mark.error_class("E5")``; the marker wins when present.

| class | the milestone looks like |
|---|---|
| E1 | unfinished: agent timed out or ran out of steps, focus file missing, TODO / NotImplementedError |
| E2 | import and interface surface: contracts / cross_imports stage, ImportError / AttributeError, argument-count TypeError |
| E3 | main-path behaviour (the default) |
| E4 | state transitions: an operation sequence and an assertion on the documented transition |
| E5 | error paths: ``pytest.raises`` not triggered or the wrong exception |
| E6 | boundaries and the object protocol: empty input, __eq__ / __hash__ / __iter__ / __repr__ |
| E7 | cross-module integration: the test refers to symbols from >= 2 modules |
| E8 | performance: the per-case timeout |
| E9 | scattered or stuck: >= 3 classes, or the same class twice without progress |
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

CLASSES = ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9")
MARKER_RE = re.compile(r"error_class\(\s*[\"']([A-Za-z_0-9]+)[\"']\s*\)")
#: the verifier prompt's names for the classes (2026-10-04)
MARKER_NAMES = {
    "interface": "E2", "main_path": "E3", "state_transition": "E4", "error_path": "E5",
    "boundary": "E6", "integration": "E7", "performance": "E8", "unfinished": "E1", "scattered": "E9",
}

_E2_NAME = ("import", "export", "reexport", "re_export", "public_surface", "signature", "is_importable", "package_root", "module_file")
_E2_OUT = ("ImportError", "ModuleNotFoundError", "AttributeError: module", "has no attribute",
           "positional argument", "unexpected keyword argument", "required positional", "cannot import name")
_E4_SRC = ("split", "merge", "reopen", "persist", "rollback", "transition", "evict", "flush", "lifecycle",
           "close()", "commit", "checkpoint", "reload", "overflow", "rebalanc")
_E5_OUT = ("DID NOT RAISE", "Failed: DID NOT RAISE")
_E5_NAME = ("raise", "rejects", "invalid", "error", "exception", "unsupported", "not_allowed", "forbid")
_E6_NAME = ("empty", "boundary", "repr", "hash", "iter", "len", "contains", "protocol", "equal", "eq_",
            "zero", "none", "max_", "min_", "limit", "edge")
_E6_SRC = ("__eq__", "__hash__", "__iter__", "__repr__", "__len__", "__contains__", "hash(", "repr(", "iter(", "len(")
_E8_OUT = ("Timeout", "timed out", "TIMED OUT", "Failed: Timeout")
_E1_OUT = ("NotImplementedError", "TODO")


@dataclass
class CaseFacts:
    """Everything the classifier and the router know about one persistent case."""

    case_id: str
    key: str
    name: str = ""
    #: the test function's source (body and decorators), "" when not found
    source: str = ""
    #: repository-relative files that define the symbols the test refers to
    symbol_files: set[str] = field(default_factory=set)
    #: this case's section of the pytest output on the incumbent, "" when unknown
    output: str = ""
    timed_out: bool = False
    flaky: bool = False
    #: True / False when the citation comment was checked against the documents; None when not checked
    citation_ok: bool | None = None
    marker: str | None = None
    environment_hit: bool = False


def marker_in(source: str) -> str | None:
    m = MARKER_RE.search(source or "")
    if not m:
        return None
    raw = m.group(1)
    if re.fullmatch(r"E[1-9]", raw):
        return raw
    return MARKER_NAMES.get(raw.lower())


def classify_case(
    facts: CaseFacts,
    *,
    structural_stage_failed: bool = False,
    agent_truncated: bool = False,
    focus_file_missing: bool = False,
) -> str:
    """One case, one class, by the rules; ``marker`` wins."""
    marker = facts.marker or marker_in(facts.source)
    if marker:
        return marker
    name = facts.name.lower()
    out = facts.output or ""
    src = facts.source or ""
    if agent_truncated or focus_file_missing or any(tok in out for tok in _E1_OUT):
        return "E1"
    if facts.timed_out or any(tok in out for tok in _E8_OUT):
        return "E8"
    if structural_stage_failed or any(tok in out for tok in _E2_OUT) or any(tok in name for tok in _E2_NAME):
        return "E2"
    if "pytest.raises(" in src and (any(tok in out for tok in _E5_OUT) or any(tok in name for tok in _E5_NAME)):
        return "E5"
    if "pytest.raises(" in src and not out:
        return "E5"
    if len(facts.symbol_files) >= 2:
        return "E7"
    low = src.lower()
    if any(tok in low for tok in _E4_SRC) or any(tok in name for tok in ("split", "merge", "reopen", "persist", "transition", "lifecycle")):
        return "E4"
    if any(tok in name for tok in _E6_NAME) or any(tok in src for tok in _E6_SRC):
        return "E6"
    return "E3"


@dataclass(frozen=True)
class Classification:
    #: main classes in order (one, two, or ["E9"])
    classes: tuple[str, ...]
    by_case: dict[str, str]
    counts: dict[str, int]
    note: str

    def to_dict(self) -> dict[str, object]:
        return {"classes": list(self.classes), "by_case": dict(self.by_case), "counts": dict(self.counts), "note": self.note}


def classify_persistent(
    cases: list[CaseFacts],
    *,
    furthest_stage: str = "",
    structural_stage_failed: bool = False,
    agent_truncated: bool = False,
    focus_file_missing: bool = False,
    stuck: bool = False,
) -> Classification:
    """Every persistent case, then the mixing rule (§2.2.3).

    >= 2/3 in one class -> that class; two classes -> both, larger first;
    >= 3 classes -> E9. ``stuck`` (the same class ran two rows without
    moving) is E9 regardless.
    """
    by_case: dict[str, str] = {}
    for c in cases:
        by_case[c.key] = classify_case(
            c,
            structural_stage_failed=structural_stage_failed or str(furthest_stage) in ("contracts", "cross_imports"),
            agent_truncated=agent_truncated,
            focus_file_missing=focus_file_missing,
        )
    counts = Counter(by_case.values())
    if stuck:
        return Classification(("E9",), by_case, dict(counts), "same class twice without progress")
    if not counts:
        return Classification((), by_case, {}, "no persistent cases")
    total = sum(counts.values())
    ranked = [c for c, _ in counts.most_common()]
    top, top_n = counts.most_common(1)[0]
    if top_n * 3 >= total * 2:
        return Classification((top,), by_case, dict(counts), f"{top} holds {top_n}/{total}")
    if len(ranked) == 2:
        return Classification(tuple(ranked), by_case, dict(counts), "two classes, both taken")
    if len(ranked) >= 3:
        return Classification(("E9",), by_case, dict(counts), f"{len(ranked)} classes scattered")
    return Classification((top,), by_case, dict(counts), "single class")


LEGACY_TO_CLASS = {"budget": "E1", "functional": "E3", "design": "E9"}


__all__ = ["CLASSES", "CaseFacts", "Classification", "LEGACY_TO_CLASS", "classify_case", "classify_persistent", "marker_in"]
