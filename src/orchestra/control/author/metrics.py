"""Author-suite metrics of the author-evolution spec §6, as pure functions.

Nothing here opens the held-out suite: the sealed scripts (``scripts/sealed``) hand in
case ids, pass/fail, labels and symbol names, and these functions only count. The
inputs for one milestone:

* ``suite``: the evaluation suite's cases with their tier and the project symbols each
  case reaches (from the suite's own source), and the cases the reference fails;
* ``suite_results``: ``{workspace: {case: pass|fail}}`` of the suite on the retained
  workspaces, one of which is the final repository;
* ``heldout``: ``{workspace: {case: pass|fail}}`` of the attributed held-out cases,
  with each held-out case's label (documented / undocumented / unknown), its symbols and
  whether the inventory missed its sentence.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field


@dataclass
class SuiteCase:
    case_id: str
    tier: str = "hard"                       # hard | soft
    symbols: frozenset[str] = frozenset()
    citations: tuple[str, ...] = ()          # normalised quoted sentences


@dataclass
class HeldoutCase:
    case_id: str
    label: str = "unknown"                   # documented | undocumented | unknown
    symbols: frozenset[str] = frozenset()
    inventory_miss: bool | None = None       # None: no inventory to compare with
    depth: str = "unknown"                   # detail_specified | behaviour_only | not_specified | unknown
    paragraphs: tuple[int, ...] = ()         # document paragraphs the case rests on (indices)


@dataclass
class MilestoneInputs:
    task: str
    milestone: str
    final: str                                           # workspace key of the final repository
    suite: list[SuiteCase]
    reference_failed: frozenset[str]
    suite_results: Mapping[str, Mapping[str, str]]       # workspace -> case -> pass|fail
    heldout_cases: list[HeldoutCase]
    heldout_results: Mapping[str, Mapping[str, str]]     # workspace -> held-out case -> pass|fail
    inventory_hard_total: int | None = None
    inventory_hard_covered: int | None = None
    cost: Mapping[str, float] = field(default_factory=dict)
    audit_violations: int = 0
    doc_paragraphs: tuple[str, ...] = ()                 # normalised document paragraphs


def _match(sym: str, pool: Iterable[str]) -> bool:
    leaf = sym.split(".")[-1]
    for p in pool:
        if p == sym or p.split(".")[-1] == leaf:
            return True
    return False


def _public(symbols: Iterable[str]) -> set[str]:
    return {s for s in symbols if not any(part.startswith("_") for part in s.split("."))}


def s_prime(m: MilestoneInputs) -> list[SuiteCase]:
    """S': hard cases the reference passes."""
    return [c for c in m.suite if c.tier == "hard" and c.case_id not in m.reference_failed]


def failing(results: Mapping[str, str], ids: Iterable[str]) -> set[str]:
    return {i for i in ids if results.get(i) == "fail"}


def milestone_metrics(m: MilestoneInputs) -> dict:
    sp = s_prime(m)
    sp_ids = {c.case_id for c in sp}
    by_id = {c.case_id: c for c in m.suite}
    final_suite = m.suite_results.get(m.final, {})
    final_held = m.heldout_results.get(m.final, {})
    held_by_id = {h.case_id: h for h in m.heldout_cases}
    f_doc = [h for h in m.heldout_cases if h.label == "documented" and final_held.get(h.case_id) == "fail"]
    f_undoc = [h for h in m.heldout_cases if h.label == "undocumented" and final_held.get(h.case_id) == "fail"]
    sp_fail = failing(final_suite, sp_ids)
    fail_syms = set().union(*[by_id[c].symbols for c in sp_fail]) if sp_fail else set()

    out: dict = {"task": m.task, "milestone": m.milestone, "suite_cases": len(m.suite), "s_prime": len(sp),
                 "f_doc": len(f_doc), "f_undoc": len(f_undoc),
                 # how many valid hard cases fail on the final repository, and whether the suite catches
                 # the milestone at all where documented held-out cases fail (cell-level miss)
                 "s_prime_fail_final": len(sp_fail),
                 "cell_miss": (None if not f_doc else (0.0 if sp_fail else 1.0))}
    # 1-2 symbol coverage and true miss
    doc_syms = _public(set().union(*[h.symbols for h in f_doc])) if f_doc else set()
    if f_doc and doc_syms:
        covered = {s for s in doc_syms if _match(s, fail_syms)}
        out["symbol_coverage"] = round(len(covered) / len(doc_syms), 4)
        out["true_miss"] = round(1 - len(covered) / len(doc_syms), 4)
        out["uncovered_symbols"] = sorted(doc_syms - covered)
    else:
        out["symbol_coverage"] = out["true_miss"] = None
    # 3 ceiling share
    denom = len(f_doc) + len(f_undoc)
    out["ceiling_share"] = round(len(f_undoc) / denom, 4) if denom else None
    # 4 reference-fail rate, hard and soft
    for tier in ("hard", "soft"):
        ids = [c.case_id for c in m.suite if c.tier == tier]
        out[f"ref_fail_rate_{tier}"] = round(sum(1 for i in ids if i in m.reference_failed) / len(ids), 4) if ids else None
    # 5 false positives: S' failures on the final repo whose symbols' held-out cases all pass
    fp = judged = 0
    for cid in sp_fail:
        syms = _public(by_id[cid].symbols)
        touching = [h for h in m.heldout_cases if h.symbols and any(_match(s, h.symbols) for s in syms)]
        if not touching:
            continue
        judged += 1
        if all(final_held.get(h.case_id) == "pass" for h in touching):
            fp += 1
    out["false_positive_rate"] = round(fp / judged, 4) if judged else None
    out["false_positive_judged"] = judged
    # 6 discrimination over retained workspaces with held-out results
    out["discrimination"], out["discrimination_pairs"] = discrimination(m, sp_ids)
    # 8 inventory hard coverage, 9 inventory gap rate
    if m.inventory_hard_total:
        out["inventory_coverage_hard"] = round((m.inventory_hard_covered or 0) / m.inventory_hard_total, 4)
    else:
        out["inventory_coverage_hard"] = None
    judged_gap = [h for h in f_doc if h.inventory_miss is not None]
    out["inventory_gap_rate"] = round(sum(1 for h in judged_gap if h.inventory_miss) / len(judged_gap), 4) if judged_gap else None
    sc = sentence_classes(m)
    out["sentence_classes"] = {k: len(v) for k, v in sc.items()}
    hb = {h.case_id: h for h in m.heldout_cases}
    sp_syms = set().union(*[c.symbols for c in sp]) if sp else set()
    out["breadth_split"] = {
        "detail_specified": sum(1 for c in sc["breadth"] if hb[c].depth == "detail_specified"),
        "behaviour_only": sum(1 for c in sc["breadth"] if hb[c].depth == "behaviour_only"),
        # a valid hard case still reaches one of the case's public symbols: the suite tests the symbol
        # while citing another sentence (or the paragraph match is too strict)
        "symbol_reached": sum(1 for c in sc["breadth"] if any(_match(s, sp_syms) for s in _public(hb[c].symbols))),
    }
    out["breadth_inventory_listed"] = sum(1 for h in m.heldout_cases if h.case_id in set(sc["breadth"]) and h.inventory_miss is False)
    # 10-11 cost and audit, passed through
    out["cost"] = dict(m.cost)
    out["audit_violations"] = m.audit_violations
    del held_by_id
    return out


SENTENCE_CLASSES = ("caught", "depth", "detail_ceiling", "breadth", "ceiling", "unattributable")


def _cites(citation: str, paragraph: str) -> bool:
    return bool(citation) and bool(paragraph) and (
        (len(citation) >= 12 and citation in paragraph) or (len(paragraph) >= 12 and paragraph in citation))


def sentence_classes(m: MilestoneInputs) -> dict[str, list[str]]:
    """Every held-out case failing on the final repository, by why the suite did not catch it:

    ceiling          the documents do not state the behaviour;
    breadth          no valid hard suite case cites a paragraph the case rests on;
    depth            the suite cites it, its cases pass on the final code, and the documents give the detail;
    detail_ceiling   as depth, but the documents give the behaviour, not the asserted detail;
    caught           a valid hard suite case citing it fails on the final code;
    unattributable   no project symbol, no paragraph to rest on.
    """
    sp = {c.case_id: c for c in s_prime(m)}
    final_suite = m.suite_results.get(m.final, {})
    final_held = m.heldout_results.get(m.final, {})
    out: dict[str, list[str]] = {k: [] for k in SENTENCE_CLASSES}
    for h in m.heldout_cases:
        if final_held.get(h.case_id) != "fail":
            continue
        if h.depth == "unknown":
            out["unattributable"].append(h.case_id)
            continue
        if h.depth == "not_specified":
            out["ceiling"].append(h.case_id)
            continue
        paras = [m.doc_paragraphs[i] for i in h.paragraphs if 0 <= i < len(m.doc_paragraphs)]
        touching = [cid for cid, c in sp.items() if any(_cites(q, p) for q in c.citations for p in paras)]
        if not touching:
            out["breadth"].append(h.case_id)
        elif any(final_suite.get(cid) == "fail" for cid in touching):
            out["caught"].append(h.case_id)
        else:
            out["depth" if h.depth == "detail_specified" else "detail_ceiling"].append(h.case_id)
    return out


def discrimination(m: MilestoneInputs, sp_ids: set[str]) -> tuple[float | None, int]:
    """Share of workspace pairs with different held-out pass counts whose S' pass counts differ
    in the same direction (S' tie = 0.5)."""
    ws = [w for w in m.suite_results if w in m.heldout_results]
    held_ids = [h.case_id for h in m.heldout_cases]
    hp = {w: sum(1 for c in held_ids if m.heldout_results[w].get(c) == "pass") for w in ws}
    sp = {w: sum(1 for c in sp_ids if m.suite_results[w].get(c) == "pass") for w in ws}
    score = 0.0
    pairs = 0
    for i, a in enumerate(ws):
        for b in ws[i + 1:]:
            dh = hp[a] - hp[b]
            if dh == 0:
                continue
            pairs += 1
            ds = sp[a] - sp[b]
            score += 0.5 if ds == 0 else (1.0 if (ds > 0) == (dh > 0) else 0.0)
    return (round(score / pairs, 4) if pairs else None), pairs


def aggregate(rows: list[dict], *, exclude_tasks: Iterable[str] = ()) -> dict:
    """Means over milestones (each metric over the milestones where it is defined), plus counts."""
    ex = set(exclude_tasks)
    rs = [r for r in rows if r["task"] not in ex]
    keys = ["true_miss", "cell_miss", "symbol_coverage", "ceiling_share", "ref_fail_rate_hard", "ref_fail_rate_soft",
            "false_positive_rate", "discrimination", "inventory_coverage_hard", "inventory_gap_rate"]
    out: dict = {"milestones": len(rs), "tasks": len({r["task"] for r in rs})}
    for k in keys:
        vals = [r[k] for r in rs if r.get(k) is not None]
        out[k] = round(statistics.fmean(vals), 4) if vals else None
        out[f"{k}_n"] = len(vals)
    out["audit_violations"] = sum(int(r.get("audit_violations") or 0) for r in rs)
    pooled = {k: sum((r.get("sentence_classes") or {}).get(k, 0) for r in rs) for k in SENTENCE_CLASSES}
    total = sum(pooled.values())
    out["sentence_classes"] = pooled
    out["breadth_split"] = {k: sum((r.get("breadth_split") or {}).get(k, 0) for r in rs) for k in ("detail_specified", "behaviour_only", "symbol_reached")}
    out["breadth_inventory_listed"] = sum(int(r.get("breadth_inventory_listed") or 0) for r in rs)
    out["sentence_shares"] = {k: round(v / total, 4) for k, v in pooled.items()} if total else {}
    for ck in ("cases", "seconds", "tokens", "rounds"):
        vals = [float(r["cost"][ck]) for r in rs if ck in (r.get("cost") or {})]
        if vals:
            out[f"cost_{ck}"] = round(sum(vals), 1)
    return out


def stability(per_sample: list[float | None]) -> float | None:
    """Standard deviation of one metric across k samples (§6 item 7)."""
    vals = [v for v in per_sample if v is not None]
    return round(statistics.pstdev(vals), 4) if len(vals) >= 2 else None


__all__ = ["SENTENCE_CLASSES", "sentence_classes", "HeldoutCase", "MilestoneInputs", "SuiteCase", "aggregate", "discrimination", "milestone_metrics", "s_prime", "stability"]
