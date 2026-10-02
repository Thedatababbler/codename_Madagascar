"""The ledger (self-evolution spec §5): one record per candidate, one per milestone.

Written when a milestone's search ends, under
``outputs/evolution/ledger/<playbook_version>/``. The per-case verdicts,
the pairing with R0 and the workspace reference are what the design cycle
ranks rows on and what memory replay rebuilds after the suite author
changes; nothing here is read inside a search.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

# No fast_loop import at module level: the controller imports this module
# while the fast_loop package is initialising, and the reverse import would
# be circular. `failure_key` is trivial enough to restate.
ACCEPTANCE_KEY = "acceptance"


def failure_key(name: str) -> str:
    return str(name or "").strip().rsplit("/", 1)[-1]


#: Every evolution artefact (ledger, bank, cycles, re-runs, sealed file) lives
#: under one root so a series on another model or benchmark can keep its own
#: statistics: ``ADAMAS_EVOLUTION_ROOT`` (default ``outputs/evolution``).
EVOLUTION_ROOT_ENV = "ADAMAS_EVOLUTION_ROOT"


def evolution_root() -> Path:
    return Path(os.environ.get(EVOLUTION_ROOT_ENV) or (Path("outputs") / "evolution"))


def ledger_root_default() -> Path:
    return evolution_root() / "ledger"


LEDGER_ROOT = Path("outputs") / "evolution" / "ledger"  # historical default; prefer ledger_root_default()
SPLIT_FILE = Path("configs") / "datasets" / "evolution_split.yaml"


def split_of(task_id: str, split_file: Path | None = None) -> str:
    """``train`` / ``test`` / ``unknown`` per the fixed split file."""
    p = Path(split_file) if split_file else SPLIT_FILE
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return "unknown"
    # The scheduler names a CPE task ``rb_<task>``; the split file lists ``<task>``.
    bare = task_id[3:] if str(task_id).startswith("rb_") else str(task_id)
    for side in ("train", "test"):
        for names in (data.get(side) or {}).values():
            if task_id in (names or []) or bare in (names or []):
                return side
    return "unknown"


def candidate_kind(record: Any) -> str:
    meta = record.metadata or {}
    if meta.get("incumbent"):
        return "first_run"
    if meta.get("probe") or meta.get("persistence_phase") == 1:
        return "probe"
    kind = meta.get("candidate_kind")
    if kind:
        return str(kind)
    if (meta.get("node_resample") or {}).get("reauthor"):
        return "reauthor"
    if meta.get("node_resample"):
        return "resample"
    if record.playbook_id:
        return "row"
    return "other"


def _keys(names: list[str] | None) -> list[str]:
    return sorted({failure_key(n) for n in (names or [])})


def candidate_records(
    fl_state: Any,
    *,
    task_id: str,
    milestone_id: str,
    split: str,
    playbook_version: str,
    first_pass_version: str = "F0",
    suite_version: str = "",
    features: dict[str, Any] | None = None,
    f_entries_applied: list[str] | None = None,
    assignment: str = "deterministic",
    run_dir: str = "",
) -> list[dict[str, Any]]:
    """§5.1, one dict per candidate of this search (incumbent included)."""
    cands = list(fl_state.candidates)
    r0 = next((c for c in cands if (c.metadata or {}).get("candidate_kind") == "R0"), None)
    r0_net = (r0.metadata.get(ACCEPTANCE_KEY) or {}).get("net_fix") if r0 is not None else None
    persistence = fl_state.persistence or {}
    incumbent = next((c for c in cands if (c.metadata or {}).get("incumbent")), None)
    probes = [c for c in cands if (c.metadata or {}).get("persistence_phase") == 1]
    stable = None
    if incumbent is not None and incumbent.behaviour_passed:
        stable = set(_keys(incumbent.behaviour_passed))
        for p in probes:
            if p.behaviour_passed:
                stable &= set(_keys(p.behaviour_passed))
    out: list[dict[str, Any]] = []
    error_classes = (fl_state.error_classes or {}).get("classes") or []
    counts = (fl_state.error_classes or {}).get("counts") or {}
    for c in cands:
        meta = c.metadata or {}
        verdict = meta.get(ACCEPTANCE_KEY) or {}
        per_case: dict[str, str] = {}
        for k in _keys(c.behaviour_passed):
            per_case[k] = "pass"
        for k in _keys(c.behaviour_failures):
            per_case[k] = "fail"
        for k in verdict.get("excluded") or []:
            per_case[k] = "excluded"
        ws = getattr(c.workspace_ref, "path", None) or ""
        row_id = str(meta.get("v2_row") or c.playbook_id or "")
        out.append({
            "record_id": f"{task_id}:{milestone_id}:{c.candidate_id}",
            "task_id": task_id,
            "milestone_id": milestone_id,
            "split": split,
            "playbook_version": playbook_version,
            "first_pass_version": first_pass_version,
            "suite_version": suite_version,
            "features": dict(features or {}),
            "f_entries_applied": list(f_entries_applied or []),
            "assignment": assignment,
            "predicted_error_classes": list((features or {}).get("predicted_error_classes") or []),
            "actual_error_classes": dict(counts),
            "error_classes": list(error_classes),
            "candidate_kind": candidate_kind(c),
            "row_id": row_id,
            "row_state_at_run": str(meta.get("v2_row_state") or ""),
            "status": str(getattr(c.status, "value", c.status)),
            "behaviour_score": c.behaviour_score,
            "per_case_results": per_case,
            "persistent_before": _keys(persistence.get("persistent")),
            "flaky_before": _keys(persistence.get("flaky")),
            "stable_pass_before": sorted(stable) if stable is not None else [],
            "fixed": list(verdict.get("fixed") or []),
            "regressed": list(verdict.get("regressed") or []),
            "prior_regressions": list(verdict.get("prior_regressions") or []),
            "net_fix": verdict.get("net_fix"),
            "accepted": bool(verdict.get("accepted")),
            "accept_path": str(verdict.get("accept_path") or ""),
            "paired_R0_record_id": f"{task_id}:{milestone_id}:{r0.candidate_id}" if r0 is not None and c is not r0 else "",
            "delta_vs_R0": (
                (verdict.get("net_fix") - r0_net)
                if r0 is not None and c is not r0 and verdict.get("net_fix") is not None and r0_net is not None
                else None
            ),
            "committed": str(getattr(c.status, "value", c.status)) == "committed",
            "filtered_reason": str(c.rejection_message or "") if str(getattr(c.status, "value", c.status)) == "rejected" else "",
            "cost": {
                "calls": c.cost.backend_calls,
                "wall_seconds": (c.latency_ms or 0) / 1000.0,
                "tokens": c.cost.prompt_tokens + c.cost.completion_tokens,
                "usd": c.cost.estimated_cost_usd,
            },
            "workspace_ref": ws,
            "patch_hash": c.patch_hash or "",
            "llm_calls": [str(x) for x in (meta.get("llm_calls") or [])],
            "run_dir": run_dir,
        })
    return out


def milestone_record(
    fl_state: Any, *, task_id: str, milestone_id: str, split: str, features: dict[str, Any] | None = None,
    f_entries_applied: list[str] | None = None, final_status: str = "", admitted_to_bank: bool = False,
) -> dict[str, Any]:
    """§5.2."""
    cands = list(fl_state.candidates)
    incumbent = next((c for c in cands if (c.metadata or {}).get("incumbent")), None)
    persistence = fl_state.persistence or {}
    cost = {"calls": 0, "tokens": 0, "usd": 0.0}
    for c in cands:
        cost["calls"] += c.cost.backend_calls
        cost["tokens"] += c.cost.prompt_tokens + c.cost.completion_tokens
        cost["usd"] += c.cost.estimated_cost_usd
    return {
        "task_id": task_id,
        "milestone_id": milestone_id,
        "split": split,
        "features": dict(features or {}),
        "f_entries_applied": list(f_entries_applied or []),
        "first_run_gate": str(getattr(incumbent.status, "value", incumbent.status)) if incumbent is not None else "",
        "first_run_behaviour": incumbent.behaviour_score if incumbent is not None else None,
        "first_run_persistent": _keys(persistence.get("persistent")),
        "error_classes": list((fl_state.error_classes or {}).get("classes") or []),
        "routing": {k: v for k, v in (fl_state.routing or {}).items() if k != "incumbent_prior_suites"},
        "final_status": final_status,
        "total_cost": cost,
        "rows_tried": sorted({str((c.metadata or {}).get("v2_row") or c.playbook_id) for c in cands if c.playbook_id}),
        "committed_record_id": (
            f"{task_id}:{milestone_id}:{fl_state.selected_candidate_id}" if fl_state.selected_candidate_id else ""
        ),
        "admitted_to_bank": admitted_to_bank,
        "notes": list(fl_state.notes),
    }


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def write_search_ledger(
    fl_state: Any,
    *,
    task_id: str,
    milestone_id: str,
    playbook_version: str,
    ledger_root: Path | None = None,
    split_file: Path | None = None,
    run_dir: str = "",
    features: dict[str, Any] | None = None,
    f_entries_applied: list[str] | None = None,
    assignment: str = "deterministic",
    final_status: str = "",
) -> tuple[Path, Path]:
    root = Path(ledger_root) if ledger_root else ledger_root_default()
    split = split_of(task_id, split_file)
    version_dir = root / playbook_version
    cands = candidate_records(
        fl_state, task_id=task_id, milestone_id=milestone_id, split=split, playbook_version=playbook_version,
        features=features, f_entries_applied=f_entries_applied, assignment=assignment, run_dir=run_dir,
    )
    ms = milestone_record(
        fl_state, task_id=task_id, milestone_id=milestone_id, split=split, features=features,
        f_entries_applied=f_entries_applied, final_status=final_status,
    )
    ms["run_dir"] = run_dir
    return (
        append_jsonl(version_dir / "candidates.jsonl", cands),
        append_jsonl(version_dir / "milestones.jsonl", [ms]),
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not Path(path).is_file():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


__all__ = [
    "EVOLUTION_ROOT_ENV", "LEDGER_ROOT", "SPLIT_FILE", "evolution_root", "ledger_root_default", "append_jsonl", "candidate_kind", "candidate_records", "milestone_record",
    "read_jsonl", "split_of", "write_search_ledger",
]
