"""The first-pass designer (spec §4): preventive design layered on the planner's choice.

The planner's output is F0 and is never edited on disk. For each milestone
the designer computes the features (§4.2), matches the entries of the
compatibility table (§4.3, ``configs/playbook_v2/first_pass.yaml``), applies
the matched entries as increments to the milestone draft in memory (a
read-only role added, an instruction appended to the writer's mandate, a
budget raised, or -- for F4 / F5 -- a different template), and records the
decision in ``first_pass_decision.json``. Trial entries are assigned at
random with probability 0.5 (§4.6, seeded on task and milestone); an
unvalidated combination of entries is never applied (§4.5).
"""

from __future__ import annotations

import hashlib
import os
import random
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from orchestra.control.first_pass.features import milestone_features
from orchestra.realbench.milestone_planner import AgentDraft, MilestoneDraft, MilestonePlanDraft

ROOT = Path(__file__).resolve().parents[4]
FIRST_PASS_TABLE_PATH = ROOT / "configs" / "playbook_v2" / "first_pass.yaml"
INSTRUCTIONS_DIR = ROOT / "configs" / "playbook_v2" / "instructions"
FORCE_F_ENV = "ADAMAS_FORCE_F"
READ_ONLY = ("contract_critic", "spec_auditor", "behaviour_critic")
EDITING_DEFAULT = ("implementer", "test_driven_implementer", "contract_author", "integrator")
#: templates whose last slot is a writer, or that accept an appended repairer (§4.4 rule 1)
REPAIRABLE_SHAPES = ("test_first", "gate_then_repair", "review_then_fix", "parallel_audit", "chain")
DEFAULT_THRESHOLDS = {"e1_files": 6, "f2": 5, "f3": 3, "f5": 8, "f5s": 20, "f6": 4}


@dataclass(frozen=True)
class Trigger:
    feature: str
    op: str = ">="
    #: a literal, or "thr.<key>" resolved from the thresholds block
    value: str = "1"

    def holds(self, features: Mapping[str, Any], thresholds: Mapping[str, Any]) -> bool:
        actual = features.get(self.feature)
        wanted: Any = self.value
        if isinstance(wanted, str) and wanted.startswith("thr."):
            wanted = thresholds.get(wanted[4:], DEFAULT_THRESHOLDS.get(wanted[4:], 0))
        if self.op == "==":
            return str(actual) == str(wanted)
        try:
            return float(actual or 0) >= float(wanted)
        except (TypeError, ValueError):
            return False


@dataclass(frozen=True)
class FEntry:
    entry_id: str
    triggers: tuple[Trigger, ...]
    predicted_error_classes: tuple[str, ...]
    #: kinds: add_reviewer(role) | instruction(file) | template(id) | budget(steps, seconds) | chain
    actions: tuple[dict[str, Any], ...]
    source_rows: tuple[str, ...] = ()
    state: str = "candidate"
    #: any trigger suffices when true, all when false
    any_trigger: bool = True
    combination_of: tuple[str, ...] = ()
    origin: str = ""
    intent: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["triggers"] = [asdict(t) for t in self.triggers]
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> FEntry:
        return cls(
            entry_id=str(d["entry_id"]),
            triggers=tuple(Trigger(**t) for t in (d.get("triggers") or ())),
            predicted_error_classes=tuple(str(x) for x in (d.get("predicted_error_classes") or ())),
            actions=tuple(dict(a) for a in (d.get("actions") or ())),
            source_rows=tuple(str(x) for x in (d.get("source_rows") or ())),
            state=str(d.get("state") or "candidate"),
            any_trigger=bool(d.get("any_trigger", True)),
            combination_of=tuple(str(x) for x in (d.get("combination_of") or ())),
            origin=str(d.get("origin") or ""),
            intent=str(d.get("intent") or ""),
        )


def default_first_pass_entries() -> tuple[FEntry, ...]:
    return (
        FEntry("F0", (), (), (), state="active", intent="the planner's own choice, unchanged"),
        FEntry("F1", (Trigger("kind", "==", "foundation"),), ("E2", "E5"),
               ({"kind": "add_reviewer", "role": "contract_critic"},), source_rows=("E2-R1", "E5-R1"), state="trial",
               intent="a contract critic after the implementer of a foundation milestone"),
        FEntry("F2", (Trigger("n_documented_exceptions", ">=", "thr.f2"),), ("E5",),
               ({"kind": "instruction", "file": "F2.md"},), source_rows=("E5-S1",),
               intent="the documented exception list handed to the implementer up front"),
        FEntry("F3", (Trigger("n_state_transitions", ">=", "thr.f3"),), ("E4",),
               ({"kind": "instruction", "file": "F3.md"}, {"kind": "add_reviewer", "role": "spec_auditor"}),
               source_rows=("E4-S1", "E4-R1"), intent="a state-machine checklist and a spec auditor"),
        FEntry("F4", (Trigger("kind", "==", "integration"),), ("E7", "E2"),
               ({"kind": "template", "template_id": "parallel_audit"},), source_rows=("E7-T1",), state="active",
               intent="the integration milestone audited from two angles (the existing practice)"),
        FEntry("F5", (Trigger("n_focus_files", ">=", "thr.f5"), Trigger("n_public_symbols", ">=", "thr.f5s")), ("E1",),
               ({"kind": "budget", "steps": 4, "seconds": 300}, {"kind": "chain", "when_files_at_least": "thr.f5"}),
               source_rows=("E1-B2", "E1-T1"), intent="budget by size; a contract pass first when the scope is wide"),
        FEntry("F6", (Trigger("n_public_classes", ">=", "thr.f6"),), ("E6",),
               ({"kind": "instruction", "file": "F6.md"},), source_rows=("E6-S1",),
               intent="the object-protocol checklist handed to the implementer up front"),
    )


def save_first_pass_table(entries: Iterable[FEntry], path: Path = FIRST_PASS_TABLE_PATH, *, version: str = "v1") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": version, "entries": [e.to_dict() for e in entries]}, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def load_first_pass_table(path: Path | None = None) -> tuple[FEntry, ...]:
    p = Path(path) if path else FIRST_PASS_TABLE_PATH
    if not p.is_file():
        return default_first_pass_entries()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return tuple(FEntry.from_dict(d) for d in (data.get("entries") or []))


def _instruction(file: str, directory: Path) -> str:
    p = Path(directory) / file
    return p.read_text(encoding="utf-8").strip() if p.is_file() else ""


# --------------------------------------------------------------------------- applying an entry


def _roles(m: MilestoneDraft) -> list[str]:
    return [a.role for a in m.agents]


def _agent(m: MilestoneDraft, *, slot: str, role: str, title: str, mandate: str) -> AgentDraft:
    proto = next((a for a in m.agents if a.role in EDITING_DEFAULT), m.agents[0] if m.agents else None)
    return AgentDraft(
        role_id=f"{slot}_{role}"[:48], title=title, mandate=mandate, role=role, slot_id=slot,
        max_tokens=proto.max_tokens if proto else 8192, max_steps=proto.max_steps if proto else 12,
        timeout_seconds=proto.timeout_seconds if proto else 1200.0,
    )


def _find(m: MilestoneDraft, roles: Iterable[str]) -> AgentDraft | None:
    for r in roles:
        for a in m.agents:
            if a.role == r:
                return a
    return None


def add_reviewer(m: MilestoneDraft, role: str) -> tuple[MilestoneDraft, str]:
    """Put a read-only role after the implementer; the shape becomes review_then_fix."""
    if role in _roles(m):
        return m, f"{role} already in the shape"
    if m.template_id == "parallel_audit":
        return m, "parallel_audit already audits from two angles"
    author = _find(m, EDITING_DEFAULT)
    test_author = _find(m, ("test_author",))
    if author is None:
        return m, "no writer to review"
    if m.template_id == "review_then_fix":
        agents = [replace(a, role=role, role_id=f"reviewer_{role}"[:48]) if a.role in READ_ONLY else a for a in m.agents]
        return replace(m, agents=agents), f"reviewer angle replaced by {role}"
    fixer = _find(m, ("gate_repairer", "edge_case_hardener"))
    agents: list[AgentDraft] = []
    if test_author is not None:
        agents.append(replace(test_author, slot_id="test_author"))
    agents.append(replace(author, slot_id="author"))
    agents.append(_agent(m, slot="reviewer", role=role, title=f"{role} review",
                         mandate=f"Review the implementer's result as a {role}; report, do not edit."))
    agents.append(replace(fixer, slot_id="fixer") if fixer is not None else _agent(
        m, slot="fixer", role="gate_repairer", title="fixer", mandate="Act on the reviewer's report and the gate's failures."))
    return replace(m, template_id="review_then_fix", agents=agents), f"{m.template_id} -> review_then_fix with {role}"


def add_instruction(m: MilestoneDraft, text: str) -> tuple[MilestoneDraft, str]:
    if not text:
        return m, "no instruction text"
    writer = _find(m, EDITING_DEFAULT)
    if writer is None:
        return m, "no writer to instruct"
    agents = [replace(a, mandate=(a.mandate + "\n\n" + text).strip()) if a is writer else a for a in m.agents]
    return replace(m, agents=agents), "instruction appended to the writer's mandate"


def set_template(m: MilestoneDraft, template_id: str) -> tuple[MilestoneDraft, str]:
    if m.template_id == template_id:
        return m, f"already {template_id}"
    if template_id == "parallel_audit":
        author = _find(m, EDITING_DEFAULT)
        if author is None:
            return m, "no writer"
        test_author = _find(m, ("test_author",))
        fixer = _find(m, ("gate_repairer", "edge_case_hardener"))
        agents = []
        if test_author is not None:
            agents.append(replace(test_author, slot_id="test_author"))
        agents += [
            replace(author, slot_id="author"),
            _agent(m, slot="spec_review", role="spec_auditor", title="spec audit", mandate="Audit the result against the design documents; report only."),
            _agent(m, slot="contract_review", role="contract_critic", title="contract review", mandate="Audit the result against the frozen contracts; report only."),
            replace(fixer, slot_id="fixer") if fixer is not None else _agent(m, slot="fixer", role="gate_repairer", title="fixer", mandate="Act on both reports and the gate's failures."),
        ]
        return replace(m, template_id="parallel_audit", agents=agents), f"{m.template_id} -> parallel_audit"
    if template_id == "chain":
        author = _find(m, EDITING_DEFAULT)
        if author is None:
            return m, "no writer"
        test_author = _find(m, ("test_author",))
        agents = []
        if test_author is not None:
            agents.append(replace(test_author, slot_id="test_author"))
        agents += [
            _agent(m, slot="first", role="contract_author", title="contract pass", mandate="Settle the shared substrate first: bases, protocols, exception types, data shapes."),
            replace(author, slot_id="second", role="implementer" if author.role not in ("implementer", "test_driven_implementer", "integrator") else author.role),
            _agent(m, slot="third", role="integrator", title="integration pass", mandate="Wire the public surface at its documented import paths."),
        ]
        return replace(m, template_id="chain", agents=agents), f"{m.template_id} -> chain"
    return m, f"template {template_id} not handled"


def add_budget(m: MilestoneDraft, steps: int, seconds: int) -> tuple[MilestoneDraft, str]:
    agents = [
        replace(a, max_steps=a.max_steps + int(steps), timeout_seconds=a.timeout_seconds + float(seconds))
        if a.role not in READ_ONLY and a.role != "test_author" else a
        for a in m.agents
    ]
    return replace(m, agents=agents), f"writers +{steps} steps, +{seconds}s"


def apply_entry(m: MilestoneDraft, entry: FEntry, *, features: Mapping[str, Any], thresholds: Mapping[str, Any],
                instructions_dir: Path = INSTRUCTIONS_DIR) -> tuple[MilestoneDraft, list[str]]:
    notes: list[str] = []
    for action in entry.actions:
        kind = action.get("kind")
        if kind == "add_reviewer":
            m, note = add_reviewer(m, str(action.get("role")))
        elif kind == "instruction":
            m, note = add_instruction(m, _instruction(str(action.get("file")), instructions_dir))
        elif kind == "template":
            m, note = set_template(m, str(action.get("template_id")))
        elif kind == "budget":
            m, note = add_budget(m, int(action.get("steps", 0)), int(action.get("seconds", 0)))
        elif kind == "chain":
            key = str(action.get("when_files_at_least", "thr.f5"))
            limit = thresholds.get(key[4:], DEFAULT_THRESHOLDS.get(key[4:], 8)) if key.startswith("thr.") else int(key)
            if int(features.get("n_focus_files") or 0) >= int(limit):
                m, note = set_template(m, "chain")
            else:
                note = "scope below the chain threshold"
        else:
            note = f"unknown action {kind}"
        notes.append(f"{entry.entry_id}: {note}")
    return m, notes


def shape_leaves_an_interface(m: MilestoneDraft) -> bool:
    """§4.4 rule 1: the shape ends in a writer or accepts an appended repairer."""
    return m.template_id in REPAIRABLE_SHAPES


# --------------------------------------------------------------------------- the decision


def matched_entries(features: Mapping[str, Any], table: Iterable[FEntry], thresholds: Mapping[str, Any]) -> list[FEntry]:
    out = []
    for e in table:
        if e.entry_id == "F0" or e.state == "retired" or not e.triggers:
            continue
        holds = [t.holds(features, thresholds) for t in e.triggers]
        if (any(holds) if e.any_trigger else all(holds)):
            out.append(e)
    return out


def _rng(*parts: str) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:16], 16))


def design_milestone(
    m: MilestoneDraft, *, index: int, total: int, docs_text: str, table: Iterable[FEntry],
    thresholds: Mapping[str, Any], seed: str = "", history_freq: Mapping[str, float] | None = None,
    instructions_dir: Path = INSTRUCTIONS_DIR,
) -> tuple[MilestoneDraft, dict[str, Any]]:
    entries = list(table)
    features = milestone_features(m, index=index, total=total, docs_text=docs_text)
    matched = matched_entries(features, entries, thresholds)
    forced = os.environ.get(FORCE_F_ENV, "").strip()
    if forced:
        # A bank re-run (§7.3): this entry (or F0) regardless of state or draw.
        mid, _, entry_id = forced.partition("=") if "=" in forced else ("", "", forced)
        if not mid or mid == m.milestone_id:
            chosen = next((e for e in entries if e.entry_id == entry_id), None)
            decision = {
                "milestone_id": m.milestone_id, "features": features, "matched": [e.entry_id for e in matched],
                "applied": [entry_id], "assignment": "forced", "predicted_error_classes": [],
                "template_before": m.template_id, "template_after": m.template_id, "notes": [f"forced {entry_id}"],
            }
            if chosen is None or entry_id == "F0":
                decision["applied"] = ["F0"]
                return m, decision
            designed, notes = apply_entry(m, chosen, features=features, thresholds=thresholds, instructions_dir=instructions_dir)
            decision["notes"].extend(notes)
            decision["predicted_error_classes"] = list(chosen.predicted_error_classes)
            decision["template_after"] = designed.template_id
            return designed, decision
    decision: dict[str, Any] = {
        "milestone_id": m.milestone_id, "features": features, "matched": [e.entry_id for e in matched],
        "applied": [], "assignment": "deterministic", "predicted_error_classes": [],
        "template_before": m.template_id, "template_after": m.template_id, "notes": [],
    }
    if not matched:
        decision["applied"] = ["F0"]
        return m, decision
    active = [e for e in matched if e.state == "active"]
    trial = [e for e in matched if e.state == "trial"]
    # §4.5: a validated combination is its own entry; otherwise one entry, the
    # one whose predicted class has been most frequent (table order without history)
    combos = [e for e in matched if e.combination_of and e.state == "active"]
    chosen: FEntry | None = None
    if combos:
        chosen = combos[0]
    elif active:
        freq = history_freq or {}
        active.sort(key=lambda e: -max([freq.get(c, 0.0) for c in e.predicted_error_classes] or [0.0]))
        chosen = active[0]
        if len(active) > 1:
            decision["notes"].append("several active entries matched; the combination is not validated, one applied")
    if trial:
        rng = _rng(seed, m.milestone_id)
        decision["assignment"] = "randomized"
        if rng.random() < 0.5:
            chosen = trial[0]
            decision["notes"].append(f"trial entry {trial[0].entry_id} drawn")
        else:
            decision["notes"].append(f"trial entry {trial[0].entry_id} not drawn; " + (chosen.entry_id if chosen else "F0") + " used")
    if chosen is None:
        decision["applied"] = ["F0"]
        return m, decision
    designed, notes = apply_entry(m, chosen, features=features, thresholds=thresholds, instructions_dir=instructions_dir)
    decision["notes"].extend(notes)
    if not shape_leaves_an_interface(designed):
        decision["notes"].append(f"{chosen.entry_id} rejected: the shape leaves no interface for the repairer")
        decision["applied"] = ["F0"]
        return m, decision
    decision["applied"] = [chosen.entry_id]
    decision["predicted_error_classes"] = list(chosen.predicted_error_classes)
    decision["template_after"] = designed.template_id
    return designed, decision


def design_plan(
    draft: MilestonePlanDraft, *, docs_text: str, table: Iterable[FEntry] | None = None,
    thresholds: Mapping[str, Any] | None = None, seed: str = "", history_freq: Mapping[str, float] | None = None,
    instructions_dir: Path = INSTRUCTIONS_DIR,
) -> tuple[MilestonePlanDraft, list[dict[str, Any]]]:
    entries = list(table) if table is not None else list(load_first_pass_table())
    thr = dict(DEFAULT_THRESHOLDS)
    thr.update(thresholds or {})
    total = len(draft.milestones)
    designed: list[MilestoneDraft] = []
    decisions: list[dict[str, Any]] = []
    for i, m in enumerate(draft.milestones):
        d, decision = design_milestone(
            m, index=i, total=total, docs_text=docs_text, table=entries, thresholds=thr, seed=seed,
            history_freq=history_freq, instructions_dir=instructions_dir,
        )
        designed.append(d)
        decisions.append(decision)
    return replace(draft, milestones=designed), decisions


__all__ = [
    "DEFAULT_THRESHOLDS", "FEntry", "FIRST_PASS_TABLE_PATH", "Trigger", "add_budget", "add_instruction",
    "add_reviewer", "apply_entry", "default_first_pass_entries", "design_milestone", "design_plan",
    "load_first_pass_table", "matched_entries", "save_first_pass_table", "set_template",
    "shape_leaves_an_interface",
]
