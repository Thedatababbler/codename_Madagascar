"""LLM classification of a failed milestone, falling back to the lookup.

The lookup in ``diagnosis.py`` maps a ``SubtaskFailureReason`` to a hardcoded
edit list. This module keeps that lookup as the baseline and, when asked, asks
an LLM only for a class, a confidence, an anchor node and a rationale. It
never proposes edits: a class that invents a role or a node costs a real
execution to discover, and the playbook table is the whole reachable space.

Four properties are not negotiable. The diagnoser sees what the gate saw and
nothing else — not a held-out suite, not ``proj_with_test``, not test source.
Calls are ``temperature=0`` and the prompt plus response are written beside
the candidate records. Spend is recorded as ``accounting_source=
"fast_loop_diagnosis"``. Unavailable model, unparseable JSON, a class outside
the enum or confidence below the floor all fall back to the lookup; diagnosis
failure must not fail the milestone.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from orchestra.control.backend_usage import (
    BackendUsageRecord,
    derive_cost_usd,
    exception_usage_record,
)
from orchestra.control.fast_loop.playbooks import FailureClass, infer_failure_class
from orchestra.control.fast_loop.schemas import FailureDiagnosis
from orchestra.control.task_state import SubtaskState
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import AgentNodeSpec, NodeKind
from orchestra.roles.pool import RolePool, default_role_pool

ACCOUNTING_SOURCE = "fast_loop_diagnosis"
DEFAULT_MODEL = "gpt-5.4"

#: Paths and names that must never enter the diagnoser prompt. A leak here
#: voids finished arms, not just the current one.
_HELD_OUT_MARKERS = (
    "proj_with_test",
    "held_out",
    "held-out",
    "private_test",
    "hidden_test",
    "hidden_tests",
    "check_tests/",
    "private_evaluator",
)

_SYSTEM_PROMPT = """You classify why a repository milestone failed. Emit only JSON.

You never invent tests, never ask for a private or held-out suite, and never
refer to files you have not been shown. The only evidence you have is in the
user message. The gate's failing test names are names, not source.

Classes:
- budget: the agent did not finish, or finished by abandoning work — timeout,
  steps exhausted, truncated output, TODO / NotImplementedError left behind,
  imports resolve but bodies are empty. Evidence: exit signal, steps at cap,
  furthest_stage in compile or imports.
- functional: the agent finished and the structure is complete, but the
  behaviour is wrong. Evidence: furthest_stage reached tests or spec_tests
  with a non-empty failure list.
- design: failures spread across unrelated areas, or concentrate where this
  milestone was not meant to be responsible, or the same repair has already
  failed to move them twice.

When the message says the gate PASSED, the milestone is being refined, not
repaired: budget then needs evidence of truncation (an exit signal, steps at
the cap); a finished agent whose behaviour is merely wrong is functional.

When a "Persistent failures" section is present, those behaviours failed in
every independent attempt listed; behaviours that flipped between attempts
were removed. Diagnose the persistent set only.

Also name the specialist the next attempt should hand these failures to.
recommended_role must be an id from the EDITING roles in the role pool
section, chosen for what the persistent failures actually require -- a
misread of the documented behaviour wants the role that writes from the
design documents, a missing or unreachable public symbol wants the one that
wires the public surface, a corner-case regression wants the hardener. Do not
default to the hardener for failures that are not about corner cases.
recommended_reviewer is optional: a READ-ONLY role whose report would help
that specialist, or empty.

Output exactly this object and nothing else:
{"failure_class":"budget|functional|design","confidence":0.0,"target_node_id":"","recommended_role":"","recommended_reviewer":"","rationale":"","evidence":[]}
confidence is in [0, 1]. target_node_id must be one of the agent node ids in
the subgraph summary, or empty to keep the lookup's anchor. A role id not in
the pool, or of the wrong kind, is discarded.
"""


@dataclass(frozen=True)
class DiagnosisConfig:
    """How the fast loop classifies a failure, if it classifies at all."""

    mode: str = "deterministic"
    min_confidence: float = 0.5
    model: str = DEFAULT_MODEL

    @classmethod
    def from_mapping(cls, raw: Any) -> DiagnosisConfig:
        payload = dict(raw or {})
        mode = str(payload.get("mode") or "deterministic").strip().lower()
        if mode not in {"llm", "deterministic"}:
            raise ValueError(f"unknown diagnosis mode {mode!r}")
        confidence = float(payload.get("min_confidence", 0.5))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("diagnosis.min_confidence must be in [0, 1]")
        model = str(payload.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        return cls(mode=mode, min_confidence=confidence, model=model)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "min_confidence": self.min_confidence,
            "model": self.model,
        }


@dataclass(frozen=True)
class DiagnosisCompletion:
    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str = ""


class DiagnosisClient(Protocol):
    def complete(
        self, *, model: str, messages: list[dict[str, str]]
    ) -> DiagnosisCompletion: ...


@dataclass
class DiagnosisCall:
    """What happened when we asked, whether or not the class was applied."""

    applied: bool
    fallback_reason: str = ""
    #: Whether a recommended_role from the model was adopted (not pre-empted
    #: by the rule floor and inside the pool).
    role_applied: bool = False
    prompt: dict[str, str] = field(default_factory=dict)
    response: str = ""
    parsed: dict[str, Any] | None = None
    usage: BackendUsageRecord | None = None


class OpenAIDiagnosisClient:
    """The same OpenAI-compatible client the planner uses, with usage attached."""

    def complete(
        self, *, model: str, messages: list[dict[str, str]]
    ) -> DiagnosisCompletion:
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")
        if not api_key or not base_url:
            raise RuntimeError("OPENAI_API_KEY or OPENAI_BASE_URL is missing")
        client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=messages,
        )
        usage = getattr(response, "usage", None)
        return DiagnosisCompletion(
            content=(response.choices[0].message.content or "").strip(),
            prompt_tokens=_optional_int(getattr(usage, "prompt_tokens", None)),
            completion_tokens=_optional_int(getattr(usage, "completion_tokens", None)),
            model=model,
        )


def refine_diagnosis(
    *,
    lookup: FailureDiagnosis,
    graph: OrchestraGraph,
    subtask_state: SubtaskState,
    config: DiagnosisConfig,
    remaining: Mapping[str, Any] | None = None,
    playbook_history: list[str] | None = None,
    client: DiagnosisClient | None = None,
    artifact_dir: Path | None = None,
    task_id: str = "",
    subtask_id: str = "",
    attempt_id: int = 0,
    pool: RolePool | None = None,
) -> tuple[FailureDiagnosis, DiagnosisCall]:
    """Apply an LLM class on top of ``lookup``, or return ``lookup`` unchanged.

    Always returns a diagnosis that is safe to generate from. The call record
    says whether the class was applied and, if a provider was reached, how
    much it cost.
    """
    call = DiagnosisCall(applied=False)
    if config.mode != "llm":
        call.fallback_reason = "deterministic"
        return lookup, call
    if lookup.infrastructure_related or not lookup.retryable:
        call.fallback_reason = "not_classifiable"
        return lookup, call

    role_pool = pool or default_role_pool()
    user_prompt = build_diagnosis_prompt(
        lookup=lookup,
        graph=graph,
        subtask_state=subtask_state,
        remaining=remaining or {},
        playbook_history=playbook_history or [],
        pool=role_pool,
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    call.prompt = {"system": _SYSTEM_PROMPT, "user": user_prompt}

    started = datetime.now(UTC)
    try:
        completion = (client or OpenAIDiagnosisClient()).complete(
            model=config.model, messages=messages
        )
    except Exception as exc:  # noqa: BLE001 — fail closed to the lookup
        finished = datetime.now(UTC)
        call.fallback_reason = f"unavailable:{type(exc).__name__}"
        call.usage = exception_usage_record(
            task_id=task_id,
            subtask_id=subtask_id,
            attempt_id=attempt_id,
            node_id="__diagnosis__",
            backend_id="openai_compatible",
            started_at=started,
            finished_at=finished,
            status=f"{type(exc).__name__}: {exc}",
            accounting_source=ACCOUNTING_SOURCE,
        )
        _write_artifact(artifact_dir, subtask_id, attempt_id, call, lookup, lookup)
        return lookup, call

    finished = datetime.now(UTC)
    call.response = completion.content
    call.usage = _usage_record(
        task_id=task_id,
        subtask_id=subtask_id,
        attempt_id=attempt_id,
        started=started,
        finished=finished,
        completion=completion,
    )
    parsed = _parse_classification(completion.content)
    if parsed is None:
        call.fallback_reason = "unparseable"
        _write_artifact(artifact_dir, subtask_id, attempt_id, call, lookup, lookup)
        return lookup, call
    call.parsed = parsed

    failure_class = str(parsed.get("failure_class") or "").strip()
    if failure_class not in {item.value for item in FailureClass}:
        call.fallback_reason = f"unknown_class:{failure_class}"
        _write_artifact(artifact_dir, subtask_id, attempt_id, call, lookup, lookup)
        return lookup, call

    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        call.fallback_reason = "bad_confidence"
        _write_artifact(artifact_dir, subtask_id, attempt_id, call, lookup, lookup)
        return lookup, call
    if confidence < config.min_confidence:
        call.fallback_reason = f"low_confidence:{confidence}"
        _write_artifact(artifact_dir, subtask_id, attempt_id, call, lookup, lookup)
        return lookup, call

    # Self-consistency against evidence the model cannot argue with: a run that
    # reached the behavioural tests and shows no exit signal did not run out of
    # budget, whatever the rationale says. Cheaper than a wasted candidate.
    if (
        failure_class == FailureClass.BUDGET.value
        and str(lookup.furthest_stage).lower() in {"tests", "spec_tests"}
        and _exit_signal(lookup.concise_feedback) == "(none)"
    ):
        call.fallback_reason = "inconsistent:budget_without_signal"
        _write_artifact(artifact_dir, subtask_id, attempt_id, call, lookup, lookup)
        return lookup, call

    target = str(parsed.get("target_node_id") or "").strip()
    agent_ids = {node.node_id for node in graph.nodes if node.node_kind is NodeKind.AGENT}
    if target and target not in agent_ids:
        target = ""

    # The role space is closed: an id outside the pool, or a read-only role
    # offered as the writer, is dropped rather than trusted. What survives is
    # executable by construction; whether it was *right* is the ledger's job.
    role = _validated_role(parsed.get("recommended_role"), role_pool, editing=True)
    reviewer = _validated_role(parsed.get("recommended_reviewer"), role_pool, editing=False)
    if role and not lookup.recommended_role:
        call.role_applied = True

    refined = lookup.model_copy(
        update={
            "failure_class": failure_class,
            "primary_failed_node_id": target or lookup.primary_failed_node_id,
            "diagnosis_source": "llm",
            "diagnosis_confidence": confidence,
            "diagnosis_rationale": str(parsed.get("rationale") or "")[:2000],
            "recommended_role": lookup.recommended_role or role,
            "recommended_reviewer": lookup.recommended_reviewer or reviewer,
            "role_source": lookup.role_source or ("llm" if role else ""),
        }
    )
    call.applied = True
    _write_artifact(artifact_dir, subtask_id, attempt_id, call, lookup, refined)
    return refined, call


def build_diagnosis_prompt(
    *,
    lookup: FailureDiagnosis,
    graph: OrchestraGraph,
    subtask_state: SubtaskState,
    remaining: Mapping[str, Any],
    playbook_history: list[str],
    pool: RolePool,
) -> str:
    """The user message. Built only from what the gate and the graph already know."""
    stages = _latest_harness_stages(subtask_state)
    sections = [
        "# Failure",
        f"reason: {lookup.reason}",
        f"exit_signal: {_exit_signal(lookup.concise_feedback)}",
        f"furthest_stage: {lookup.furthest_stage or '(none)'}",
        f"lookup_class: {infer_failure_class(lookup).value}",
        f"feedback: {lookup.concise_feedback}",
        "",
        "# Gate stages",
        _format_stages(stages) or "(none recorded)",
        "",
        *_failures_section(lookup),
        "",
        "# Role pool",
        _format_role_pool(pool),
        "",
        "# Subgraph",
        _format_subgraph(graph, pool),
        "",
        "# Remaining milestone budget",
        _format_remaining(remaining),
        "",
        "# Earlier playbooks on this milestone",
        _bullets(playbook_history) or "(none)",
    ]
    prompt = "\n".join(sections)
    return _strip_held_out(prompt)


def _format_subgraph(graph: OrchestraGraph, pool: RolePool) -> str:
    lines = [
        f"template_id: {str((graph.metadata or {}).get('template_id') or '') or '(unknown)'}"
    ]
    for node in graph.nodes:
        if node.node_kind is not NodeKind.AGENT:
            continue
        assert isinstance(node, AgentNodeSpec)
        role = pool.role_for_node_id(node.node_id)
        model = node.model.name if node.model else ""
        backend = node.resolved_backend()
        steps = getattr(backend, "max_steps", None)
        lines.append(
            f"- {node.node_id}: role={role.role_id if role else '?'}, "
            f"model={model or '?'}, max_steps={steps if steps is not None else '?'}, "
            f"timeout_seconds={node.timeout_seconds}"
        )
    return "\n".join(lines) if len(lines) > 1 else lines[0] + "\n(no agents)"


def _format_stages(stages: list[Any]) -> str:
    lines: list[str] = []
    for entry in stages:
        if isinstance(entry, Mapping):
            name = str(entry.get("stage") or "")
            passed = entry.get("passed_units")
            total = entry.get("total_units")
            failed = [str(t) for t in (entry.get("failed_tests") or [])]
        else:
            name = str(getattr(entry, "stage", "") or "")
            passed = getattr(entry, "passed_units", None)
            total = getattr(entry, "total_units", None)
            failed = [str(t) for t in (getattr(entry, "failed_tests", None) or [])]
        if not name:
            continue
        extra = f" failed={failed}" if failed else ""
        lines.append(f"- {name}: {passed}/{total}{extra}")
    return "\n".join(lines)


def _latest_harness_stages(subtask_state: SubtaskState) -> list[Any]:
    stages: list[Any] = []
    for attempt in subtask_state.attempts:
        raw = (attempt.metadata or {}).get("harness_stages")
        if isinstance(raw, list) and raw:
            stages = raw
    return stages


def _exit_signal(feedback: str) -> str:
    match = re.search(r"\b(SIG[A-Z]+|TimeoutError|TimeoutExpired)\b", feedback or "")
    return match.group(1) if match else "(none)"


def _format_remaining(remaining: Mapping[str, Any]) -> str:
    if not remaining:
        return "(not provided)"
    return "\n".join(f"- {key}: {remaining[key]}" for key in remaining)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items if item)


def _failures_section(lookup: FailureDiagnosis) -> list[str]:
    names = [_short_failure(n) for n in lookup.behaviour_failures]
    gate = "PASSED" if lookup.behaviour_total and not lookup.failed_node_ids else "FAILED"
    totals = ""
    if lookup.behaviour_total is not None and lookup.behaviour_passed is not None:
        totals = f" ({lookup.behaviour_passed}/{lookup.behaviour_total} behaviours passed)"
    if lookup.persistence_samples > 1:
        return [
            f"# Persistent failures -- gate {gate}{totals}",
            f"Failed in every one of {lookup.persistence_samples} independent "
            "attempts at this milestone; flipping behaviours were removed.",
            _bullets(names) or "(none persistent)",
        ]
    return [f"# Named behavioural failures -- gate {gate}{totals}", _bullets(names) or "(none named)"]


def _short_failure(name: str) -> str:
    return str(name or "").strip().rsplit("/", 1)[-1]


def _format_role_pool(pool: RolePool) -> str:
    editing, reading = [], []
    for role in pool.ordered():
        line = f"- {role.role_id}: {' '.join(str(role.capability).split())[:140]}"
        (editing if role.edits_repository else reading).append(line)
    return "\n".join(["EDITING roles (recommended_role):", *editing, "READ-ONLY roles (recommended_reviewer):", *reading])


def _validated_role(value: Any, pool: RolePool, *, editing: bool) -> str:
    role_id = str(value or "").strip()
    if not role_id:
        return ""
    role = pool.get(role_id)
    if role is None or bool(role.edits_repository) is not editing:
        return ""
    return role_id


def _strip_held_out(text: str) -> str:
    """Drop any line that names a held-out suite, rather than refuse the call.

    A diagnosis that crashes because a failure message happened to mention a
    path would fail the milestone. Stripping the line keeps the rest of the
    evidence and leaves nothing the diagnoser is forbidden to see.
    """
    kept = [
        line
        for line in text.splitlines()
        if not any(marker in line.lower() for marker in _HELD_OUT_MARKERS)
    ]
    return "\n".join(kept)


def _parse_classification(payload: str) -> dict[str, Any] | None:
    match = re.search(r"\{[\s\S]*\}", payload or "")
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _usage_record(
    *,
    task_id: str,
    subtask_id: str,
    attempt_id: int,
    started: datetime,
    finished: datetime,
    completion: DiagnosisCompletion,
) -> BackendUsageRecord:
    cost, quality = derive_cost_usd(
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        cached_tokens=None,
        model_name=completion.model or None,
        provider_cost_usd=None,
    )
    latency = max(0.0, (finished - started).total_seconds())
    return BackendUsageRecord(
        usage_id=(
            f"{task_id}:{subtask_id}:__diagnosis__:{attempt_id}:main:"
            f"openai_compatible:diag"
        ),
        task_id=task_id,
        subtask_id=subtask_id,
        node_id="__diagnosis__",
        backend_id="openai_compatible",
        attempt_id=attempt_id,
        started_at=started,
        finished_at=finished,
        latency_seconds=latency,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        estimated_cost_usd=cost,
        cost_quality=quality,
        accounting_source=ACCOUNTING_SOURCE,
        status="ok",
        model_name=completion.model or None,
        phase="control_plane",
        provenance="control_plane",
        session_ref_identity="diagnosis",
    )


def _write_artifact(
    directory: Path | None,
    subtask_id: str,
    attempt_id: int,
    call: DiagnosisCall,
    lookup: FailureDiagnosis,
    final: FailureDiagnosis,
) -> None:
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{subtask_id or 'subtask'}_{attempt_id}"
    payload = {
        "subtask_id": subtask_id,
        "attempt_id": attempt_id,
        "temperature": 0,
        "prompt": call.prompt,
        "response": call.response,
        "parsed": call.parsed,
        "applied": call.applied,
        "fallback_reason": call.fallback_reason,
        "lookup_class": infer_failure_class(lookup).value,
        "final_class": final.failure_class or infer_failure_class(final).value,
        "recommended_role": final.recommended_role,
        "recommended_reviewer": final.recommended_reviewer,
        "role_source": final.role_source,
        "persistence_samples": final.persistence_samples,
        "final_source": final.diagnosis_source,
    }
    (directory / f"{stem}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ACCOUNTING_SOURCE",
    "DEFAULT_MODEL",
    "DiagnosisCall",
    "DiagnosisClient",
    "DiagnosisCompletion",
    "DiagnosisConfig",
    "OpenAIDiagnosisClient",
    "build_diagnosis_prompt",
    "refine_diagnosis",
]
