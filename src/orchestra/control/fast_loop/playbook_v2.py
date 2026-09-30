"""Playbook table v2: rows keyed on error class, five actions, a state per row.

Self-evolution spec §3. A row is *data* (loaded from
``configs/playbook_v2/repair.yaml``, with the S-row texts in
``instructions/<row_id>.md``); it is turned into a legacy ``Playbook`` at
selection time so the existing binding machinery (template switch, feedback
edits, budget edits, node resample) runs it. Every row but E9-T1 starts from
the incumbent's work: R0 and the rows stack on the same floor.

Actions: S (an instruction to the writer), T (a shape change), R (a read-only
role inserted before the repairer), B (more budget), N (node resample, built
by the controller). Row states: candidate -> trial -> active, with retired
for a row that failed its second trial (§3.4); the design cycle moves them
(stage 6), this module only reads and writes them.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from orchestra.control.fast_loop.playbooks import (
    _QUALITY_GUARD,
    _REPAIR_EVIDENCE_NOTE,
    Playbook,
    SearchReason,
)

ROOT = Path(__file__).resolve().parents[4]
V2_DIR = ROOT / "configs" / "playbook_v2"
REPAIR_TABLE_PATH = V2_DIR / "repair.yaml"
INSTRUCTIONS_DIR = V2_DIR / "instructions"

Action = Literal["S", "T", "R", "B", "N"]
RowState = Literal["active", "trial", "candidate", "retired"]
Table = Literal["repair", "first_pass", "legacy"]

_MINIMUM_PASSING = (
    "Reach a minimum passing implementation first. Do not polish, refactor, "
    "or add features the gate did not name."
)
#: read-only roles, for the composition rule
READ_ONLY_ROLES = ("contract_critic", "spec_auditor", "behaviour_critic")
#: the alternative first-pass shapes E9-T1 may switch to (§12.5 fallback)
E9_ALTERNATIVES = ("gate_then_repair", "review_then_fix")


@dataclass(frozen=True)
class SlotEdit:
    slot: str
    role: str
    mode: Literal["insert", "replace"] = "insert"
    read_only: bool = False


@dataclass(frozen=True)
class BudgetDelta:
    steps: int = 0
    seconds: int = 0


@dataclass(frozen=True)
class Precondition:
    kind: str
    value: str = ""


@dataclass(frozen=True)
class PlaybookRow:
    row_id: str
    table: Table
    error_classes: tuple[str, ...]
    action: Action
    target_template: str | None = None
    slot_edits: tuple[SlotEdit, ...] = ()
    instruction: str | None = None
    evidence_routing: tuple[str, ...] = ("improver",)
    budget_delta: BudgetDelta | None = None
    preconditions: tuple[Precondition, ...] = ()
    from_incumbent: bool = True
    preventive_variant: str | None = None
    source_rows: tuple[str, ...] = ()
    origin: str = ""
    intent: str = ""
    state: RowState = "candidate"
    #: legacy id of the mechanism this row runs through (node resample only)
    legacy_id: str = ""

    def applies_to(self, error_class: str) -> bool:
        return "*" in self.error_classes or error_class in self.error_classes

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["error_classes"] = list(self.error_classes)
        d["slot_edits"] = [asdict(e) for e in self.slot_edits]
        d["evidence_routing"] = list(self.evidence_routing)
        d["preconditions"] = [asdict(p) for p in self.preconditions]
        d["source_rows"] = list(self.source_rows)
        d["budget_delta"] = asdict(self.budget_delta) if self.budget_delta else None
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> PlaybookRow:
        bd = d.get("budget_delta")
        return cls(
            row_id=str(d["row_id"]),
            table=str(d.get("table") or "repair"),  # type: ignore[arg-type]
            error_classes=tuple(str(x) for x in (d.get("error_classes") or ())),
            action=str(d["action"]),  # type: ignore[arg-type]
            target_template=d.get("target_template") or None,
            slot_edits=tuple(SlotEdit(**e) for e in (d.get("slot_edits") or ())),
            instruction=d.get("instruction") or None,
            evidence_routing=tuple(str(x) for x in (d.get("evidence_routing") or ("improver",))),
            budget_delta=BudgetDelta(**bd) if bd else None,
            preconditions=tuple(Precondition(**p) for p in (d.get("preconditions") or ())),
            from_incumbent=bool(d.get("from_incumbent", True)),
            preventive_variant=d.get("preventive_variant") or None,
            source_rows=tuple(str(x) for x in (d.get("source_rows") or ())),
            origin=str(d.get("origin") or ""),
            intent=str(d.get("intent") or ""),
            state=str(d.get("state") or "candidate"),  # type: ignore[arg-type]
            legacy_id=str(d.get("legacy_id") or ""),
        )


# --------------------------------------------------------------------------- the table


def _row(rid, classes, action, *, state="candidate", origin="new", intent="", target=None, slots=(), instr=None,
         budget=None, pre=(), from_inc=True, evidence=("improver",), preventive=None, legacy=""):
    return PlaybookRow(
        row_id=rid, table="repair", error_classes=tuple(classes), action=action, target_template=target,
        slot_edits=tuple(slots), instruction=instr, evidence_routing=tuple(evidence), budget_delta=budget,
        preconditions=tuple(pre), from_incumbent=from_inc, preventive_variant=preventive, origin=origin,
        intent=intent, state=state, legacy_id=legacy,
    )


def default_repair_rows() -> tuple[PlaybookRow, ...]:
    """The table as written in §3.2-3.3, with its initial states."""
    R = lambda role: (SlotEdit("reviewer", role, "insert", True),)  # noqa: E731
    return (
        # universal
        _row("U-N1", ["*"], "N", state="active", origin="pb_q_node_resample", legacy="pb_q_node_resample",
             intent="flaky failures made deterministic by re-sampling the node that owns them",
             pre=(Precondition("high_variance"),)),
        _row("U-S1", ["*"], "S", origin="pb_failures_to_agent", evidence=("upstream",),
             intent="the upstream writer, not the repairer, fixes what it caused",
             pre=(Precondition("blamed_before_repairer"),)),
        _row("U-B1", ["*"], "B", state="active", origin="pb_budget_steps_time_small", budget=BudgetDelta(2, 30),
             intent="a writer that hit its cap reaches a later stage", pre=(Precondition("writer_hit_cap"),)),
        # E1 unfinished
        _row("E1-B2", ["E1"], "B", state="active", origin="pb_budget_steps_time_large", budget=BudgetDelta(8, 300),
             instr=_MINIMUM_PASSING, intent="a minimum passing implementation is reached before polish"),
        _row("E1-T1", ["E1"], "T", origin="chain", target="continuation_chain",
             slots=(SlotEdit("first", "contract_author"),), evidence=("first", "improver"),
             intent="contract pass then implementation pass finish what one writer could not",
             pre=(Precondition("focus_files_min", "6"),), preventive="F5"),
        _row("E1-T2", ["E1"], "T", origin="pb_rtf_drop_reviewer", target="continuation", budget=BudgetDelta(4, 120),
             intent="the reviewer's budget goes to the writer", pre=(Precondition("has_readonly_role"),)),
        # E2 imports and interface
        _row("E2-S1", ["E2"], "S", intent="interface checklist first, then the fix"),
        _row("E2-R1", ["E2"], "R", state="active", origin="pb_tf_diagnose_before_repair",
             target="continuation_reviewed", slots=R("contract_critic"), evidence=("reviewer", "improver"),
             intent="a contract reading raises the repair's fix rate on interface failures",
             pre=(Precondition("lacks_role", "contract_critic"),), preventive="F1"),
        # E3 main path
        _row("E3-S1", ["E3"], "S", intent="a reproduction before every fix"),
        _row("E3-R1", ["E3"], "R", origin="test_first_diagnosed", target="continuation_reviewed",
             slots=R("behaviour_critic"), evidence=("reviewer", "improver"),
             intent="a behaviour reading raises the repair's fix rate", pre=(Precondition("lacks_role", "behaviour_critic"),)),
        _row("E3-T1", ["E3"], "T", state="active", origin="pb_tf_second_repairer", target="continuation_double",
             evidence=("improver", "second_improver"), intent="what the first round leaves, the second fixes"),
        # E4 state transitions
        _row("E4-S1", ["E4"], "S", intent="the documented state machine as a checklist", preventive="F3"),
        _row("E4-R1", ["E4"], "R", target="continuation_reviewed", slots=R("spec_auditor"), evidence=("reviewer", "improver"),
             intent="a spec reading of the state machine before the fix",
             pre=(Precondition("lacks_role", "spec_auditor"),), preventive="F3"),
        # E5 error paths
        _row("E5-S1", ["E5"], "S", intent="the documented exception list drives the fix", preventive="F2"),
        _row("E5-R1", ["E5"], "R", target="continuation_reviewed", slots=R("contract_critic"), evidence=("reviewer", "improver"),
             intent="exception hierarchy checked against the base milestone's definitions",
             pre=(Precondition("lacks_role", "contract_critic"),), preventive="F1"),
        # E6 boundaries and protocol
        _row("E6-S1", ["E6"], "S", intent="the object protocol checked item by item", preventive="F6"),
        _row("E6-R1", ["E6"], "R", target="continuation_reviewed", slots=R("behaviour_critic"), evidence=("reviewer", "improver"),
             intent="a boundary-focused reading before the fix", pre=(Precondition("lacks_role", "behaviour_critic"),)),
        # E7 cross-module
        _row("E7-T1", ["E7"], "T", state="active", origin="parallel_audit / pb_rtf_second_angle", target="continuation_audited",
             evidence=("spec_review", "contract_review", "improver"),
             intent="two angles surface the cross-module defect one reviewer misses", preventive="F4"),
        _row("E7-R1", ["E7"], "R", origin="pb_chain_fill_third", target="continuation_reviewed",
             slots=(SlotEdit("improver", "integrator", "replace"),), intent="an integrator wires the modules together",
             pre=(Precondition("lacks_role", "integrator"),)),
        _row("E7-S1", ["E7"], "S", intent="the call chain traced module by module"),
        # E8 performance
        _row("E8-S1", ["E8"], "S", intent="the hot spot found and its complexity fixed"),
        _row("E8-B1", ["E8"], "B", budget=BudgetDelta(0, 300), intent="the repairer gets the clock a profiling pass needs"),
        # E9 scattered or stuck
        _row("E9-T2", ["E9"], "T", origin="pb_chain_to_review_fix", target="continuation_reviewed",
             slots=R("contract_critic"), evidence=("reviewer", "improver"),
             intent="a reader between the writers converts findings into fixes", pre=(Precondition("writers_min", "2"),)),
        _row("E9-S1", ["E9"], "S", intent="the largest cluster fixed this round, the rest left"),
        _row("E9-T1", ["E9"], "T", state="active", origin="pb_solo_* generalised", from_inc=False, target=None,
             intent="a different first-pass shape reaches what this one cannot",
             pre=(Precondition("gate_failed"), Precondition("stuck"))),
    )


def default_legacy_rows() -> tuple[PlaybookRow, ...]:
    """Kept for replaying frozen solo plans only; never counted or evolved (§3.6)."""
    return tuple(
        PlaybookRow(row_id=rid, table="legacy", error_classes=("*",), action="T", origin=rid, legacy_id=rid, state="active")
        for rid in ("pb_solo_to_gate_repair", "pb_solo_to_review_fix", "pb_solo_specialist", "pb_solo_budget")
    )


MERGED_INTO_R0 = (
    "pb_q_continue_improve", "pb_tf_q_improve_after_gate", "pb_tf_q_failures_to_improver",
    "pb_tf_q_diagnose_from_improve", "pb_tf_failures_to_repairer", "pb_gtr_failures_to_repairer",
)


# --------------------------------------------------------------------------- yaml


def save_repair_table(rows: Iterable[PlaybookRow], path: Path = REPAIR_TABLE_PATH, *, version: str = "v1") -> Path:
    payload = {"version": version, "rows": [r.to_dict() for r in rows]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def load_repair_table(path: Path | None = None) -> tuple[PlaybookRow, ...]:
    """The published table, or the in-code default when no file exists."""
    p = Path(path) if path else REPAIR_TABLE_PATH
    if not p.is_file():
        return default_repair_rows() + default_legacy_rows()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return tuple(PlaybookRow.from_dict(d) for d in (data.get("rows") or []))


def table_version(path: Path | None = None) -> str:
    p = Path(path) if path else REPAIR_TABLE_PATH
    if not p.is_file():
        return "v0-default"
    try:
        return str((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("version") or "v?")
    except (OSError, yaml.YAMLError):
        return "v?"


def instruction_text(row: PlaybookRow, directory: Path = INSTRUCTIONS_DIR) -> str:
    if row.instruction:
        return row.instruction
    p = Path(directory) / f"{row.row_id}.md"
    if p.is_file():
        return p.read_text(encoding="utf-8").strip()
    return ""


# --------------------------------------------------------------------------- facts and preconditions


@dataclass
class RowFacts:
    """What the preconditions and the composition rule are evaluated on."""

    template_id: str
    roles_by_slot: dict[str, str] = field(default_factory=dict)
    high_variance: bool = False
    n_focus_files: int = 0
    writer_hit_cap: bool = False
    gate_failed: bool = False
    stuck: bool = False
    blamed_before_repairer: bool = False
    tried_rows: tuple[str, ...] = ()
    templates_tried: tuple[str, ...] = ()

    @property
    def roles(self) -> set[str]:
        return set(self.roles_by_slot.values())

    @property
    def writers(self) -> int:
        return sum(1 for r in self.roles_by_slot.values() if r not in READ_ONLY_ROLES and r != "test_author")


def precondition_holds(pre: Precondition, facts: RowFacts) -> bool:
    k, v = pre.kind, pre.value
    if k == "high_variance":
        return facts.high_variance
    if k == "focus_files_min":
        return facts.n_focus_files >= int(v or 0)
    if k == "has_readonly_role":
        return bool(facts.roles & set(READ_ONLY_ROLES))
    if k == "lacks_role":
        return v not in facts.roles
    if k == "writers_min":
        return facts.writers >= int(v or 0)
    if k == "writer_hit_cap":
        return facts.writer_hit_cap
    if k == "gate_failed":
        return facts.gate_failed
    if k == "stuck":
        return facts.stuck
    if k == "blamed_before_repairer":
        return facts.blamed_before_repairer
    return False


def compose(row: PlaybookRow, facts: RowFacts) -> tuple[PlaybookRow | None, str]:
    """§3.5 rule 1: a read-only role already in the shape is not added twice.

    Returns the row to run (possibly the swap-angle variant) and a note; None
    when the row is skipped.
    """
    for edit in row.slot_edits:
        if edit.read_only and edit.role in facts.roles:
            others = [r for r in READ_ONLY_ROLES if r not in facts.roles]
            if not others:
                return None, f"{row.row_id}: every read-only angle is already in the shape"
            swapped = replace(row, row_id=row.row_id + "~swap", slot_edits=tuple(
                replace(e, role=others[0]) if e is edit else e for e in row.slot_edits))
            return swapped, f"{row.row_id}: {edit.role} already present, swapped to {others[0]}"
    return row, ""


# --------------------------------------------------------------------------- selection (§3.7)


@dataclass
class Selection:
    rows: list[PlaybookRow]
    filtered: dict[str, str]
    notes: list[str]
    trial_slot_used: bool = False


def _rng(seed_parts: Iterable[str]) -> random.Random:
    digest = hashlib.sha256("|".join(seed_parts).encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def select_rows(
    rows: Iterable[PlaybookRow],
    *,
    classes: Iterable[str],
    facts: RowFacts,
    row_slots: int,
    trial_prob: float,
    max_trial_concurrent: int,
    trials_running: int,
    ranking: Mapping[str, Mapping[str, float]] | None = None,
    seed: Iterable[str] = (),
) -> Selection:
    """The rows to run this search, in order, within ``row_slots`` (§3.7).

    1. rows of the main class(es) plus universal rows whose precondition holds;
    2. filtered by precondition, composition, already tried on this milestone;
    3. U-N1 takes a slot first when high_variance holds;
    4. active rows ordered by the ranking score, first taken;
    5. a trial slot goes to a trial row of the class with probability
       ``trial_prob`` (mode B) -- and never beyond ``max_trial_concurrent``.
    """
    classes = [c for c in classes] or []
    filtered: dict[str, str] = {}
    notes: list[str] = []
    pool: list[PlaybookRow] = []
    for row in rows:
        if row.table != "repair" or row.state == "retired":
            continue
        if not (any(row.applies_to(c) for c in classes) or "*" in row.error_classes):
            continue
        if row.row_id in facts.tried_rows or row.row_id.split("~", 1)[0] in facts.tried_rows:
            filtered[row.row_id] = "already tried on this milestone"
            continue
        missing = [p for p in row.preconditions if not precondition_holds(p, facts)]
        if missing:
            filtered[row.row_id] = "precondition: " + ", ".join(f"{p.kind} {p.value}".strip() for p in missing)
            continue
        composed, note = compose(row, facts)
        if composed is None:
            filtered[row.row_id] = note
            continue
        if note:
            notes.append(note)
        pool.append(composed)

    def score(row: PlaybookRow) -> tuple[float, int]:
        by_class = ranking or {}
        base = row.row_id.split("~", 1)[0]
        vals = [float(by_class.get(c, {}).get(base, 0.0)) for c in classes]
        # the class the row was selected for; ties broken by table order
        return (-max(vals) if vals else 0.0, 0)

    chosen: list[PlaybookRow] = []
    n1 = next((r for r in pool if r.row_id == "U-N1"), None)
    if n1 is not None and facts.high_variance:
        chosen.append(n1)
    active = [r for r in pool if r.state == "active" and r not in chosen]
    active.sort(key=score)
    trial = [r for r in pool if r.state == "trial" and r not in chosen]
    trial_used = False
    rng = _rng(seed)
    while len(chosen) < row_slots:
        want_trial = (
            trial and not trial_used and trials_running < max_trial_concurrent and rng.random() < trial_prob
        )
        if want_trial:
            chosen.append(trial.pop(0))
            trial_used = True
            continue
        if active:
            chosen.append(active.pop(0))
            continue
        if trial and not trial_used and trials_running < max_trial_concurrent:
            chosen.append(trial.pop(0))
            trial_used = True
            continue
        break
    return Selection(rows=chosen, filtered=filtered, notes=notes, trial_slot_used=trial_used)


# --------------------------------------------------------------------------- row -> legacy Playbook


def to_playbook(row: PlaybookRow, *, facts: RowFacts, instructions_dir: Path = INSTRUCTIONS_DIR) -> Playbook | None:
    """The binding recipe for a row, on the existing machinery.

    ``None`` for rows the controller builds itself (N) and for E9-T1 when no
    alternative first-pass shape is left.
    """
    quality = frozenset({SearchReason.QUALITY})
    pid = f"v2:{row.row_id}"
    guard = _QUALITY_GUARD + " " + _REPAIR_EVIDENCE_NOTE
    text = instruction_text(row, instructions_dir)
    if row.action == "N":
        return None
    if row.action == "S" and "upstream" in row.evidence_routing:
        return Playbook(
            playbook_id=pid, reason=row.intent, classes=frozenset(), intent=row.intent,
            target="", include_failure_list=True, extra_prompt=(text + " " + guard).strip(),
            search_reasons=quality,
        )
    if row.action in ("S", "B") or (row.action == "T" and row.target_template == "continuation"):
        bd = row.budget_delta or BudgetDelta()
        return Playbook(
            playbook_id=pid, reason=row.intent, classes=frozenset(), intent=row.intent,
            target="improver", include_failure_list=True,
            extra_prompt=(text + " " + guard).strip() if text else guard,
            steps_delta=bd.steps, timeout_delta=bd.seconds,
            switch_template="continuation", role_from_diagnosis="improver",
            continue_from_incumbent=True, search_reasons=quality,
        )
    if row.action in ("R", "T") and row.target_template and row.from_incumbent:
        slots = tuple((e.slot, e.role) for e in row.slot_edits)
        return Playbook(
            playbook_id=pid, reason=row.intent, classes=frozenset(), intent=row.intent,
            target="improver", include_failure_list=True, extra_prompt=(text + " " + guard).strip() if text else guard,
            feedback_slots=tuple(row.evidence_routing), switch_template=row.target_template, switch_slots=slots,
            role_from_diagnosis="improver" if not any(e.slot == "improver" for e in row.slot_edits) else "",
            continue_from_incumbent=True, search_reasons=quality,
        )
    if row.action == "T" and not row.from_incumbent:
        alternatives = [t for t in E9_ALTERNATIVES if t != facts.template_id and t not in facts.templates_tried]
        if not alternatives:
            return None
        return Playbook(
            playbook_id=pid, reason=row.intent, classes=frozenset(), intent=row.intent,
            include_failure_list=True, feedback_slots=("repairer", "fixer"), switch_template=alternatives[0],
            role_from_diagnosis="repairer" if alternatives[0] == "gate_then_repair" else "fixer",
            search_reasons=quality,
        )
    return None


def replace_row_state(row: PlaybookRow, state: str) -> PlaybookRow:
    return replace(row, state=state)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- state machine (§3.4)


def transition(row: PlaybookRow, event: str) -> PlaybookRow:
    """candidate -(select)-> trial -(pass)-> active; trial -(fail)-> candidate then retired; active -(fail)-> candidate."""
    s = row.state
    if event == "select" and s == "candidate":
        return replace(row, state="trial")
    if event == "pass" and s == "trial":
        return replace(row, state="active")
    if event == "fail" and s == "trial":
        return replace(row, state="retired" if row.origin.endswith("#trial2") else "candidate",
                       origin=row.origin + "#trial2" if not row.origin.endswith("#trial2") else row.origin)
    if event == "fail" and s == "active":
        return replace(row, state="candidate")
    return row


__all__ = [
    "BudgetDelta", "E9_ALTERNATIVES", "INSTRUCTIONS_DIR", "MERGED_INTO_R0", "PlaybookRow", "Precondition",
    "REPAIR_TABLE_PATH", "RowFacts", "Selection", "SlotEdit", "compose", "replace_row_state", "default_legacy_rows",
    "default_repair_rows", "instruction_text", "load_repair_table", "precondition_holds", "save_repair_table",
    "select_rows", "table_version", "to_playbook", "transition",
]
