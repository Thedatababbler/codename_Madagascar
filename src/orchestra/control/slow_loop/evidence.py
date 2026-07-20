"""Typed runtime evidence events for Slow Loop observation / watermarks."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.control.backend_usage import session_ref_identity
from orchestra.control.fast_loop.schemas import CandidateStatus
from orchestra.control.task_state import (
    BackendSessionRecord,
    SubtaskFailureReason,
    SubtaskStatus,
    TaskExecutionState,
    WorkspaceCommitStatus,
)


class RuntimeEvidenceKind(StrEnum):
    DELIVERY = "delivery"
    ACTIVE_COMMUNICATION_BLOCK = "active_communication_block"
    BACKEND_FAILURE = "backend_failure"
    HARNESS_FAILURE = "harness_failure"
    CANONICAL_CONFLICT = "canonical_conflict"


class RuntimeEvidenceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_key: str
    kind: RuntimeEvidenceKind

    task_id: str
    subtask_id: str | None = None
    target_subtask_id: str | None = None

    attempt_id: int | None = None
    node_id: str | None = None
    backend_id: str | None = None
    candidate_id: str | None = None

    communication_plan_version: int | None = None
    state_version: int | None = None

    failure_reason: str | None = None
    source_record_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _stable_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def active_block_evidence_key(
    *,
    state: TaskExecutionState,
    target_subtask_id: str,
    reason: str,
) -> str:
    """Fingerprint an unresolved active communication block."""
    plan = state.communication_plan
    contracts = [
        c.model_dump(mode="json")
        for c in plan.payload_contracts
        if c.target_subtask_id == target_subtask_id
    ]
    contract_ids = {
        c.payload_id for c in plan.payload_contracts if c.target_subtask_id == target_subtask_id
    }
    rules = [
        r.model_dump(mode="json")
        for r in plan.delivery_schedule
        if r.payload_id in contract_ids
    ]
    aggs = [
        a.model_dump(mode="json")
        for a in plan.aggregation_rules
        if (a.metadata or {}).get("target_subtask_id") == target_subtask_id
        or any(
            pid in contract_ids for pid in (a.source_payload_ids or [])
        )
    ]
    budget = plan.context_budgets.get(target_subtask_id)
    contract_fp = _stable_hash(
        {
            "contracts": sorted(contracts, key=lambda x: x.get("payload_id", "")),
            "rules": sorted(rules, key=lambda x: x.get("rule_id", "")),
            "aggregation": sorted(aggs, key=lambda x: x.get("rule_id", "")),
            "budget": budget,
            "target_status": (
                state.subtasks[target_subtask_id].status.value
                if target_subtask_id in state.subtasks
                else None
            ),
        }
    )
    source_artifacts: list[str] = []
    for contract in plan.payload_contracts:
        if contract.target_subtask_id != target_subtask_id:
            continue
        src = state.subtasks.get(contract.source_subtask_id)
        if src is None:
            continue
        if src.final_output_artifact_id:
            source_artifacts.append(src.final_output_artifact_id)
        for ref in src.committed_artifacts:
            source_artifacts.append(ref.artifact_id)
    source_fp = _stable_hash(sorted(set(source_artifacts)))
    return (
        f"block:{plan.version}:{target_subtask_id}:{reason}:{contract_fp}:{source_fp}"
    )


def backend_evidence_key(
    *,
    subtask_id: str,
    session: BackendSessionRecord,
    failure_reason: str,
) -> str:
    cand = session.candidate_id or "main"
    sess_id = session_ref_identity(session.session_ref)
    return (
        f"backend:{subtask_id}:{session.node_id}:{session.attempt_id}:"
        f"{cand}:{session.backend_id}:{failure_reason}:{sess_id}"
    )


def harness_evidence_key(
    *,
    subtask_id: str,
    attempt_id: int,
    harness_artifact_id: str,
    failure_reason: str,
) -> str:
    art = harness_artifact_id or "none"
    return f"harness:{subtask_id}:{attempt_id}:{art}:{failure_reason}"


def collect_active_block_events(state: TaskExecutionState) -> list[RuntimeEvidenceEvent]:
    events: list[RuntimeEvidenceEvent] = []
    for sid, sub in sorted(state.subtasks.items()):
        reason = sub.communication_block_reason
        if not reason:
            continue
        if sub.status not in {SubtaskStatus.PENDING, SubtaskStatus.READY}:
            continue
        if sub.lease_status == "leased":
            continue
        key = active_block_evidence_key(
            state=state, target_subtask_id=sid, reason=reason
        )
        events.append(
            RuntimeEvidenceEvent(
                evidence_key=key,
                kind=RuntimeEvidenceKind.ACTIVE_COMMUNICATION_BLOCK,
                task_id=state.task_id,
                subtask_id=sid,
                target_subtask_id=sid,
                communication_plan_version=state.communication_plan.version,
                state_version=state.state_version,
                failure_reason=reason,
                metadata={"lease_status": sub.lease_status, "status": sub.status.value},
            )
        )
    return events


def collect_backend_failure_events(state: TaskExecutionState) -> list[RuntimeEvidenceEvent]:
    events: list[RuntimeEvidenceEvent] = []
    seen: set[str] = set()

    def _add(sid: str, sess: BackendSessionRecord, failure_reason: str) -> None:
        key = backend_evidence_key(
            subtask_id=sid, session=sess, failure_reason=failure_reason
        )
        if key in seen:
            return
        seen.add(key)
        events.append(
            RuntimeEvidenceEvent(
                evidence_key=key,
                kind=RuntimeEvidenceKind.BACKEND_FAILURE,
                task_id=state.task_id,
                subtask_id=sid,
                attempt_id=sess.attempt_id,
                node_id=sess.node_id,
                backend_id=sess.backend_id,
                candidate_id=sess.candidate_id,
                failure_reason=failure_reason,
                source_record_id=session_ref_identity(sess.session_ref),
            )
        )

    for sid, sub in sorted(state.subtasks.items()):
        if sub.failure_reason in {
            SubtaskFailureReason.INFRA,
            SubtaskFailureReason.MODEL,
        }:
            reason = sub.failure_reason.value
            for sess in sub.backend_sessions:
                _add(sid, sess, reason)

        fl = state.fast_loop_states.get(sid)
        if fl is None:
            continue
        for cand in getattr(fl, "candidates", []) or []:
            if getattr(cand, "status", None) is not CandidateStatus.BACKEND_FAILED:
                continue
            fr = getattr(cand, "failure_reason", None)
            fr_s = (
                fr.value
                if hasattr(fr, "value")
                else (str(fr) if fr else SubtaskFailureReason.INFRA.value)
            )
            for sess in getattr(cand, "backend_sessions", []) or []:
                _add(sid, sess, fr_s)
    return events


def collect_harness_failure_events(state: TaskExecutionState) -> list[RuntimeEvidenceEvent]:
    events: list[RuntimeEvidenceEvent] = []
    seen: set[str] = set()

    def _add(
        *,
        subtask_id: str,
        attempt_id: int,
        harness_artifact_id: str,
        failure_reason: str,
        source_record_id: str | None = None,
        candidate_id: str | None = None,
    ) -> None:
        key = harness_evidence_key(
            subtask_id=subtask_id,
            attempt_id=attempt_id,
            harness_artifact_id=harness_artifact_id,
            failure_reason=failure_reason,
        )
        if key in seen:
            return
        seen.add(key)
        events.append(
            RuntimeEvidenceEvent(
                evidence_key=key,
                kind=RuntimeEvidenceKind.HARNESS_FAILURE,
                task_id=state.task_id,
                subtask_id=subtask_id,
                attempt_id=attempt_id,
                candidate_id=candidate_id,
                failure_reason=failure_reason,
                source_record_id=source_record_id,
                metadata={"harness_artifact_id": harness_artifact_id},
            )
        )

    for sid, sub in sorted(state.subtasks.items()):
        for att in sub.attempts:
            harnessish = att.status in {
                SubtaskStatus.HARNESS_FAILED,
                SubtaskStatus.RETRY_PENDING,
            }
            meta_art = str((att.metadata or {}).get("harness_artifact_id") or "")
            if harnessish or (
                sub.failure_reason is SubtaskFailureReason.HARNESS
                and att.attempt_id == (sub.attempts[-1].attempt_id if sub.attempts else None)
            ):
                if not harnessish and sub.failure_reason is not SubtaskFailureReason.HARNESS:
                    continue
                if (
                    sub.failure_reason is SubtaskFailureReason.HARNESS
                    or harnessish
                ):
                    _add(
                        subtask_id=sid,
                        attempt_id=att.attempt_id,
                        harness_artifact_id=meta_art,
                        failure_reason=SubtaskFailureReason.HARNESS.value,
                        source_record_id=f"attempt:{att.attempt_id}",
                    )

        fl = state.fast_loop_states.get(sid)
        if fl is not None:
            for cand in getattr(fl, "candidates", []) or []:
                if getattr(cand, "status", None) is not CandidateStatus.HARNESS_FAILED:
                    continue
                art = getattr(cand, "harness_artifact_id", None) or ""
                _add(
                    subtask_id=sid,
                    attempt_id=int(getattr(cand, "attempt_id", 0) or 0),
                    harness_artifact_id=str(art),
                    failure_reason=SubtaskFailureReason.HARNESS.value,
                    source_record_id=getattr(cand, "candidate_id", None),
                    candidate_id=getattr(cand, "candidate_id", None),
                )

    for rec in state.workspace_commit_records or []:
        if rec.status is not WorkspaceCommitStatus.VALIDATION_FAILED:
            continue
        _add(
            subtask_id=rec.subtask_id,
            attempt_id=rec.attempt_id,
            harness_artifact_id=rec.harness_artifact_id or "",
            failure_reason=WorkspaceCommitStatus.VALIDATION_FAILED.value,
            source_record_id=rec.record_id,
        )
    return events


def collect_delivery_and_canonical_events(
    state: TaskExecutionState,
    *,
    delivery_start: int = 0,
    commit_start: int = 0,
) -> list[RuntimeEvidenceEvent]:
    events: list[RuntimeEvidenceEvent] = []
    ledger = list(state.delivery_ledger or [])
    for rec in ledger[delivery_start:]:
        did = getattr(rec, "delivery_id", None)
        if not did:
            continue
        events.append(
            RuntimeEvidenceEvent(
                evidence_key=f"delivery:{did}",
                kind=RuntimeEvidenceKind.DELIVERY,
                task_id=state.task_id,
                subtask_id=getattr(rec, "source_subtask_id", None),
                target_subtask_id=getattr(rec, "target_subtask_id", None),
                communication_plan_version=getattr(
                    rec, "communication_plan_version", None
                ),
                failure_reason=_reason_value(getattr(rec, "failure_reason", None)),
                source_record_id=str(did),
            )
        )
    commits = list(state.workspace_commit_records or [])
    for rec in commits[commit_start:]:
        rid = getattr(rec, "record_id", None) or getattr(rec, "commit_id", None)
        status = getattr(rec, "status", None)
        status_s = status.value if hasattr(status, "value") else str(status)
        if not rid:
            continue
        kind = RuntimeEvidenceKind.CANONICAL_CONFLICT
        if status is WorkspaceCommitStatus.VALIDATION_FAILED:
            # Harness path owns validation-failed identity.
            continue
        if status is not WorkspaceCommitStatus.CONFLICTED:
            # Keep non-conflict commit rows out of conflict triggers.
            if status is WorkspaceCommitStatus.FAILED:
                pass
            else:
                continue
        events.append(
            RuntimeEvidenceEvent(
                evidence_key=f"canonical:{rid}:{status_s}",
                kind=kind,
                task_id=state.task_id,
                subtask_id=getattr(rec, "subtask_id", None),
                attempt_id=getattr(rec, "attempt_id", None),
                failure_reason=status_s,
                source_record_id=str(rid),
            )
        )
    return events


def _reason_value(reason: object | None) -> str | None:
    if reason is None:
        return None
    if hasattr(reason, "value"):
        return str(reason.value)
    return str(reason)


def collect_runtime_evidence_events(
    state: TaskExecutionState,
    *,
    delivery_start: int = 0,
    commit_start: int = 0,
) -> list[RuntimeEvidenceEvent]:
    """Collect all typed evidence events (stable keys, real attempt identity)."""
    events: list[RuntimeEvidenceEvent] = []
    events.extend(
        collect_delivery_and_canonical_events(
            state, delivery_start=delivery_start, commit_start=commit_start
        )
    )
    events.extend(collect_backend_failure_events(state))
    events.extend(collect_harness_failure_events(state))
    events.extend(collect_active_block_events(state))
    # Deduplicate by evidence_key while preserving order.
    out: list[RuntimeEvidenceEvent] = []
    seen: set[str] = set()
    for ev in events:
        if ev.evidence_key in seen:
            continue
        seen.add(ev.evidence_key)
        out.append(ev)
    return out
