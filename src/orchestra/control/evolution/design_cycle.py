"""The design cycle (self-evolution spec §9): between tasks, never inside one.

Steps, in the spec's order: statistics update (§8.2 shrinkage ranking),
promotions and demotions of playbook rows, memory replay when the ranking
changed, re-runs from the bank for trial rows and trial first-pass entries
with their §7.4 verdicts, evolver proposals, the sealed held-out tripwire,
and a versioned publish (or a report only). Every version is immutable;
``rollback`` republishes an older one as a new version with the rows that
had just been promoted demoted to candidate (§9.3).
"""

from __future__ import annotations

import json
import re
import shutil
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestra.control.evolution.bank import Bank
from orchestra.control.evolution.evolver import (
    Proposals,
    combinations_worth_preventing,
    propose,
    render_prompt,
    unresolved_pool,
)
from orchestra.control.evolution.ledger import LEDGER_ROOT, read_jsonl
from orchestra.control.evolution.memory_replay import (
    RowClassStats,
    class_distribution,
    f_calibration,
    pair_statistics,
    ranking_from_stats,
    simulate_ranking,
)
from orchestra.control.evolution.rerun import (
    RERUN_ROOT,
    FVerdict,
    RerunJob,
    f_entry_verdict,
    launch,
    paired_case_stats,
    plan_f_reruns,
    plan_row_reruns,
)
from orchestra.control.evolution.tripwire import check_tripwire
from orchestra.control.evolution.validators import (
    available_roles,
    available_templates,
    training_identifiers,
)
from orchestra.control.fast_loop.playbook_v2 import (
    RANKING_PATH,
    REPAIR_TABLE_PATH,
    V2_DIR,
    PlaybookRow,
    load_repair_table,
    save_repair_table,
    transition,
)
from orchestra.control.first_pass.designer import (
    DEFAULT_THRESHOLDS,
    FIRST_PASS_TABLE_PATH,
    FEntry,
    load_first_pass_table,
    matched_entries,
    save_first_pass_table,
)

VERSIONS_DIR = V2_DIR / "versions"
CURRENT_FILE = VERSIONS_DIR / "current"
CYCLES_ROOT = Path("outputs") / "evolution" / "cycles"
ROUTING_RECORDS = Path("outputs") / "evolution" / "routing_records.jsonl"


# --------------------------------------------------------------------------- configuration (§10)


@dataclass(frozen=True)
class CycleConfig:
    n0: int = 3
    m: int = 5
    max_trial_concurrent: int = 2
    reps: int = 2
    max_runs_per_cycle: int = 40
    concurrency: int = 2
    f_success_share: float = 0.3
    cost_tolerance: float = 0.15
    m_f: int = 5
    batch_tasks: int = 6
    buffer_failures: int = 12
    max_proposals: int = 3
    tripwire_tolerance: float = 2.0
    min_class_samples: int = 5
    thresholds: tuple[tuple[str, float], ...] = tuple(DEFAULT_THRESHOLDS.items())
    split_file: str = ""

    @property
    def thr(self) -> dict[str, float]:
        return dict(self.thresholds)

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> CycleConfig:
        cfg = dict(config or {})
        g = lambda block, key, default: (cfg.get(block) or {}).get(key, default)  # noqa: E731
        thr = dict(DEFAULT_THRESHOLDS)
        thr.update({str(k): float(v) for k, v in (cfg.get("thr") or {}).items()})
        return cls(
            n0=int(g("ranking", "n0", 3)), m=int(g("promotion", "m", 5)),
            max_trial_concurrent=int(g("trial", "max_concurrent", 2)),
            reps=int(g("rerun", "reps", 2)), max_runs_per_cycle=int(g("rerun", "max_runs_per_cycle", 40)),
            concurrency=int(g("rerun", "concurrency", 2)), f_success_share=float(g("rerun", "f_success_share", 0.3)),
            cost_tolerance=float(g("rerun", "cost_tolerance", 0.15)), m_f=int(g("rerun", "m_f", 5)),
            batch_tasks=int(g("cycle", "batch_tasks", 6)), buffer_failures=int(g("cycle", "buffer_failures", 12)),
            max_proposals=int(g("evolver", "max_proposals", 3)), tripwire_tolerance=float(g("tripwire", "tolerance", 2.0)),
            min_class_samples=int(g("replay", "min_class_samples", 5)),
            thresholds=tuple(thr.items()), split_file=str(g("evolution", "split_file", "") or ""),
        )


# --------------------------------------------------------------------------- versions (§9.2 step 7, §9.3)


def _version_number(name: str) -> int:
    m = re.fullmatch(r"v(\d+)", str(name).strip())
    return int(m.group(1)) if m else 0


def current_version(versions_dir: Path = VERSIONS_DIR) -> str:
    f = Path(versions_dir) / "current"
    if f.is_file():
        return f.read_text(encoding="utf-8").strip() or "v1"
    return "v1"


def next_version(versions_dir: Path = VERSIONS_DIR) -> str:
    existing = [_version_number(p.name) for p in Path(versions_dir).glob("v*") if p.is_dir()]
    return f"v{(max(existing) if existing else _version_number(current_version(versions_dir))) + 1}"


def publish_version(
    rows: Iterable[PlaybookRow], entries: Iterable[FEntry], ranking: Mapping[str, Mapping[str, float]], changes: Mapping[str, Any],
    *, version: str, versions_dir: Path = VERSIONS_DIR, repair_path: Path = REPAIR_TABLE_PATH,
    first_pass_path: Path = FIRST_PASS_TABLE_PATH, ranking_path: Path = RANKING_PATH,
) -> Path:
    """Write the immutable version directory, then point the live tables and ``current`` at it."""
    vdir = Path(versions_dir) / version
    if vdir.exists():
        raise FileExistsError(f"version {version} already exists; versions are immutable")
    vdir.mkdir(parents=True)
    save_repair_table(rows, vdir / "repair.yaml", version=version)
    save_first_pass_table(entries, vdir / "first_pass.yaml", version=version)
    (vdir / "ranking.json").write_text(json.dumps({"version": version, "scores": ranking}, indent=1, sort_keys=True), encoding="utf-8")
    (vdir / "changes.json").write_text(json.dumps(dict(changes), indent=1, default=str), encoding="utf-8")
    for name, target in (("repair.yaml", repair_path), ("first_pass.yaml", first_pass_path), ("ranking.json", ranking_path)):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vdir / name, target)
    (Path(versions_dir) / "current").write_text(version + "\n", encoding="utf-8")
    return vdir


def load_version(version: str, versions_dir: Path = VERSIONS_DIR) -> tuple[tuple[PlaybookRow, ...], tuple[FEntry, ...], dict[str, Any], dict[str, Any]]:
    vdir = Path(versions_dir) / version
    rows = load_repair_table(vdir / "repair.yaml")
    entries = load_first_pass_table(vdir / "first_pass.yaml")
    ranking = json.loads((vdir / "ranking.json").read_text(encoding="utf-8")).get("scores") or {}
    changes = json.loads((vdir / "changes.json").read_text(encoding="utf-8")) if (vdir / "changes.json").is_file() else {}
    return rows, entries, ranking, changes


def rollback(
    to_version: str, *, versions_dir: Path = VERSIONS_DIR, reason: str = "", **paths: Path,
) -> tuple[str, dict[str, Any]]:
    """§9.3: republish ``to_version`` as a new version; rows/entries the later versions promoted go back to candidate."""
    cur = current_version(versions_dir)
    rows, entries, ranking, _ = load_version(to_version, versions_dir)
    demoted_rows: set[str] = set()
    demoted_entries: set[str] = set()
    for p in sorted(Path(versions_dir).glob("v*"), key=lambda p: _version_number(p.name)):
        if _version_number(to_version) < _version_number(p.name) <= _version_number(cur):
            _, _, _, changes = load_version(p.name, versions_dir)
            demoted_rows.update(str(x) for x in (changes.get("promoted_rows") or ()))
            demoted_entries.update(str(x) for x in (changes.get("promoted_entries") or ()))
    new_rows = tuple(replace(r, state="candidate") if r.row_id in demoted_rows and r.state == "active" else r for r in rows)
    new_entries = tuple(replace(e, state="candidate") if e.entry_id in demoted_entries and e.state == "active" else e for e in entries)
    version = next_version(versions_dir)
    changes = {"rollback_of": cur, "restored": to_version, "reason": reason, "demoted_rows": sorted(demoted_rows),
               "demoted_entries": sorted(demoted_entries), "promoted_rows": [], "promoted_entries": [],
               "at": datetime.now(UTC).isoformat()}
    publish_version(new_rows, new_entries, ranking, changes, version=version, versions_dir=versions_dir, **paths)
    return version, changes


def batch_regressed(prev_records: Iterable[Mapping[str, Any]], new_records: Iterable[Mapping[str, Any]], *, m: int = 5,
                    drop: float = 1.0) -> tuple[bool, dict[str, Any]]:
    """§9.3 trigger: per-milestone net fix of committed candidates, by milestone kind, fell by more than ``drop``."""
    def by_kind(records):
        out: dict[str, list[float]] = defaultdict(list)
        for r in records:
            if r.get("committed") and r.get("net_fix") is not None:
                out[str((r.get("features") or {}).get("kind") or "?")].append(float(r["net_fix"]))
        return out
    prev, new = by_kind(prev_records), by_kind(new_records)
    detail = {}
    fired = False
    for kind in set(prev) & set(new):
        if len(prev[kind]) >= m and len(new[kind]) >= m:
            a = sum(prev[kind]) / len(prev[kind])
            b = sum(new[kind]) / len(new[kind])
            detail[kind] = {"prev": round(a, 3), "new": round(b, 3), "n_prev": len(prev[kind]), "n_new": len(new[kind])}
            if a - b > drop:
                fired = True
    return fired, detail


# --------------------------------------------------------------------------- ledger access


def load_candidate_records(ledger_root: Path = LEDGER_ROOT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(Path(ledger_root).glob("*/candidates.jsonl")):
        for r in read_jsonl(f):
            r = dict(r)
            r.setdefault("source", "rerun" if str(r.get("run_dir") or "").startswith(str(RERUN_ROOT)) else "online")
            out.append(r)
    return out


def load_milestone_records(ledger_root: Path = LEDGER_ROOT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(Path(ledger_root).glob("*/milestones.jsonl")):
        out.extend(dict(r) for r in read_jsonl(f))
    return out


def should_trigger(milestone_records: Iterable[Mapping[str, Any]], *, seen_tasks: Iterable[str], new_failures: int,
                   cfg: CycleConfig) -> tuple[bool, str]:
    """§9.1: a finished batch of training tasks, or enough new failed milestones in the bank buffer."""
    seen = set(seen_tasks)
    tasks = {str(r.get("task_id")) for r in milestone_records if str(r.get("split") or "train") == "train"} - seen
    if len(tasks) >= cfg.batch_tasks:
        return True, f"{len(tasks)} new training tasks >= batch_tasks={cfg.batch_tasks}"
    if new_failures >= cfg.buffer_failures:
        return True, f"{new_failures} new failed milestones >= buffer_failures={cfg.buffer_failures}"
    return False, f"{len(tasks)} new tasks, {new_failures} new failures"


# --------------------------------------------------------------------------- promotions (§9.2 step 2)


@dataclass
class RowChange:
    row_id: str
    before: str
    after: str
    reason: str


def _classes_of(row: PlaybookRow, stats: Mapping[tuple[str, str], RowClassStats]) -> list[str]:
    if "*" in row.error_classes:
        return sorted({cls for (rid, cls) in stats if rid == row.row_id})
    return list(row.error_classes)


def _evolver_cycle(origin: str) -> int:
    m = re.search(r"evolver:(?:cycle-)?(\d+)", origin or "")
    return int(m.group(1)) if m else -1


def update_row_states(
    rows: Iterable[PlaybookRow], stats: Mapping[tuple[str, str], RowClassStats], *, cfg: CycleConfig,
) -> tuple[list[PlaybookRow], list[RowChange]]:
    """trial -> active / candidate / retired, active -> candidate, then candidates into free trial slots."""
    out: list[PlaybookRow] = []
    changes: list[RowChange] = []
    for row in rows:
        if row.table != "repair":
            out.append(row)
            continue
        new = row
        if row.state == "trial":
            judged = [stats[(row.row_id, c)] for c in _classes_of(row, stats) if (row.row_id, c) in stats and stats[(row.row_id, c)].n >= cfg.m]
            if judged:
                passing = [s for s in judged if s.score(cfg.n0) > 0 and s.mean_regressions() <= s.mean_r0_regressions()]
                if passing:
                    new = transition(row, "pass")
                    changes.append(RowChange(row.row_id, row.state, new.state,
                                             f"{passing[0].error_class}: {passing[0].n} pairs, score {passing[0].score(cfg.n0):.2f} > 0, regressions {passing[0].mean_regressions():.2f} <= R0 {passing[0].mean_r0_regressions():.2f}"))
                else:
                    new = transition(row, "fail")
                    s = judged[0]
                    changes.append(RowChange(row.row_id, row.state, new.state,
                                             f"{s.error_class}: {s.n} pairs, score {s.score(cfg.n0):.2f} <= 0 or regressions above R0"))
        elif row.state == "active":
            recent = [(c, stats[(row.row_id, c)]) for c in _classes_of(row, stats) if (row.row_id, c) in stats and stats[(row.row_id, c)].n > 0]
            bad = [(c, s) for c, s in recent if s.recent_score(2 * cfg.m, cfg.n0) < 0]
            if bad and len(bad) == len(recent):
                new = transition(row, "fail")
                c, s = bad[0]
                changes.append(RowChange(row.row_id, row.state, new.state, f"{c}: recent score {s.recent_score(2 * cfg.m, cfg.n0):.2f} < 0 over the last {2 * cfg.m} pairs"))
        out.append(new)
    # candidates into trial, by error class, newest origin first, within max_concurrent trials overall
    trials = sum(1 for r in out if r.table == "repair" and r.state == "trial")
    by_class: dict[str, list[PlaybookRow]] = defaultdict(list)
    for r in out:
        if r.table == "repair" and r.state == "candidate":
            for c in (r.error_classes or ("*",)):
                by_class[c].append(r)
    promoted: set[str] = set()
    for cls in sorted(by_class):
        if trials >= cfg.max_trial_concurrent:
            break
        if any(r.state == "trial" and r.applies_to(cls) for r in out):
            continue
        pool = sorted(by_class[cls], key=lambda r: (-_evolver_cycle(r.origin), r.row_id))
        pick = next((r for r in pool if r.row_id not in promoted), None)
        if pick is None:
            continue
        idx = next(i for i, r in enumerate(out) if r.row_id == pick.row_id)
        out[idx] = transition(pick, "select")
        promoted.add(pick.row_id)
        trials += 1
        changes.append(RowChange(pick.row_id, "candidate", "trial", f"{cls}: free trial slot, origin {pick.origin or 'new'}"))
    return out, changes


# --------------------------------------------------------------------------- first-pass entries (§7.3, §7.4)


def _final_record(records: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    committed = [r for r in records if r.get("committed")]
    if committed:
        return committed[-1]
    inc = [r for r in records if r.get("candidate_kind") == "incumbent"]
    return inc[-1] if inc else (records[-1] if records else None)


def evaluate_f_entry(
    entry: FEntry, candidate_records: Iterable[Mapping[str, Any]], *, cfg: CycleConfig, tripwire_fired: bool = False,
) -> tuple[FVerdict, dict[str, Any]]:
    """§7.4 from paired runs: milestones run once with the entry and once with F0 (online or re-run)."""
    runs: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in candidate_records:
        if str(r.get("split") or "train") != "train":
            continue
        applied = [str(x) for x in (r.get("f_entries_applied") or ())]
        variant = "F" if entry.entry_id in applied else ("F0" if not applied or applied == ["F0"] else None)
        if variant is None:
            continue
        runs[(str(r.get("task_id")), str(r.get("milestone_id")), variant)].append(r)
    milestones = {(t, m) for (t, m, v) in runs}
    paired = [(t, m) for (t, m) in sorted(milestones) if (t, m, "F") in runs and (t, m, "F0") in runs]
    targets = set(entry.predicted_error_classes)
    control_cases, variant_cases = [], []
    cost_c = cost_v = 0.0
    before = after = 0
    for t, m in paired:
        fc = _final_record(runs[(t, m, "F0")])
        fv = _final_record(runs[(t, m, "F")])
        if fc is None or fv is None:
            continue
        control_cases.append(dict(fc.get("per_case_results") or {}))
        variant_cases.append(dict(fv.get("per_case_results") or {}))
        cost_c += sum(float((r.get("cost") or {}).get("usd") or 0.0) for r in runs[(t, m, "F0")])
        cost_v += sum(float((r.get("cost") or {}).get("usd") or 0.0) for r in runs[(t, m, "F")])
        inc_c = next((r for r in runs[(t, m, "F0")] if r.get("candidate_kind") == "incumbent"), None)
        inc_v = next((r for r in runs[(t, m, "F")] if r.get("candidate_kind") == "incumbent"), None)
        before += sum(int(n) for c, n in ((inc_c or {}).get("actual_error_classes") or {}).items() if c in targets)
        after += sum(int(n) for c, n in ((inc_v or {}).get("actual_error_classes") or {}).items() if c in targets)
    stats = paired_case_stats(control_cases, variant_cases) if paired else {"net": 0, "stable_fixes": [], "stable_regressions": []}
    verdict = f_entry_verdict(net_effect=int(stats.get("net", 0)), cost_variant=cost_v, cost_control=cost_c, target_before=before,
                              target_after=after, checkpoints=len(paired), m_f=cfg.m_f, cost_tolerance=cfg.cost_tolerance,
                              tripwire_fired=tripwire_fired)
    return verdict, {"paired": len(paired), "net": stats.get("net"), "cost_variant": cost_v, "cost_control": cost_c,
                     "target_before": before, "target_after": after}


# --------------------------------------------------------------------------- the cycle


@dataclass
class CycleResult:
    cycle_id: str
    version: str | None
    published: bool
    report_path: str
    ranking: dict[str, dict[str, float]] = field(default_factory=dict)
    row_changes: list[RowChange] = field(default_factory=list)
    entry_changes: list[dict[str, Any]] = field(default_factory=list)
    rerun_jobs: list[RerunJob] = field(default_factory=list)
    proposals: Proposals | None = None
    tripwire: list[dict[str, Any]] = field(default_factory=list)
    replay: dict[str, Any] = field(default_factory=dict)


def _write_routing_record(record: Mapping[str, Any], path: Path = ROUTING_RECORDS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as h:
        h.write(json.dumps(dict(record)) + "\n")


def run_cycle(
    cfg: CycleConfig, *, cycle_id: str | None = None, ledger_root: Path = LEDGER_ROOT, bank_root: Path | None = None,
    versions_dir: Path = VERSIONS_DIR, cycles_root: Path = CYCLES_ROOT, publish: bool = False, launch_reruns: bool = False,
    run_evolver: bool = False, evolver_call: Callable[[str, str], str] | None = None, rerun_config: str = "",
    sealed_path: Path | None = None, routing_path: Path = ROUTING_RECORDS, seen_tasks: Iterable[str] = (),
    table_paths: Mapping[str, Path] | None = None,
) -> CycleResult:
    cycle_id = cycle_id or datetime.now(UTC).strftime("cycle-%Y%m%dT%H%M%SZ")
    out_dir = Path(cycles_root) / cycle_id
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = dict(table_paths or {})
    rows_before = load_repair_table(paths.get("repair_path"))
    entries_before = load_first_pass_table(paths.get("first_pass_path"))
    cand = load_candidate_records(ledger_root)
    miles = load_milestone_records(ledger_root)
    bank = Bank(bank_root)
    bank_entries = bank.entries()
    lines: list[str] = [f"# Design cycle {cycle_id}", "", f"candidate records: {len(cand)}, milestone records: {len(miles)}, bank entries: {len(bank_entries)}", ""]

    # 1. statistics
    stats = pair_statistics(cand, training_only=True)
    ranking = ranking_from_stats(stats, n0=cfg.n0)
    old_ranking = json.loads(Path(paths.get("ranking_path") or RANKING_PATH).read_text(encoding="utf-8")).get("scores") or {} \
        if Path(paths.get("ranking_path") or RANKING_PATH).is_file() else {}
    lines += ["## 1. Ranking (shrinkage score vs R0)", "```json", json.dumps(ranking, indent=1, sort_keys=True), "```", ""]
    dist = class_distribution(miles or cand, min_class_samples=cfg.min_class_samples)
    lines += ["## Error-class distribution (§8.1)", "```json", json.dumps(dist, indent=1), "```", ""]

    # 2. promotions / demotions
    rows, row_changes = update_row_states(rows_before, stats, cfg=cfg)
    lines += ["## 2. Row state changes"] + ([f"- {c.row_id}: {c.before} -> {c.after} ({c.reason})" for c in row_changes] or ["- none"]) + [""]

    # 3. memory replay when the ranking changed
    replay: dict[str, Any] = {}
    if ranking != old_ranking:
        table_rows: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            if r.table == "repair" and r.state in ("active", "trial"):
                for c in r.error_classes:
                    table_rows[c].append(r.row_id)
        replay["ranking"] = simulate_ranking(cand, old_ranking=old_ranking, new_ranking=ranking, table_rows=table_rows)
    replay["f_calibration"] = f_calibration(miles, entries_before, matched=lambda feats, e: bool(matched_entries(feats, [e], cfg.thr)))
    lines += ["## 3. Memory replay", "```json", json.dumps(replay, indent=1, default=str), "```", ""]
    if replay.get("ranking", {}).get("verdict") == "insufficient evidence":
        lines.append("ranking simulation: insufficient evidence; the new scores are published as statistics, the selection rule is unchanged.")
        lines.append("")

    # 4. re-runs for trial rows and trial F entries
    jobs: list[RerunJob] = []
    budget = cfg.max_runs_per_cycle
    for r in rows:
        if r.table != "repair" or r.state != "trial":
            continue
        for c in (r.error_classes if "*" not in r.error_classes else sorted(bank.class_counts())):
            have = stats[(r.row_id, c)].n if (r.row_id, c) in stats else 0
            if have >= cfg.m or budget <= 0:
                continue
            plan = plan_row_reruns(bank_entries, row_id=r.row_id, error_class=c, reps=cfg.reps, needed=cfg.m - have, max_runs=budget)
            jobs += plan.jobs
            budget -= plan.runs
    entry_changes: list[dict[str, Any]] = []
    for e in entries_before:
        if e.state != "trial" or budget <= 0:
            continue
        verdict, detail = evaluate_f_entry(e, cand, cfg=cfg)
        if verdict.checkpoints < cfg.m_f:
            plan = plan_f_reruns(bank_entries, entry_id=e.entry_id, matches=lambda feats, e=e: bool(matched_entries(feats, [e], cfg.thr)),
                                 reps=1, needed=cfg.m_f - verdict.checkpoints, max_runs=budget, success_share=cfg.f_success_share)
            jobs += plan.jobs
            budget -= plan.runs
    lines += ["## 4. Re-runs", f"planned: {len(jobs)} run(s) (cap {cfg.max_runs_per_cycle})"]
    lines += [f"- {j.run_id}: {j.task_id}/{j.milestone_id} {j.variant} vs {j.control} rep {j.rep}" for j in jobs[:60]] + [""]
    if launch_reruns and jobs:
        entries_by_id = {e.entry_id: e for e in bank_entries}
        results = launch(jobs, entries_by_id, config=rerun_config, concurrency=cfg.concurrency)
        (out_dir / "rerun_results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
        cand = load_candidate_records(ledger_root)
        stats = pair_statistics(cand, training_only=True)
        ranking = ranking_from_stats(stats, n0=cfg.n0)
        rows, row_changes = update_row_states(rows_before, stats, cfg=cfg)
        lines += ["re-runs launched; statistics and row states recomputed:"] + [f"- {c.row_id}: {c.before} -> {c.after} ({c.reason})" for c in row_changes] + [""]
    entries = list(entries_before)
    for i, e in enumerate(entries):
        if e.state != "trial":
            continue
        verdict, detail = evaluate_f_entry(e, cand, cfg=cfg)
        if verdict.promote:
            entries[i] = replace(e, state="active")
            entry_changes.append({"entry_id": e.entry_id, "before": "trial", "after": "active", **detail})
        elif verdict.checkpoints >= cfg.m_f:
            entries[i] = replace(e, state="candidate" if not e.origin.endswith("#trial2") else "retired",
                                 origin=e.origin if e.origin.endswith("#trial2") else e.origin + "#trial2")
            entry_changes.append({"entry_id": e.entry_id, "before": "trial", "after": entries[i].state, "reasons": verdict.reasons, **detail})
        else:
            entry_changes.append({"entry_id": e.entry_id, "before": "trial", "after": "trial", "reasons": verdict.reasons, **detail})
    lines += ["## F entry verdicts (§7.4)"] + ([f"- {c}" for c in entry_changes] or ["- none in trial"]) + [""]

    # 5. evolver proposals
    proposals: Proposals | None = None
    if run_evolver:
        identifiers = training_identifiers(
            task_ids=[e.task_id for e in bank_entries] + [str(r.get("task_id")) for r in miles if str(r.get("split") or "train") == "train"],
            milestone_ids=[e.milestone_id for e in bank_entries],
            case_names=[c for e in bank_entries for c in e.persistent_failures] + [c for r in cand for c in (r.get("persistent_before") or ())],
            focus_paths=[p for e in bank_entries for p in ((e.milestone_spec or {}).get("focus_paths") or ())],
        )
        roles, templates = available_roles(), available_templates()
        summary = {f"{rid}/{c}": {"n": s.n, "score": round(s.score(cfg.n0), 3), "mean_cost": round(s.mean_cost(), 4),
                                  "mean_regressions": round(s.mean_regressions(), 2)} for (rid, c), s in sorted(stats.items())}
        prompt = render_prompt(
            ranking=ranking, stats_summary=summary, clusters=unresolved_pool(cand),
            combinations=combinations_worth_preventing(ranking, miles), repair_rows=[r.to_dict() for r in rows if r.table == "repair"],
            f_entries=[e.to_dict() for e in entries], roles=roles, templates=templates, max_proposals=cfg.max_proposals,
        )
        proposals = propose(prompt=prompt, out_dir=out_dir, cycle_id=cycle_id, roles=roles, templates=templates, identifiers=identifiers,
                            repair_row_ids=[r.row_id for r in rows], f_entry_ids=[e.entry_id for e in entries],
                            max_proposals=cfg.max_proposals, call=evolver_call)
        for d in proposals.rows:
            rows.append(PlaybookRow.from_dict(d))
        for d in proposals.f_entries:
            entries.append(FEntry.from_dict(d))
        lines += ["## 5. Evolver proposals", f"accepted rows: {[r['row_id'] for r in proposals.rows]}",
                  f"accepted entries: {[e['entry_id'] for e in proposals.f_entries]}", f"rejected: {json.dumps(proposals.rejected, indent=1)}", ""]
    else:
        lines += ["## 5. Evolver", "not run this cycle", ""]

    # 6. tripwire on this cycle's promotions
    tripwire: list[dict[str, Any]] = []
    promoted_rows = [c.row_id for c in row_changes if c.after == "active"]
    by_id = {str(r.get("record_id")): r for r in cand}
    for rid in promoted_rows:
        rec_ids = [x for (r, _c), s in stats.items() if r == rid for x in s.record_ids]
        base_ids = [str(by_id[x].get("paired_R0_record_id") or "") for x in rec_ids if x in by_id]
        gate = sum(float(by_id[x].get("delta_vs_R0") or 0.0) for x in rec_ids if x in by_id)
        fired, note = check_tripwire(promoted_record_ids=rec_ids, baseline_record_ids=[b for b in base_ids if b], gate_net_fix=gate,
                                     tolerance=cfg.tripwire_tolerance, path=sealed_path)
        tripwire.append({"row_id": rid, "fired": fired, "note": note})
        if fired:
            rows = [replace(r, state="candidate") if r.row_id == rid else r for r in rows]
            row_changes.append(RowChange(rid, "active", "candidate", f"tripwire: {note}"))
            _write_routing_record({"route": "suite_suspect", "entry_id": rid, "cycle_id": cycle_id, "note": note}, routing_path)
    for ch in [c for c in entry_changes if c.get("after") == "active"]:
        eid = ch["entry_id"]
        var_ids = [str(r.get("record_id")) for r in cand if eid in (r.get("f_entries_applied") or ()) and r.get("committed")]
        base_ids = [str(r.get("record_id")) for r in cand if not [x for x in (r.get("f_entries_applied") or ()) if x != "F0"] and r.get("committed")
                    and any((r.get("task_id"), r.get("milestone_id")) == (by_id[v].get("task_id"), by_id[v].get("milestone_id")) for v in var_ids if v in by_id)]
        fired, note = check_tripwire(promoted_record_ids=var_ids, baseline_record_ids=base_ids, gate_net_fix=float(ch.get("net") or 0),
                                     tolerance=cfg.tripwire_tolerance, path=sealed_path)
        tripwire.append({"entry_id": eid, "fired": fired, "note": note})
        if fired:
            entries = [replace(e, state="candidate") if e.entry_id == eid else e for e in entries]
            ch["after"] = "candidate"
            ch["tripwire"] = note
            _write_routing_record({"route": "suite_suspect", "entry_id": eid, "cycle_id": cycle_id, "note": note}, routing_path)
    lines += ["## 6. Tripwire"] + ([f"- {t}" for t in tripwire] or ["- nothing promoted this cycle"]) + [""]

    # 7. publish or report
    changes = {
        "cycle_id": cycle_id, "at": datetime.now(UTC).isoformat(),
        "row_changes": [c.__dict__ for c in row_changes], "entry_changes": entry_changes,
        "promoted_rows": [c.row_id for c in row_changes if c.after == "active"],
        "promoted_entries": [c["entry_id"] for c in entry_changes if c.get("after") == "active"],
        "proposals": {"rows": [r["row_id"] for r in proposals.rows], "entries": [e["entry_id"] for e in proposals.f_entries],
                      "rejected": proposals.rejected} if proposals else {},
        "tripwire": tripwire, "reruns_planned": len(jobs), "replay": replay,
    }
    proposed = out_dir / "proposed"
    proposed.mkdir(exist_ok=True)
    save_repair_table(rows, proposed / "repair.yaml", version="proposed")
    save_first_pass_table(entries, proposed / "first_pass.yaml", version="proposed")
    (proposed / "ranking.json").write_text(json.dumps({"scores": ranking}, indent=1, sort_keys=True), encoding="utf-8")
    (proposed / "changes.json").write_text(json.dumps(changes, indent=1, default=str), encoding="utf-8")
    version = None
    if publish:
        version = next_version(versions_dir)
        publish_version(rows, entries, ranking, changes, version=version, versions_dir=versions_dir,
                        **{k: v for k, v in paths.items() if k in ("repair_path", "first_pass_path", "ranking_path")})
        lines += ["## 7. Published", f"version {version} -> {Path(versions_dir) / version}", ""]
    else:
        lines += ["## 7. Report only", f"proposed tables written to {proposed}; nothing published", ""]
    report = out_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return CycleResult(cycle_id=cycle_id, version=version, published=publish, report_path=str(report), ranking=ranking,
                       row_changes=row_changes, entry_changes=entry_changes, rerun_jobs=jobs, proposals=proposals, tripwire=tripwire, replay=replay)


__all__ = [
    "CURRENT_FILE", "CYCLES_ROOT", "CycleConfig", "CycleResult", "ROUTING_RECORDS", "RowChange", "VERSIONS_DIR",
    "batch_regressed", "current_version", "evaluate_f_entry", "load_candidate_records", "load_milestone_records",
    "load_version", "next_version", "publish_version", "rollback", "run_cycle", "should_trigger", "update_row_states",
]
