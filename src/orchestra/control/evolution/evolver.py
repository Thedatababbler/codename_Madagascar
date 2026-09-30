"""The evolver agent (self-evolution spec §9.4): proposes new rows and first-pass entries.

Input comes from the gate and the ledger only (never from held-out): the
per-class row ranking, the unresolved pool clustered by error class, the
row-x-feature combinations worth a preventive version, both current tables,
the role pool, the template library and the structural invariants. Output
is YAML in the §3.1 row format. The model is reached the way the milestone
planner reaches it (the OpenAI-compatible endpoint of the same proxy, the
same model variable), at temperature 0, and prompt and reply are written
to disk. Every proposal passes the validators or is dropped with a reason.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from orchestra.control.evolution.validators import (
    ACTIONS,
    ERROR_CLASSES,
    F_ACTION_KINDS,
    FEATURES,
    READ_ONLY_ROLES,
    validate_f_entry,
    validate_row,
)

EVOLVER_MODEL_ENV = "ADAMAS_EVOLVER_MODEL"
SYSTEM_MESSAGE = (
    "You improve a playbook for a repair loop that fixes failing behaviour tests on milestone-sized coding tasks. "
    "You only propose table rows; you never change the standard procedure, the guards, the test author, the planner "
    "or the evaluator. Reply with YAML only."
)

INVARIANTS = """Structural invariants (proposals violating them are dropped):
- every shape ends in a writer, or accepts a repairer after the gate; read-only roles (contract_critic, spec_auditor, behaviour_critic) never write;
- a slot not named in slot_edits keeps the role the planner gave it; roles are never downgraded;
- S rows carry instruction text; T rows name a target template and nothing else; B rows carry a budget_delta;
- instruction text is generic method guidance: it must not mention any task, module, file, function or test-case name from the training tasks;
- a first-pass entry must cite the repair rows it is the preventive version of (source_rows) and at least one trigger over the listed features;
- new rows and entries start in state candidate."""


@dataclass
class Cluster:
    error_class: str
    cases: list[dict[str, Any]] = field(default_factory=list)  # case, doc_sentence, output_tail, task, milestone

    def to_prompt(self, limit: int = 6) -> str:
        lines = [f"cluster {self.error_class}: {len(self.cases)} unresolved case(s)"]
        for c in self.cases[:limit]:
            lines.append(f"  - case: {c.get('case', '')}")
            if c.get("doc_sentence"):
                lines.append(f"    doc: {str(c['doc_sentence'])[:240]}")
            if c.get("output_tail"):
                lines.append(f"    output: {str(c['output_tail'])[:240]!r}")
        return "\n".join(lines)


def unresolved_pool(candidate_records: Iterable[Mapping[str, Any]]) -> list[Cluster]:
    """Persistent failures no tried row fixed, grouped by error class (§9.4 input)."""
    by_search: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in candidate_records:
        if str(r.get("split") or "train") != "train":
            continue
        by_search[(str(r.get("task_id")), str(r.get("milestone_id")))].append(r)
    clusters: dict[str, Cluster] = {}
    for (task, mid), recs in by_search.items():
        fixed_anywhere: set[str] = set()
        persistent: set[str] = set()
        classes: dict[str, str] = {}
        for r in recs:
            fixed_anywhere.update(str(x) for x in (r.get("fixed") or ()))
            persistent.update(str(x) for x in (r.get("persistent_before") or ()))
            for case, cls in (r.get("case_classes") or {}).items():
                classes[str(case)] = str(cls)
        for case in sorted(persistent - fixed_anywhere):
            cls = classes.get(case) or (list(recs[0].get("error_classes") or ["E3"])[0])
            clusters.setdefault(cls, Cluster(cls)).cases.append({
                "case": case, "task": task, "milestone": mid,
                "doc_sentence": (recs[0].get("doc_sentences") or {}).get(case, ""),
                "output_tail": (recs[0].get("failure_output") or {}).get(case, ""),
            })
    return [clusters[k] for k in sorted(clusters)]


def combinations_worth_preventing(
    ranking: Mapping[str, Mapping[str, float]], milestone_records: Iterable[Mapping[str, Any]], *, min_score: float = 0.5,
    min_share: float = 0.3,
) -> list[dict[str, Any]]:
    """Rows scoring high on a class that appears often on milestones of one kind (§9.4 input, third item)."""
    kinds: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[str, int] = defaultdict(int)
    for r in milestone_records:
        kind = str((r.get("features") or {}).get("kind") or "")
        if not kind:
            continue
        totals[kind] += 1
        for cls in set(r.get("error_classes") or ()):
            kinds[kind][str(cls)] += 1
    out = []
    for kind, per_class in kinds.items():
        for cls, n in per_class.items():
            share = n / totals[kind] if totals[kind] else 0.0
            if share < min_share:
                continue
            for row, score in (ranking.get(cls) or {}).items():
                if score >= min_score:
                    out.append({"row_id": row, "error_class": cls, "score": score, "milestone_kind": kind, "share": round(share, 2)})
    return sorted(out, key=lambda d: (-d["score"], d["row_id"]))


def render_prompt(
    *, ranking: Mapping[str, Mapping[str, float]], stats_summary: Mapping[str, Any], clusters: Iterable[Cluster],
    combinations: Iterable[Mapping[str, Any]], repair_rows: Iterable[Mapping[str, Any]], f_entries: Iterable[Mapping[str, Any]],
    roles: Iterable[str], templates: Iterable[str], max_proposals: int,
) -> str:
    rows_txt = yaml.safe_dump([dict(r) for r in repair_rows], sort_keys=False, allow_unicode=True)
    entries_txt = yaml.safe_dump([dict(e) for e in f_entries], sort_keys=False, allow_unicode=True)
    parts = [
        "# Ranking per error class (shrinkage score vs R0; higher is better)",
        json.dumps(ranking, indent=1, sort_keys=True),
        "# Row statistics",
        json.dumps(stats_summary, indent=1, sort_keys=True),
        "# Unresolved pool (persistent failures no tried row fixed)",
        "\n".join(c.to_prompt() for c in clusters) or "(empty)",
        "# Row x milestone-kind combinations worth a preventive first-pass version",
        json.dumps(list(combinations), indent=1) or "[]",
        "# Current repair table", rows_txt,
        "# Current first-pass table", entries_txt,
        "# Role pool: " + ", ".join(sorted(roles)),
        "# Read-only roles: " + ", ".join(READ_ONLY_ROLES),
        "# Template library: " + ", ".join(sorted(templates)),
        "# Features available to triggers: " + ", ".join(FEATURES),
        "# Error classes: " + ", ".join(ERROR_CLASSES) + "; actions: " + ", ".join(ACTIONS)
        + "; first-pass action kinds: " + ", ".join(F_ACTION_KINDS),
        INVARIANTS,
        f"""# Task
Propose at most {max_proposals} items in total. Each item targets one cluster of the unresolved pool
(state `targets: cluster:<error_class>`) or is the preventive version of an existing row
(state `targets: row:<row_id>` and cite it in source_rows). Prefer S rows (method instructions) for
clusters where the failing cases share a cause, T/R rows only when a different shape or role plausibly
changes the outcome. Reply with exactly this YAML document and nothing else:

rows:
  - row_id: <class>-<action><n>   # e.g. E5-S2; the number must be unused
    table: repair
    error_classes: [E5]
    action: S
    target_template: null
    slot_edits: []                 # for R rows: [{{slot: reviewer, role: contract_critic, read_only: true}}]
    instruction: |
      <generic method guidance for the writer>
    evidence_routing: [improver]
    budget_delta: null
    preconditions: []
    intent: <one sentence>
    targets: cluster:E5
f_entries:
  - entry_id: F<n>
    triggers: [{{feature: n_documented_exceptions, op: ">=", value: thr.f2}}]
    predicted_error_classes: [E5]
    actions: [{{kind: instruction, text: <generic guidance>}}]
    source_rows: [E5-S1]
    intent: <one sentence>
    targets: row:E5-S1
""",
    ]
    return "\n\n".join(parts)


def parse_reply(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    data = yaml.safe_load(body) or {}
    if not isinstance(data, Mapping):
        return [], []
    rows = [dict(r) for r in (data.get("rows") or ()) if isinstance(r, Mapping)]
    entries = [dict(e) for e in (data.get("f_entries") or ()) if isinstance(e, Mapping)]
    return rows, entries


def default_call(system: str, prompt: str, *, timeout: float = 600.0) -> str:
    """The planner's route: OpenAI-compatible chat completion at temperature 0."""
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("OPENAI_API_KEY and OPENAI_BASE_URL are required for the evolver")
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"), timeout=timeout)
    model = os.getenv(EVOLVER_MODEL_ENV) or os.getenv("CODEX_MODEL") or os.getenv("SMOLAGENTS_MODEL") or "gpt-5.4"
    response = client.chat.completions.create(
        model=model, temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
    )
    return (response.choices[0].message.content or "").strip()


@dataclass
class Proposals:
    rows: list[dict[str, Any]] = field(default_factory=list)
    f_entries: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    prompt_path: str = ""
    reply_path: str = ""


def propose(
    *, prompt: str, out_dir: Path, cycle_id: str, roles: frozenset[str], templates: frozenset[str],
    identifiers: frozenset[str], repair_row_ids: Iterable[str], f_entry_ids: Iterable[str], max_proposals: int,
    call: Callable[[str, str], str] | None = None,
) -> Proposals:
    """Ask the model once, write prompt and reply beside the cycle, validate, and tag accepted items."""
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = out_dir / "evolver_prompt.md"
    reply_path = out_dir / "evolver_reply.yaml"
    prompt_path.write_text(prompt, encoding="utf-8")
    reply = (call or default_call)(SYSTEM_MESSAGE, prompt)
    reply_path.write_text(reply, encoding="utf-8")
    rows, entries = parse_reply(reply)
    result = Proposals(prompt_path=str(prompt_path), reply_path=str(reply_path))
    existing_rows = set(repair_row_ids)
    existing_entries = set(f_entry_ids)
    budget = max_proposals
    for row in rows:
        if budget <= 0:
            result.rejected.append({"item": row.get("row_id"), "reasons": ["over max_proposals"]})
            continue
        verdict = validate_row(row, roles=roles, templates=templates, identifiers=identifiers, existing_ids=existing_rows)
        if not verdict.ok:
            result.rejected.append({"item": row.get("row_id"), "reasons": verdict.reasons})
            continue
        targets = str(row.pop("targets", "") or "")
        row["origin"] = f"evolver:{cycle_id}"
        row["state"] = "candidate"
        row["intent"] = f"{row.get('intent', '')} [{targets}]".strip()
        row.setdefault("table", "repair")
        result.rows.append(row)
        existing_rows.add(str(row["row_id"]))
        budget -= 1
    for entry in entries:
        if budget <= 0:
            result.rejected.append({"item": entry.get("entry_id"), "reasons": ["over max_proposals"]})
            continue
        verdict = validate_f_entry(entry, roles=roles, templates=templates, identifiers=identifiers,
                                   repair_row_ids=existing_rows, existing_ids=existing_entries)
        if not verdict.ok:
            result.rejected.append({"item": entry.get("entry_id"), "reasons": verdict.reasons})
            continue
        targets = str(entry.pop("targets", "") or "")
        entry["origin"] = f"evolver:{cycle_id}"
        entry["state"] = "candidate"
        entry["intent"] = f"{entry.get('intent', '')} [{targets}]".strip()
        result.f_entries.append(entry)
        existing_entries.add(str(entry["entry_id"]))
        budget -= 1
    (out_dir / "evolver_verdicts.json").write_text(
        json.dumps({"accepted_rows": [r["row_id"] for r in result.rows], "accepted_entries": [e["entry_id"] for e in result.f_entries],
                    "rejected": result.rejected}, indent=1), encoding="utf-8")
    return result


__all__ = [
    "Cluster", "EVOLVER_MODEL_ENV", "INVARIANTS", "Proposals", "SYSTEM_MESSAGE", "combinations_worth_preventing",
    "default_call", "parse_reply", "propose", "render_prompt", "unresolved_pool",
]
