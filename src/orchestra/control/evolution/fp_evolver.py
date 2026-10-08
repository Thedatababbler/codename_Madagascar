"""First-pass evolver for the joint experiment's real iteration (user's spec of 2026-10-08).

Each round it reads the current best first-pass design, every tested design with its gate and
verification results (clustered by error class), the residual failures under the current best with
the documented sentences they cite, the playbook rows and their known results, and proposes one or
two new first-pass designs. Held-out appears only as counts of stable fixes / stable regressions.
Temperature 0; prompt and reply are written to disk. Proposals are validated before anything runs.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from orchestra.control.evolution.validators import (
    READ_ONLY_ROLES,
    available_roles,
    available_templates,
    leaked_identifiers,
)

DESIGN_FEATURES = ("kind", "n_focus_files", "n_public_symbols", "n_documented_exceptions", "n_state_transitions",
                   "n_public_classes", "dep_depth")
ACTION_KINDS = ("add_reviewer", "instruction", "template", "budget", "chain", "inventory")
INVENTORY_KINDS = ("main_path", "boundary", "state_transition", "error_path", "integration", "protocol")
MAX_INSTRUCTION_CHARS = 2500

SYSTEM = """You design first-pass prevention for a multi-agent coding system. A milestone's first run is the
planner's subgraph (agents writing code from design documents) followed by one acceptance gate. A first-pass
design changes that subgraph before it runs: it can give the writer an instruction, hand it the milestone's
documented-behaviour inventory, add a read-only reviewer, switch the template, or change the budget.
Your designs must be task-agnostic: never name a project, package, module, file, class, function or test.
Output JSON only."""

PROMPT = """## What you can change

Actions (combine any):
- {{"kind": "instruction", "text": "<appended to the writer's mandate, <= {max_chars} chars>"}}
- {{"kind": "inventory", "kinds": [<subset of {inv_kinds}>]}}  -- the milestone's documented behaviours of these kinds, quoted from the documents, appended to the writer's mandate
- {{"kind": "add_reviewer", "role": "<one of {readonly}>"}}  -- a read-only reviewer after the writer; its findings go to a repairer
- {{"kind": "template", "template_id": "<one of {templates}>"}}
- {{"kind": "budget", "steps": <extra agent steps>, "seconds": <extra seconds>}}

Triggers use these milestone features: {features} (kind is foundation / middle / integration).
Error classes: E1 unfinished, E2 interface, E3 main path, E4 state transition, E5 error path, E6 boundary,
E7 integration, E8 performance, E9 scattered.

## The current best first-pass design
{best}

## Every design tested so far, with results (evolution checkpoints; 2 first runs per design per checkpoint)
{history}

## Failures still left under the current best design, by error class, with the sentences the failing tests cite
{residual}

## Repair playbook rows (a row's way of fixing can be rewritten as a first-pass prevention) and what is known about them
{rows}

## Facts from the previous experiment
- Every repair pair (the default repair against a playbook row, on the same incumbent) tied on the gate, on an
  independent verification suite and on the held-out tests; rows only cost more.
- Handing the writer the behaviour inventory (main path, boundary, error path) halved the gate failures of
  first runs, but its held-out improvement stayed below the bar.

## Your task
Propose 1 or 2 new first-pass designs that should make the first run right more often on milestones like these.
For each, say how it differs from every design already tested and why the latest results call for this change.
Never resubmit a rejected design. Return a JSON array:
[{{"triggers": [{{"feature": "kind", "op": "==", "value": "foundation"}}],
   "predicted_error_classes": ["E3"],
   "actions": [...],
   "source": {{"based_on": "<tested design id or none>", "playbook_row": "<row id or none>", "residual_cluster": "<error class or none>"}},
   "intent": "<one sentence: what it does>",
   "differs_from_previous": "<what is new compared with every tested design>",
   "why_now": "<which result of the last round motivates it>"}}]
"""


def render(*, best: str, history: str, residual: str, rows: str, roles: Iterable[str], templates: Iterable[str]) -> str:
    return PROMPT.format(max_chars=MAX_INSTRUCTION_CHARS, inv_kinds=list(INVENTORY_KINDS),
                         readonly=[r for r in READ_ONLY_ROLES if r in set(roles)], templates=sorted(templates),
                         features=list(DESIGN_FEATURES), best=best, history=history, residual=residual, rows=rows)


def parse(reply: str) -> list[dict]:
    m = re.search(r"\[.*\]", reply, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)][:2]


def weak_leaks(text: str, weak: frozenset[str]) -> list[str]:
    """Single plain words that are also module / file names count only when written as code
    (``name.py``, ``/name``, or in backticks): as English words ("state", "main") they identify nothing."""
    out = []
    for w in weak:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(w)}\.py\b|/{re.escape(w)}\b|`{re.escape(w)}`", text, re.I):
            out.append(w)
    return sorted(out)


def validate(design: Mapping[str, Any], *, identifiers: frozenset[str], roles: frozenset[str] | None = None,
             templates: frozenset[str] | None = None, rejected: Iterable[Mapping[str, Any]] = (),
             shape_check: Callable[[dict], list[str]] | None = None, weak: frozenset[str] = frozenset()) -> list[str]:
    """Reasons the design is invalid ([] = valid)."""
    roles = roles or available_roles()
    templates = templates or available_templates()
    reasons: list[str] = []
    triggers = design.get("triggers") or []
    if not triggers:
        reasons.append("at least one trigger is required")
    for t in triggers:
        if str((t or {}).get("feature")) not in DESIGN_FEATURES:
            reasons.append(f"unknown feature {(t or {}).get('feature')!r}")
        if str((t or {}).get("op")) not in ("==", "!=", ">=", "<=", ">", "<"):
            reasons.append(f"bad operator {(t or {}).get('op')!r}")
    classes = [str(c) for c in design.get("predicted_error_classes") or []]
    if not classes or any(not re.fullmatch(r"E[1-9]", c) for c in classes):
        reasons.append(f"predicted_error_classes must be E1-E9, got {classes}")
    actions = design.get("actions") or []
    if not actions:
        reasons.append("at least one action is required")
    texts = [str(design.get(k) or "") for k in ("intent", "differs_from_previous", "why_now")]
    for a in actions:
        kind = str((a or {}).get("kind"))
        if kind not in ACTION_KINDS:
            reasons.append(f"unknown action kind {kind!r}")
        elif kind == "instruction":
            text = str(a.get("text") or "")
            if not text.strip():
                reasons.append("instruction without text")
            if len(text) > MAX_INSTRUCTION_CHARS:
                reasons.append(f"instruction longer than {MAX_INSTRUCTION_CHARS} chars")
            texts.append(text)
        elif kind == "inventory":
            bad = [k for k in a.get("kinds") or [] if k not in INVENTORY_KINDS]
            if bad or not a.get("kinds"):
                reasons.append(f"inventory kinds must be a non-empty subset of {INVENTORY_KINDS}, got {a.get('kinds')}")
        elif kind == "add_reviewer":
            role = str(a.get("role"))
            if role not in roles or role not in READ_ONLY_ROLES:
                reasons.append(f"add_reviewer needs a read-only role from the pool, got {role!r}")
        elif kind == "template" and str(a.get("template_id")) not in templates:
            reasons.append(f"unknown template {a.get('template_id')!r}")
        elif kind == "budget" and not (a.get("steps") or a.get("seconds")):
            reasons.append("budget action needs steps or seconds")
    for t in texts:
        leaks = leaked_identifiers(t, identifiers) + weak_leaks(t, weak)
        if leaks:
            reasons.append(f"mentions training-task identifiers: {leaks}")
    for r in rejected:
        if _same(design, r):
            reasons.append(f"resubmits rejected design {r.get('entry_id')}")
    if not reasons and shape_check is not None:
        reasons += shape_check(dict(design))
    return reasons


def _norm_actions(d: Mapping[str, Any]) -> str:
    return json.dumps(sorted((json.dumps(a, sort_keys=True) for a in d.get("actions") or [])))


def _same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return _norm_actions(a) == _norm_actions(b)


def shape_checker(milestones: list[Any]) -> Callable[[dict], list[str]]:
    """Apply the design to each milestone draft, compile its subgraph and check the graph invariants
    (single writer, custody before consumers, a writer or repairer at the end)."""

    def check(design: dict) -> list[str]:
        import orchestra.control.fast_loop  # noqa: F401  (import order: the builder is imported through it)
        from orchestra.control.first_pass.designer import FEntry, apply_entry
        from orchestra.ir.graph import load_graph
        from orchestra.ir.graph_invariants import check_graph_invariants
        from orchestra.realbench.subgraph_builder import (
            CODEPROJECTEVAL_PROMPT_PROFILE,
            materialize_milestone_subgraph,
            prepare_generated_root,
        )
        from orchestra.roles.pool import default_role_pool

        reasons: list[str] = []
        entry = FEntry.from_dict({"entry_id": "Fcheck", "triggers": design.get("triggers") or [],
                                  "predicted_error_classes": design.get("predicted_error_classes") or [],
                                  "actions": design.get("actions") or [], "source_rows": [], "state": "candidate"})
        for m in milestones:
            try:
                designed, _ = apply_entry(m, entry, features={"n_focus_files": len(m.focus_paths or [])}, thresholds={})
                root = prepare_generated_root(Path(tempfile.mkdtemp()), base_contracts_dir="configs/contracts")
                path, _roster = materialize_milestone_subgraph(
                    generated_root=root, milestone=designed, agent_backend="codex_sdk",
                    harness_command=["python", "check.py", "--spec-tests", "/tmp/frozen"],
                    profile=CODEPROJECTEVAL_PROMPT_PROFILE)
                violations = check_graph_invariants(load_graph(path), pool=default_role_pool())
                for v in violations:
                    reasons.append(f"{v.invariant}: {getattr(v, 'detail', '') or getattr(v, 'message', '')}"[:200])
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"does not compile: {type(exc).__name__}: {str(exc)[:160]}")
        return sorted(set(reasons))

    return check


def propose(*, context: Mapping[str, str], out_dir: Path, tag: str, identifiers: frozenset[str],
            rejected: list[dict], shape_check: Callable[[dict], list[str]] | None,
            call: Callable[[str, str], str] | None = None, weak: frozenset[str] = frozenset()) -> tuple[list[dict], list[dict]]:
    """(valid designs, discarded designs with reasons). One automatic retry with the reasons."""
    from orchestra.control.evolution.evolver import default_call

    roles, templates = available_roles(), available_templates()
    prompt = render(roles=roles, templates=templates, **context)
    valid: list[dict] = []
    discarded: list[dict] = []
    feedback = ""
    for attempt in range(2):
        text = prompt + (f"\n\n## Your previous answer was rejected\n{feedback}\nFix every point." if feedback else "")
        (out_dir / f"{tag}.a{attempt}.prompt.txt").write_text(SYSTEM + "\n\n" + text, encoding="utf-8")
        reply = (call or default_call)(SYSTEM, text)
        (out_dir / f"{tag}.a{attempt}.reply.txt").write_text(reply, encoding="utf-8")
        designs = parse(reply)
        valid, bad = [], []
        for d in designs:
            reasons = validate(d, identifiers=identifiers, roles=roles, templates=templates, rejected=rejected,
                               shape_check=shape_check, weak=weak)
            (bad if reasons else valid).append({**d, "_reasons": reasons} if reasons else d)
        discarded += [{**b, "attempt": attempt} for b in bad]
        if valid or not designs and attempt == 1:
            break
        feedback = "\n".join(f"- {', '.join(b['_reasons'])}" for b in bad) or "- no JSON array was found"
    return valid, discarded


__all__ = ["weak_leaks", "ACTION_KINDS", "DESIGN_FEATURES", "parse", "propose", "render", "shape_checker", "validate"]
