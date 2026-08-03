"""Shared public-evaluation lifecycle for production, fixture, and formal runners.

Lifecycle:
  committed public harness artifact
  → PublicEvaluationRecord
  → persisted evidence store / task state
  → candidate estimator
  → Pareto comparison

Never reads hidden/private labels.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestra.control.pareto.schemas import EvaluationVisibility, PublicEvaluationRecord
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState

PUBLIC_HARNESS_TYPES = frozenset(
    {
        "PublicHarnessResultArtifact",
        "RepositoryHarnessResultArtifact",
    }
)

EVALUATOR_VERSION = "public-harness-v1"


def _stable_id(*parts: str) -> str:
    blob = "|".join(parts)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def source_artifact_hash(artifact: Any) -> str:
    """Stable hash of a public harness artifact payload."""
    if artifact is None:
        return ""
    if hasattr(artifact, "artifact_id") and getattr(artifact, "artifact_id", None):
        return str(artifact.artifact_id)
    if isinstance(artifact, dict):
        payload = artifact.get("payload") or artifact
        aid = artifact.get("artifact_id")
        if aid:
            return str(aid)
    else:
        payload = getattr(artifact, "payload", artifact)
    try:
        if hasattr(payload, "model_dump"):
            blob = json.dumps(payload.model_dump(mode="json"), sort_keys=True, default=str)
        elif isinstance(payload, dict):
            blob = json.dumps(payload, sort_keys=True, default=str)
        else:
            blob = str(payload)
    except Exception:  # noqa: BLE001
        blob = str(payload)
    return hashlib.sha256(blob.encode()).hexdigest()


def _score_from_payload(payload: Any) -> tuple[bool, float, int | None, int | None]:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")
    elif isinstance(payload, dict):
        data = payload
    else:
        data = {
            "passed": bool(getattr(payload, "passed", False)),
            "pass_ratio": getattr(payload, "pass_ratio", None),
            "harness_available": getattr(payload, "harness_available", True),
        }
    passed = bool(data.get("passed", False))
    ratio = data.get("pass_ratio")
    if ratio is None:
        ratio = 1.0 if passed else 0.0
    passed_checks = data.get("passed_count")
    total_checks = data.get("total_count")
    if passed_checks is None and "passed" in data:
        passed_checks = 1 if passed else 0
        total_checks = 1
    return passed, float(ratio), passed_checks, total_checks


def build_public_evaluation_record(
    *,
    run_id: str,
    task_id: str,
    subtask_id: str | None,
    harness_id: str,
    artifact: Any | None = None,
    passed: bool | None = None,
    normalized_score: float | None = None,
    plan_revision: str | None = None,
    decision_id: str | None = None,
    visibility: EvaluationVisibility = EvaluationVisibility.PUBLIC,
    provenance: str = "public_harness_commit",
    candidate_content_hash: str | None = None,
    edit_signature: str | None = None,
    state_version: int = 0,
    metric_name: str = "normalized_score",
    metadata: dict[str, Any] | None = None,
) -> PublicEvaluationRecord | None:
    """Build a PublicEvaluationRecord from public harness outputs only."""
    if visibility in {EvaluationVisibility.HIDDEN, EvaluationVisibility.PRIVATE}:
        return None

    artifact_type = None
    payload = None
    if artifact is not None:
        artifact_type = getattr(artifact, "artifact_type", None) or (
            artifact.get("artifact_type") if isinstance(artifact, dict) else None
        )
        payload = getattr(artifact, "payload", None)
        if payload is None and isinstance(artifact, dict):
            payload = artifact.get("payload")
        if artifact_type and artifact_type not in PUBLIC_HARNESS_TYPES:
            # Non-harness artifacts are not public quality evidence.
            if passed is None and normalized_score is None:
                return None

    if payload is not None:
        art_passed, score, passed_checks, total_checks = _score_from_payload(payload)
        if getattr(payload, "harness_available", True) is False or (
            isinstance(payload, dict) and payload.get("harness_available") is False
        ):
            # Genuinely unavailable public evaluator — do not fabricate.
            return PublicEvaluationRecord(
                evaluation_id=_stable_id(
                    run_id, task_id, subtask_id or "", harness_id, "unavailable"
                ),
                run_id=run_id,
                task_id=task_id,
                subtask_id=subtask_id,
                harness_id=harness_id,
                evaluator_id=harness_id,
                evaluator_version=EVALUATOR_VERSION,
                visibility=visibility,
                passed=False,
                normalized_score=0.0,
                metric_name=metric_name,
                metric_value=None,
                availability="unavailable",
                provenance=provenance,
                source_artifact_hash=source_artifact_hash(artifact),
                plan_revision=plan_revision,
                decision_id=decision_id,
                state_version=state_version,
                created_at=datetime.now(UTC),
                metadata={
                    **(metadata or {}),
                    "reason": "public_evaluator_unavailable",
                    "artifact_type": artifact_type,
                },
            )
    else:
        if passed is None and normalized_score is None:
            return None
        art_passed = bool(passed)
        score = float(
            normalized_score
            if normalized_score is not None
            else (1.0 if art_passed else 0.0)
        )
        passed_checks = 1 if art_passed else 0
        total_checks = 1

    if passed is not None:
        art_passed = bool(passed)
    if normalized_score is not None:
        score = float(normalized_score)

    src_hash = source_artifact_hash(artifact) if artifact is not None else _stable_id(
        task_id, subtask_id or "", harness_id, str(score)
    )
    evaluation_id = _stable_id(
        run_id, task_id, subtask_id or "", harness_id, src_hash, metric_name
    )
    return PublicEvaluationRecord(
        evaluation_id=evaluation_id,
        run_id=run_id,
        task_id=task_id,
        subtask_id=subtask_id,
        harness_id=harness_id,
        evaluator_id=harness_id,
        evaluator_version=EVALUATOR_VERSION,
        visibility=visibility,
        passed=art_passed,
        passed_checks=passed_checks,
        total_checks=total_checks,
        normalized_score=score,
        quality=score,
        metric_name=metric_name,
        metric_value=score,
        availability="available",
        provenance=provenance,
        source_artifact_hash=src_hash,
        plan_revision=plan_revision,
        decision_id=decision_id,
        candidate_content_hash=candidate_content_hash,
        edit_signature=edit_signature,
        state_version=state_version,
        revision_id=plan_revision,
        created_at=datetime.now(UTC),
        metadata={**(metadata or {}), "artifact_type": artifact_type},
    )


def records_from_execution_result(
    *,
    run_id: str,
    task_id: str,
    subtask_id: str,
    produced_artifacts: list[Any],
    candidate_harness_passed: bool,
    plan_revision: str | None,
    state_version: int,
    harness_id: str = "public_harness",
) -> list[PublicEvaluationRecord]:
    """Extract public evaluation records from a committed subtask result."""
    records: list[PublicEvaluationRecord] = []
    harness_arts = [
        a
        for a in produced_artifacts
        if getattr(a, "artifact_type", None) in PUBLIC_HARNESS_TYPES
        or (
            isinstance(a, dict)
            and a.get("artifact_type") in PUBLIC_HARNESS_TYPES
        )
    ]
    if harness_arts:
        for art in harness_arts:
            hid = harness_id
            at = getattr(art, "artifact_type", None) or (
                art.get("artifact_type") if isinstance(art, dict) else ""
            )
            if at == "RepositoryHarnessResultArtifact":
                hid = "repository_test_harness"
            elif at == "PublicHarnessResultArtifact":
                hid = "public_code_harness"
            rec = build_public_evaluation_record(
                run_id=run_id,
                task_id=task_id,
                subtask_id=subtask_id,
                harness_id=hid,
                artifact=art,
                plan_revision=plan_revision,
                state_version=state_version,
                provenance="public_harness_commit",
            )
            if rec is not None:
                records.append(rec)
        return records

    # Artifact-only / stub commits: record from explicit harness pass flag.
    # Still public-path evidence (not private labels).
    rec = build_public_evaluation_record(
        run_id=run_id,
        task_id=task_id,
        subtask_id=subtask_id,
        harness_id=harness_id,
        passed=candidate_harness_passed,
        normalized_score=1.0 if candidate_harness_passed else 0.0,
        plan_revision=plan_revision,
        state_version=state_version,
        provenance="public_harness_commit_flag",
        metadata={"source": "candidate_harness_passed"},
    )
    if rec is not None:
        records.append(rec)
    return records


def append_public_evaluations(
    state: TaskExecutionState,
    records: list[PublicEvaluationRecord],
) -> list[PublicEvaluationRecord]:
    """Idempotently append public evaluation records (resume-safe)."""
    existing_ids = {
        getattr(r, "evaluation_id", None)
        or (r.get("evaluation_id") if isinstance(r, dict) else None)
        for r in (state.public_evaluation_records or [])
    }
    added: list[PublicEvaluationRecord] = []
    for rec in records:
        if not rec.evaluation_id or rec.evaluation_id in existing_ids:
            continue
        if rec.availability == "unavailable":
            # Persist unavailable markers for audit, but estimators ignore them.
            pass
        state.public_evaluation_records.append(rec)
        existing_ids.add(rec.evaluation_id)
        added.append(rec)
    return added


def persist_public_evaluations(
    run_dir: str | Path,
    records: list[PublicEvaluationRecord],
) -> Path:
    """Append-only JSONL evidence store (dedupe by evaluation_id)."""
    path = Path(run_dir) / "public_evaluations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                seen.add(str(json.loads(line).get("evaluation_id") or ""))
            except json.JSONDecodeError:
                continue
    with path.open("a", encoding="utf-8") as fh:
        for rec in sorted(records, key=lambda r: r.evaluation_id):
            if rec.evaluation_id in seen:
                continue
            fh.write(rec.model_dump_json() + "\n")
            seen.add(rec.evaluation_id)
    return path


def record_commit_public_evaluations(
    *,
    state: TaskExecutionState,
    run_id: str,
    subtask_id: str,
    produced_artifacts: list[Any],
    candidate_harness_passed: bool,
    run_dir: str | Path | None = None,
    harness_id: str | None = None,
) -> list[PublicEvaluationRecord]:
    """Shared commit hook used by production scheduler (and callable by fixtures)."""
    sub = state.subtasks.get(subtask_id)
    if sub is None or sub.status is not SubtaskStatus.COMMITTED:
        return []
    hid = harness_id or getattr(
        getattr(sub, "spec", None), "keystone_harness_id", None
    ) or "public_harness"
    records = records_from_execution_result(
        run_id=run_id,
        task_id=state.task_id,
        subtask_id=subtask_id,
        produced_artifacts=list(produced_artifacts or []),
        candidate_harness_passed=candidate_harness_passed,
        plan_revision=state.active_plan_revision_id,
        state_version=state.state_version,
        harness_id=str(hid),
    )
    # Drop unavailable fabrications from the estimator pool path: keep only
    # available public/development records for estimation, but still persist
    # unavailable markers when present.
    added = append_public_evaluations(state, records)
    if run_dir is not None and added:
        persist_public_evaluations(run_dir, added)
    return added


def is_quality_neutral_scheduling_change(candidate: Any) -> bool:
    """True when the mutation cannot alter output-producing semantics."""
    edits = list(getattr(candidate, "edits", None) or [])
    edit_types = {getattr(e, "type", "") for e in edits}
    if "scheduling_concurrency" not in edit_types:
        return False
    forbidden = {
        "pending_backend_assignment",
        "pending_graph_template",
        "upsert_payload_contract",
        "remove_payload_contract",
        "serialization_group",
        "context_budget",
        "upsert_delivery_rule",
    }
    return not (edit_types & forbidden)
