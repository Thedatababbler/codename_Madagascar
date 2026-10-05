"""Audit, coverage check, fix rounds and the hard / soft split of an authored suite
(author-evolution spec §2.4-2.6; soft cases in a sibling directory, the user's option A).

Runs inside the author node, after the author's call and before the custody node freezes
``spec_tests/``. The author is called again only with the violation list and the
uncovered inventory items (sentence and kind): no implementation exists yet, so nothing
can leak. When the rounds run out the suite is frozen as it is and the record says what
stayed uncovered. Soft cases leave ``spec_tests/`` for ``spec_tests_soft/`` and the soft
directory leaves the workspace at once, so the gate, the unified acceptance and the
default repair see hard cases only, with no change to any of them.
"""

from __future__ import annotations

import ast
import json
import shutil
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from orchestra.codeprojecteval.behaviour_inventory import Inventory, InventoryItem
from orchestra.codeprojecteval.suite_audit import AuditResult, audit_suite, normalise

SPEC_DIR = "spec_tests"
SOFT_DIR = "spec_tests_soft"
_MIN_CONTAIN = 12


def _matches(citation: str, quote: str) -> bool:
    """Normalised citation and item quote are equal or one contains the other."""
    c, q = citation, normalise(quote)
    if not c or not q:
        return False
    if c == q:
        return True
    return (len(c) >= _MIN_CONTAIN and c in q) or (len(q) >= _MIN_CONTAIN and q in c)


@dataclass
class Coverage:
    hard_total: int
    hard_covered: int
    uncovered: list[InventoryItem]
    case_items: dict[str, list[str]]          # "file::test" -> item ids it cites
    by_kind: dict[str, tuple[int, int]]       # kind -> (covered, total) of hard items

    @property
    def hard_ratio(self) -> float:
        return self.hard_covered / self.hard_total if self.hard_total else 1.0

    def uncovered_text(self) -> str:
        if not self.uncovered:
            return ""
        lines = ["These required documented behaviours have no test yet. Add at least one test for each, "
                 "quoting the sentence in its citation comment:", ""]
        for i in self.uncovered:
            lines.append(f"- ({i.kind}) `{i.symbol}`: \"{i.quote}\"")
        return "\n".join(lines)


def coverage(audit: AuditResult, inv: Inventory) -> Coverage:
    case_items: dict[str, list[str]] = {}
    covered: set[str] = set()
    for case, quotes in audit.citations.items():
        hits = [i.item_id for i in inv.items if any(_matches(q, i.quote) for q in quotes)]
        case_items[case] = hits
        covered.update(hits)
    hard = inv.hard()
    by_kind: dict[str, tuple[int, int]] = {}
    for i in hard:
        c, t = by_kind.get(i.kind, (0, 0))
        by_kind[i.kind] = (c + (i.item_id in covered), t + 1)
    return Coverage(hard_total=len(hard), hard_covered=sum(1 for i in hard if i.item_id in covered),
                    uncovered=[i for i in hard if i.item_id not in covered], case_items=case_items, by_kind=by_kind)


def soft_cases(audit: AuditResult, cov: Coverage, inv: Inventory) -> list[str]:
    """Cases that are soft: an unnamed error path checked with ``raises(Exception)`` (the user's
    rule), or citing soft items only. A case citing any hard item stays hard."""
    tier = {i.item_id: i.tier for i in inv.items}
    out = []
    for case, items in cov.case_items.items():
        if case in audit.soft_cases:
            out.append(case)
        elif items and all(tier.get(i) == "soft" for i in items):
            out.append(case)
    return sorted(set(out))


# --- AST split -----------------------------------------------------------------


def _test_ranges(src: str) -> dict[str, tuple[int, int, str | None]]:
    """test name -> (first line incl. the comment block and decorators above, last line, class or None)."""
    tree = ast.parse(src)
    lines = src.splitlines()
    out: dict[str, tuple[int, int, str | None]] = {}

    def start_of(node) -> int:
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        j = first - 2
        while j >= 0 and lines[j].strip().startswith("#"):
            j -= 1
        return j + 2

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            out[node.name] = (start_of(node), node.end_lineno, None)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    out[f"{node.name}::{sub.name}"] = (start_of(sub), sub.end_lineno, node.name)
    return out


def _remove(src: str, names: set[str]) -> str:
    """``src`` without the named tests; a class left with no statement is removed whole."""
    if not names:
        return src
    tree = ast.parse(src)
    ranges = _test_ranges(src)
    drop: list[tuple[int, int]] = [(ranges[n][0], ranges[n][1]) for n in names if n in ranges]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            members = [s for s in node.body]
            removed = [s for s in members if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and f"{node.name}::{s.name}" in names]
            if removed and len(removed) == len(members):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                drop.append((first, node.end_lineno))
    lines = src.splitlines(keepends=True)
    keep = [True] * len(lines)
    for a, b in drop:
        for k in range(a - 1, b):
            keep[k] = False
    return "".join(ln for ln, k in zip(lines, keep) if k)


def split_soft(spec_dir: str | Path, soft_dir: str | Path, soft: Iterable[str]) -> dict:
    """Move the soft cases (``file::test`` or ``file::Class::test``) of ``spec_dir`` into
    ``soft_dir``. Every test file with a soft case is written twice: in ``spec_dir`` without
    its soft cases, in ``soft_dir`` without its hard ones; module-level imports, helpers,
    fixtures and the other non-test modules (conftest, __init__, helpers) are kept on both
    sides, so either suite collects alone. Returns case counts before and after."""
    spec, dest = Path(spec_dir), Path(soft_dir)
    wanted: dict[str, set[str]] = {}
    for c in soft:
        f, _, rest = c.partition("::")
        wanted.setdefault(f, set()).add(rest)
    before = after_hard = after_soft = 0
    if not wanted:
        return {"before": None, "hard": None, "soft": 0, "files": 0}
    dest.mkdir(parents=True, exist_ok=True)
    for p in sorted(spec.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(spec)
        src = p.read_text(encoding="utf-8")
        try:
            names = set(_test_ranges(src))
        except SyntaxError:
            names = set()
        before += len(names)
        soft_here = wanted.get(rel.as_posix(), set()) & names
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not names:
            shutil.copy2(p, target)          # conftest, __init__, helper modules: both sides
            continue
        if not soft_here:
            continue                          # an all-hard test file stays only in spec_dir
        hard_src = _remove(src, soft_here)
        soft_src = _remove(src, names - soft_here)
        ast.parse(hard_src)
        ast.parse(soft_src)
        p.write_text(hard_src, encoding="utf-8")
        target.write_text(soft_src, encoding="utf-8")
        after_soft += len(soft_here)
    after_hard = sum(len(_test_ranges(q.read_text(encoding="utf-8"))) for q in spec.rglob("*.py") if "__pycache__" not in q.parts)
    return {"before": before, "hard": after_hard, "soft": after_soft, "files": len(wanted)}


# --- the loop ------------------------------------------------------------------


@dataclass
class ReviewRecord:
    milestone_id: str
    rounds: list[dict] = field(default_factory=list)
    fix_calls: int = 0
    met: bool = False
    uncovered_final: list[dict] = field(default_factory=list)
    violations_final: list[dict] = field(default_factory=list)
    soft_cases: list[str] = field(default_factory=list)
    split: dict = field(default_factory=dict)
    soft_dest: str = ""
    hard_total: int = 0
    hard_covered: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


async def review_and_fix(
    *, workspace: str | Path, milestone_id: str, docs: Mapping[str, str], packages: Iterable[str],
    allowed_symbols: set[str] | None, inventory: Inventory, hard_min: float, max_rounds: int,
    run_fix: Callable[[str], Awaitable[bool]], soft_dest: str | Path | None, record_path: str | Path | None = None,
) -> ReviewRecord:
    ws = Path(workspace)
    spec = ws / SPEC_DIR
    rec = ReviewRecord(milestone_id=milestone_id)
    pk = list(packages)
    audit = cov = None
    for rnd in range(max_rounds + 1):
        if not spec.is_dir():
            rec.rounds.append({"round": rnd, "missing_suite": True})
            break
        audit = audit_suite(spec, docs, packages=pk, allowed_symbols=allowed_symbols)
        cov = coverage(audit, inventory)
        rec.rounds.append({"round": rnd, "cases": audit.cases, "violations": len(audit.violations), "by_rule": audit.by_rule(),
                           "hard_covered": cov.hard_covered, "hard_total": cov.hard_total,
                           "by_kind": {k: list(v) for k, v in cov.by_kind.items()}})
        ok = audit.ok and cov.hard_ratio >= hard_min
        if ok:
            rec.met = True
            break
        if rnd == max_rounds:
            break
        text = "\n\n".join(t for t in (audit.to_author_text(), cov.uncovered_text()) if t)
        rec.fix_calls += 1
        if not await run_fix(text):
            rec.rounds[-1]["fix_failed"] = True
            break
    if audit is not None and cov is not None:
        rec.uncovered_final = [{"item_id": i.item_id, "kind": i.kind, "symbol": i.symbol} for i in cov.uncovered]
        rec.violations_final = [v.to_dict() for v in audit.violations]
        rec.hard_total, rec.hard_covered = cov.hard_total, cov.hard_covered
        rec.soft_cases = soft_cases(audit, cov, inventory)
        if rec.soft_cases and soft_dest is not None:
            tmp = ws / SOFT_DIR
            if tmp.exists():
                shutil.rmtree(tmp)
            rec.split = split_soft(spec, tmp, rec.soft_cases)
            dest = Path(soft_dest)
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), str(dest))      # out of the workspace before any later node runs
            rec.soft_dest = str(dest)
    if record_path is not None:
        Path(record_path).parent.mkdir(parents=True, exist_ok=True)
        Path(record_path).write_text(json.dumps(rec.to_dict(), indent=1), encoding="utf-8")
    return rec


__all__ = ["Coverage", "ReviewRecord", "SOFT_DIR", "SPEC_DIR", "coverage", "review_and_fix", "soft_cases", "split_soft"]
