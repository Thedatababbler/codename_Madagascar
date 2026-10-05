"""Route failures that are not this milestone's to fix (self-evolution spec §2.2.2).

Before a persistent set is classified and handed to R0 and the table, the
cases that do not belong to this milestone are taken out and written to
``routing_records.jsonl``:

- ``regression_by_current``: a committed predecessor's case that passed when
  it committed and fails on the incumbent. Not a case of this suite at all;
  it stays *in* the milestone (the repairer is told not to break it).
- ``inherited.*``: a case of this suite whose symbols all live in a
  predecessor's focus paths and none in ours. ``committed_with_failure`` when
  the predecessor already had a failing case on those files at commit,
  ``uncovered`` when no predecessor case touches them, ``boundary`` otherwise.
- ``suite_suspect``: the citation above the test is not in the documents, or
  the case is flaky and timing out.
- ``environment``: the failure output shows the package shadow or an import
  from outside the workspace.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from orchestra.control.fast_loop.error_classes import CaseFacts

ROUTES = (
    "regression_by_current",
    "inherited.committed_with_failure",
    "inherited.uncovered",
    "inherited.boundary",
    "suite_suspect",
    "suite_conflict",
    "environment",
)
_CITATION_RE = re.compile(r"#\s*(?:PRD|Architecture|UML|Directory|Design|README)[^\"\n]*\"([^\"\n]{12,})\"")
#: With the author's evolvable layer on (ADAMAS_AUTHOR_RULES_DOC set, control/author) the
#: ``# DOC: "..."`` form the verifier prompt uses is a citation too. Off, the regex above
#: is used unchanged.
_CITATION_RE_DOC = re.compile(r"#\s*(?:PRD|Architecture|UML|Directory|Design|README|DOC)[^\"\n]*\"([^\"\n]{12,})\"")


def _citation_re() -> re.Pattern[str]:
    return _CITATION_RE_DOC if os.environ.get("ADAMAS_AUTHOR_RULES_DOC") else _CITATION_RE
_ENV_TOKENS = ("adamas shadow", "not built in this workspace", "site-packages/")


@dataclass(frozen=True)
class Predecessor:
    milestone_id: str
    focus_paths: tuple[str, ...]
    #: committed case keys of its frozen suite
    committed_failed: frozenset[str] = frozenset()
    committed_passed: frozenset[str] = frozenset()
    #: case key -> repository files its symbols live in (lazily supplied)
    case_files: Callable[[str], set[str]] | None = None


@dataclass
class RouteRecord:
    case_id: str
    route: str
    owner_milestone_id: str = ""
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"case_id": self.case_id, "route": self.route, "owner_milestone_id": self.owner_milestone_id, "evidence": self.evidence}


@dataclass
class RoutingResult:
    routed: list[RouteRecord] = field(default_factory=list)
    #: persistent cases that stay with this milestone
    kept: list[str] = field(default_factory=list)
    #: predecessor cases the incumbent broke; handed to the repairer alongside `kept`
    regressions: list[str] = field(default_factory=list)

    @property
    def suite_suspect(self) -> list[str]:
        return [r.case_id for r in self.routed if r.route == "suite_suspect"]

    def to_dict(self) -> dict[str, object]:
        return {
            "routed": [r.to_dict() for r in self.routed],
            "kept": list(self.kept),
            "regressions": list(self.regressions),
        }


def citations_in(source: str) -> list[str]:
    """Sentences quoted in ``# PRD: "..."`` style comments of a test's source."""
    return [" ".join(m.group(1).split()) for m in _citation_re().finditer(source or "")]


def citation_ok(source: str, docs_text: str) -> bool | None:
    """True when at least one quoted sentence is in the documents; None when nothing is quoted."""
    quotes = citations_in(source)
    if not quotes or not docs_text:
        return None
    docs = " ".join(docs_text.split()).lower()
    return any(q.lower() in docs for q in quotes)


def docs_text_of(repo: str | Path) -> str:
    docs = Path(repo) / "docs"
    if not docs.is_dir():
        return ""
    parts = []
    for p in sorted(docs.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".md", ".txt", ".rst", ".puml", ".uml"):
            try:
                parts.append(" ".join(p.read_text(encoding="utf-8", errors="replace").split()))
            except OSError:
                continue
    return " ".join(parts)


def _under(path: str, roots: Iterable[str]) -> bool:
    p = path.strip("/")
    for r in roots:
        r = str(r).strip("/")
        if not r:
            continue
        if p == r or p.startswith(r + "/"):
            return True
    return False


def route_case(facts: CaseFacts, *, current_focus: Iterable[str], predecessors: list[Predecessor]) -> RouteRecord | None:
    """The route of one persistent case, or None when it stays with this milestone."""
    if facts.environment_hit or any(tok in (facts.output or "") for tok in _ENV_TOKENS):
        return RouteRecord(facts.key, "environment", evidence="import resolved outside the workspace")
    if facts.citation_ok is False:
        return RouteRecord(facts.key, "suite_suspect", evidence="quoted sentence not found in the documents")
    if facts.flaky and facts.timed_out:
        return RouteRecord(facts.key, "suite_suspect", evidence="flaky and timing out")
    files = set(facts.symbol_files)
    if not files or any(_under(f, current_focus) for f in files):
        return None
    for pred in predecessors:
        if not pred.focus_paths or not all(_under(f, pred.focus_paths) for f in files):
            continue
        touched_failed = False
        touched_any = False
        if pred.case_files is not None:
            for key in pred.committed_failed:
                if pred.case_files(key) & files:
                    touched_failed = True
                    break
            if not touched_failed:
                for key in pred.committed_passed:
                    if pred.case_files(key) & files:
                        touched_any = True
                        break
        if touched_failed:
            return RouteRecord(facts.key, "inherited.committed_with_failure", pred.milestone_id, "predecessor committed with a failing case on these files")
        if not touched_any:
            return RouteRecord(facts.key, "inherited.uncovered", pred.milestone_id, "no predecessor case exercises these files")
        return RouteRecord(facts.key, "inherited.boundary", pred.milestone_id, "predecessor covers these files and passed")
    return None


def route_persistent(
    cases: list[CaseFacts],
    *,
    current_focus: Iterable[str],
    predecessors: list[Predecessor],
    incumbent_prior_regressions: dict[str, list[str]] | None = None,
) -> RoutingResult:
    result = RoutingResult()
    focus = list(current_focus)
    for facts in cases:
        rec = route_case(facts, current_focus=focus, predecessors=predecessors)
        if rec is None:
            result.kept.append(facts.case_id)
        else:
            result.routed.append(rec)
    for owner, regressed in (incumbent_prior_regressions or {}).items():
        for key in regressed:
            result.routed.append(RouteRecord(key, "regression_by_current", owner, "passed when the predecessor committed, fails on the incumbent"))
            result.regressions.append(key)
    return result


__all__ = [
    "ROUTES",
    "Predecessor",
    "RouteRecord",
    "RoutingResult",
    "citation_ok",
    "citations_in",
    "docs_text_of",
    "route_case",
    "route_persistent",
]
