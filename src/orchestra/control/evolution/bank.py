"""The bank of milestone checkpoints (self-evolution spec §6).

An entry is everything a re-run needs to start a milestone again from the
state its run started from: the canonical repository and the revision the
milestone was built on (its predecessor snapshot), the milestone
specification as the run compiled it, the frozen suite it was graded
against, the workspace of its incumbent, and the persistent failures with
their classes. Only training tasks are admitted (§0.3 item 2); the split
file decides. Entries live under ``outputs/evolution/bank/``:
``entries/<entry_id>.json`` for the live ones, ``archive/`` for superseded
or invalidated ones, ``index.jsonl`` as the append-only log of admissions.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from orchestra.control.evolution.ledger import evolution_root, split_of

BANK_ROOT = Path("outputs") / "evolution" / "bank"  # historical default; the root follows ADAMAS_EVOLUTION_ROOT


@dataclass
class BankEntry:
    entry_id: str
    task_id: str
    milestone_id: str
    features: dict[str, Any] = field(default_factory=dict)
    #: canonical repository path + the revision the milestone started from
    predecessor_snapshot: dict[str, str] = field(default_factory=dict)
    milestone_spec: dict[str, Any] = field(default_factory=dict)
    frozen_suite_ref: str = ""
    suite_version: str = ""
    first_run_workspace_ref: str = ""
    incumbent_workspace_ref: str = ""
    persistent_failures: dict[str, str] = field(default_factory=dict)
    original_records: list[str] = field(default_factory=list)
    outcome: str = ""  # gate_failed | low_score | success
    run_dir: str = ""
    suite_stale: bool = False
    admitted_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BankEntry:
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})  # type: ignore[arg-type]


def suite_version_of(frozen_dir: str | Path) -> str:
    """A content hash of the frozen suite: the same suite scores the same."""
    p = Path(frozen_dir)
    if not p.is_dir():
        return ""
    h = hashlib.sha256()
    for f in sorted(p.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        h.update(str(f.relative_to(p)).encode())
        try:
            h.update(f.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:16]


def _outcome(incumbent: dict[str, Any] | None, final_status: str, first_attempt: dict[str, Any] | None = None) -> str:
    """The first run's outcome: the search incumbent when a search ran, else the first attempt's gate result."""
    if incumbent is not None:
        if str(incumbent.get("status")) not in ("valid", "committed", "discarded"):
            return "gate_failed"
        score = incumbent.get("behaviour_score")
    else:
        attempt = first_attempt or {}
        if str(attempt.get("status") or final_status) != "committed":
            return "gate_failed"
        score = (attempt.get("metadata") or {}).get("behaviour_score")
    if score is not None and float(score) < 0.9:
        return "low_score"
    return "success"


def _case_name(node_id: str) -> str:
    return str(node_id).split(".spec_tests/", 1)[-1].rsplit("/", 1)[-1]


def entries_from_run(run_dir: str | Path, *, split_file: Path | None = None) -> list[BankEntry]:
    """Every milestone of one task run as a bank entry (before the admission rules)."""
    run = Path(run_dir)
    task_id = run.name
    exec_files = list((run / "tasks").glob("rb_*/task_execution.json"))
    if not exec_files:
        return []
    data = json.loads(exec_files[0].read_text(encoding="utf-8"))
    canonical = str(data.get("canonical_workspace_ref") or "")
    commits = {r["subtask_id"]: r for r in data.get("workspace_commit_records") or [] if r.get("status") == "committed"}
    decisions: dict[str, dict[str, Any]] = {}
    fp = run / "first_pass_decision.json"
    if fp.is_file():
        try:
            decisions = {d["milestone_id"]: d for d in json.loads(fp.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            decisions = {}
    out: list[BankEntry] = []
    order = [s for s in (data.get("subtasks") or {})]
    for i, mid in enumerate(order):
        sub = data["subtasks"][mid]
        spec = sub.get("spec") or {}
        meta = spec.get("metadata") or {}
        fl = (data.get("fast_loop_states") or {}).get(mid) or {}
        cands = fl.get("candidates") or []
        incumbent = next((c for c in cands if (c.get("metadata") or {}).get("incumbent")), None)
        commit = commits.get(mid)
        base_rev = str((commit or {}).get("expected_base_revision") or sub.get("base_task_revision") or "")
        frozen = run / "harness" / f"{mid}.spec_tests"
        by_case = {_case_name(k): v for k, v in (((fl.get("error_classes") or {}).get("by_case")) or {}).items()}
        persistent = {_case_name(k): by_case.get(_case_name(k), "")
                      for k in ((fl.get("persistence") or {}).get("persistent") or [])}
        if not persistent and not fl:
            # no search ran: the first attempt's failed gate cases are the persistent set, unclassified
            attempts = sub.get("attempts") or []
            stages = ((attempts[0].get("metadata") or {}).get("harness_stages") or []) if attempts else []
            spec_stage = next((st for st in stages if st.get("stage") == "spec_tests"), {})
            persistent = {_case_name(k): "" for k in (spec_stage.get("failed_tests") or [])}
        inc_ws = ""
        for c in cands:
            if (c.get("metadata") or {}).get("incumbent"):
                continue
            ws = (c.get("workspace_ref") or {}).get("path")
            if ws and (c.get("metadata") or {}).get("persistence_phase") == 1:
                inc_ws = str(Path(ws).parent.parent.parent / "base" / "repo")
                break
        entry = BankEntry(
            entry_id=f"{task_id}:{mid}:{base_rev[:8] or 'nobase'}",
            task_id=task_id,
            milestone_id=mid,
            features=dict((decisions.get(mid) or {}).get("features") or {}),
            predecessor_snapshot={"repo": canonical, "revision": base_rev},
            milestone_spec={
                "objective": spec.get("objective", ""),
                "title": spec.get("title", ""),
                "acceptance": meta.get("acceptance"),
                "focus_paths": meta.get("focus_paths") or [],
                "risk_rationale": meta.get("risk_rationale", ""),
                "template": spec.get("local_graph_template", ""),
                "agent_roster": meta.get("agent_roster"),
                "milestone_index": i,
                "milestone_count": len(order),
                "dependencies": spec.get("dependencies") or [],
            },
            frozen_suite_ref=str(frozen) if frozen.is_dir() else "",
            suite_version=suite_version_of(frozen),
            first_run_workspace_ref=str(sub.get("workspace_ref") or ""),
            incumbent_workspace_ref=inc_ws or str(sub.get("workspace_ref") or ""),
            persistent_failures=persistent,
            original_records=[f"{task_id}:{mid}:{c.get('candidate_id')}" for c in cands],
            outcome=_outcome(incumbent, str(sub.get("status") or ""), (sub.get("attempts") or [None])[0]),
            run_dir=str(run),
        )
        out.append(entry)
    return out


@dataclass
class AdmissionPolicy:
    success_sample_rate: float = 0.2
    min_per_class: int = 3
    split_file: Path | None = None


class Bank:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else evolution_root() / "bank"
        self.entries_dir = self.root / "entries"
        self.archive_dir = self.root / "archive"

    def _path(self, entry_id: str) -> Path:
        return self.entries_dir / (entry_id.replace(":", "__").replace("/", "_") + ".json")

    def entries(self, *, include_stale: bool = False) -> list[BankEntry]:
        out = []
        for p in sorted(self.entries_dir.glob("*.json")):
            try:
                e = BankEntry.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if e.suite_stale and not include_stale:
                continue
            out.append(e)
        return out

    def _write(self, entry: BankEntry) -> Path:
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        p = self._path(entry.entry_id)
        p.write_text(json.dumps(entry.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        with (self.root / "index.jsonl").open("a", encoding="utf-8") as h:
            h.write(json.dumps({"entry_id": entry.entry_id, "task_id": entry.task_id, "milestone_id": entry.milestone_id,
                                "outcome": entry.outcome, "classes": sorted(set(entry.persistent_failures.values()))}) + "\n")
        return p

    def archive(self, entry: BankEntry, reason: str) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        p = self._path(entry.entry_id)
        if p.is_file():
            shutil.move(str(p), str(self.archive_dir / p.name))
        with (self.root / "index.jsonl").open("a", encoding="utf-8") as h:
            h.write(json.dumps({"entry_id": entry.entry_id, "archived": reason}) + "\n")

    def admit(self, candidates: list[BankEntry], policy: AdmissionPolicy) -> list[BankEntry]:
        """§6.2: train only; failed and low-scoring milestones always; successes sampled; latest per milestone."""
        admitted: list[BankEntry] = []
        for e in candidates:
            if split_of(e.task_id, policy.split_file) != "train":
                continue
            if not e.frozen_suite_ref or not e.predecessor_snapshot.get("revision"):
                continue
            if e.outcome == "success":
                rng = random.Random(int(hashlib.sha256(f"{e.task_id}:{e.milestone_id}".encode()).hexdigest()[:16], 16))
                if rng.random() >= policy.success_sample_rate:
                    continue
            for old in self.entries(include_stale=True):
                if old.task_id == e.task_id and old.milestone_id == e.milestone_id and old.entry_id != e.entry_id:
                    self.archive(old, "superseded by a newer run")
            self._write(e)
            admitted.append(e)
        return admitted

    def class_counts(self) -> Counter[str]:
        c: Counter[str] = Counter()
        for e in self.entries():
            for cls in set(e.persistent_failures.values()):
                if cls:
                    c[cls] += 1
        return c

    def by_class(self, error_class: str) -> list[BankEntry]:
        return [e for e in self.entries() if error_class in set(e.persistent_failures.values())]

    def invalidate_task(self, task_id: str, reason: str = "plan changed") -> int:
        n = 0
        for e in self.entries(include_stale=True):
            if e.task_id == task_id:
                self.archive(e, reason)
                n += 1
        return n

    def mark_suite_stale(self, task_id: str, milestone_id: str | None = None) -> int:
        n = 0
        for e in self.entries(include_stale=True):
            if e.task_id == task_id and (milestone_id is None or e.milestone_id == milestone_id):
                e.suite_stale = True
                self._write(e)
                n += 1
        return n


__all__ = ["BANK_ROOT", "AdmissionPolicy", "Bank", "BankEntry", "entries_from_run", "suite_version_of"]
