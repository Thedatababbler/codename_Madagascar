"""Strict recursive payload projection with token budget enforcement."""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.payload import PayloadContract
from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.schemas.artifacts import FinalAnswerArtifact

PROJECTION_POLICY_VERSION = "m5.strict.v1"


class TokenEstimator(Protocol):
    def estimate(self, value: Any) -> int: ...


class DeterministicTokenEstimator:
    """Consistent char/4 approximation; never calls an external model."""

    version: str = "char4.v1"

    def estimate(self, value: Any) -> int:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        return max(1, (len(raw) + 3) // 4)


DEFAULT_TOKEN_ESTIMATOR = DeterministicTokenEstimator()


class PayloadProjectionInfeasible(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason: DeliveryFailureReason = DeliveryFailureReason.PROJECTION_INFEASIBLE,
    ) -> None:
        super().__init__(message)
        self.reason = reason


class PayloadProjectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_id: str
    source_artifact_id: str
    projected_artifact: ArtifactEnvelope
    estimated_tokens: int
    truncated: bool
    omitted_fields: list[str] = Field(default_factory=list)
    omitted_paths: list[str] = Field(default_factory=list)
    projection_policy_version: str = PROJECTION_POLICY_VERSION
    original_estimated_tokens: int = 0
    final_estimated_tokens: int = 0


def estimate_tokens(obj: Any) -> int:
    return DEFAULT_TOKEN_ESTIMATOR.estimate(obj)


def _trim_string(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 16:
        return text[:max_chars]
    return text[: max(1, max_chars - 12)] + "…[truncated]"


def _project_value(
    value: Any,
    *,
    budget: int,
    path: str,
    estimator: TokenEstimator,
    omitted_paths: list[str],
) -> Any:
    if budget <= 0:
        omitted_paths.append(path or "$")
        return None

    if value is None or isinstance(value, (bool, int, float)):
        cost = estimator.estimate(value)
        if cost > budget:
            omitted_paths.append(path or "$")
            return None
        return value

    if isinstance(value, str):
        if estimator.estimate(value) <= budget:
            return value
        lo, hi = 0, len(value)
        best = ""
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = _trim_string(value, mid)
            if estimator.estimate(candidate) <= budget:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        if best == "":
            omitted_paths.append(path or "$")
            return ""
        if best != value:
            omitted_paths.append(path or "$")
        return best

    if isinstance(value, list):
        kept: list[Any] = []
        used = estimator.estimate([])
        for idx, item in enumerate(value):
            remaining = budget - used
            if remaining <= 1:
                omitted_paths.append(f"{path}[{idx}:]")
                break
            projected_item = _project_value(
                item,
                budget=remaining,
                path=f"{path}[{idx}]",
                estimator=estimator,
                omitted_paths=omitted_paths,
            )
            trial = [*kept, projected_item]
            cost = estimator.estimate(trial)
            if cost > budget:
                omitted_paths.append(f"{path}[{idx}:]")
                break
            kept.append(projected_item)
            used = cost
        omitted = len(value) - len(kept)
        if omitted > 0:
            omitted_paths.append(f"{path}#omitted_items={omitted}")
        return kept

    if isinstance(value, dict):
        keys = sorted(value.keys())
        out: dict[str, Any] = {}
        used = estimator.estimate({})
        for key in keys:
            remaining = budget - used
            if remaining <= 1:
                omitted_paths.append(f"{path}.{key}" if path else key)
                continue
            child_path = f"{path}.{key}" if path else str(key)
            projected_child = _project_value(
                value[key],
                budget=remaining,
                path=child_path,
                estimator=estimator,
                omitted_paths=omitted_paths,
            )
            trial = {**out, key: projected_child}
            cost = estimator.estimate(trial)
            if cost > budget:
                omitted_paths.append(child_path)
                continue
            out[key] = projected_child
            used = cost
        return out

    return _project_value(
        str(value),
        budget=budget,
        path=path,
        estimator=estimator,
        omitted_paths=omitted_paths,
    )


def _build_answer_payload(
    *,
    contract: PayloadContract,
    source: ArtifactEnvelope,
    fields: dict[str, Any],
    truncated: bool,
    omitted_fields: list[str],
    omitted_paths: list[str],
) -> dict[str, Any]:
    return {
        "payload_id": contract.payload_id,
        "source_artifact_id": source.artifact_id,
        "source_artifact_type": source.artifact_type,
        "fields": fields,
        "truncated": truncated,
        "omitted_fields": sorted(set(omitted_fields)),
        "omitted_paths": sorted(set(omitted_paths)),
        "projection_policy_version": PROJECTION_POLICY_VERSION,
    }


def project_payload(
    *,
    contract: PayloadContract,
    source: ArtifactEnvelope,
    task_id: str,
    estimator: TokenEstimator | None = None,
) -> PayloadProjectionResult:
    est = estimator or DEFAULT_TOKEN_ESTIMATOR
    payload = dict(source.payload)
    omitted_fields: list[str] = []

    if contract.required_fields:
        projected: dict[str, Any] = {}
        for key in sorted(set(contract.required_fields)):
            if key not in payload:
                raise PayloadProjectionInfeasible(
                    f"REQUIRED_FIELD_MISSING: {key} for payload {contract.payload_id}",
                    reason=DeliveryFailureReason.REQUIRED_FIELD_MISSING,
                )
            projected[key] = payload[key]
        for key in sorted(payload):
            if key not in projected:
                omitted_fields.append(key)
    else:
        projected = dict(payload)

    original = est.estimate(projected)
    priority_keys = list(contract.required_fields) + sorted(
        k for k in projected if k not in set(contract.required_fields)
    )
    ordered = {k: projected[k] for k in priority_keys if k in projected}
    required_set = set(contract.required_fields)

    # Iteratively shrink until the full answer payload fits max_tokens.
    field_budget = max(1, contract.max_tokens)
    omitted_paths: list[str] = []
    fields = ordered
    truncated = False
    answer_payload = _build_answer_payload(
        contract=contract,
        source=source,
        fields=fields,
        truncated=False,
        omitted_fields=omitted_fields,
        omitted_paths=omitted_paths,
    )
    for _ in range(24):
        final_tokens = est.estimate(answer_payload)
        if final_tokens <= contract.max_tokens:
            break
        truncated = True
        # Reduce field budget relative to overflow.
        overflow = final_tokens - contract.max_tokens
        field_budget = max(1, est.estimate(fields) - overflow - 4)
        omitted_paths = []
        shrunk: dict[str, Any] = {}
        used = est.estimate({})
        for key in priority_keys:
            if key not in ordered:
                continue
            remaining = field_budget - used
            is_required = key in required_set
            child_budget = max(1, remaining if remaining > 0 else field_budget)
            child = _project_value(
                ordered[key],
                budget=child_budget,
                path=key,
                estimator=est,
                omitted_paths=omitted_paths,
            )
            trial = {**shrunk, key: child}
            cost = est.estimate(trial)
            if cost > field_budget:
                if is_required:
                    alone = _project_value(
                        ordered[key],
                        budget=max(1, field_budget),
                        path=key,
                        estimator=est,
                        omitted_paths=omitted_paths,
                    )
                    shrunk = {key: alone}
                    used = est.estimate(shrunk)
                    continue
                omitted_fields.append(key)
                omitted_paths.append(key)
                continue
            shrunk[key] = child
            used = cost
        fields = shrunk
        answer_payload = _build_answer_payload(
            contract=contract,
            source=source,
            fields=fields,
            truncated=True,
            omitted_fields=omitted_fields,
            omitted_paths=omitted_paths,
        )
    else:
        final_tokens = est.estimate(answer_payload)

    final_tokens = est.estimate(answer_payload)
    if final_tokens > contract.max_tokens:
        # Last resort: drop optional metadata from envelope for tiny budgets.
        minimal = {
            "payload_id": contract.payload_id,
            "fields": fields,
            "truncated": True,
        }
        # Keep shrinking string fields until minimal envelope fits.
        for _ in range(16):
            if est.estimate(minimal) <= contract.max_tokens:
                answer_payload = {
                    **minimal,
                    "source_artifact_id": source.artifact_id,
                    "source_artifact_type": source.artifact_type,
                    "omitted_fields": sorted(set(omitted_fields)),
                    "omitted_paths": sorted(set(omitted_paths)),
                    "projection_policy_version": PROJECTION_POLICY_VERSION,
                }
                # If still over due to restored metadata, keep minimal only.
                if est.estimate(answer_payload) > contract.max_tokens:
                    answer_payload = minimal
                truncated = True
                break
            # Halve all string leaves.
            def _halve(obj: Any) -> Any:
                if isinstance(obj, str) and len(obj) > 4:
                    return obj[: len(obj) // 2] + "…"
                if isinstance(obj, dict):
                    return {k: _halve(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_halve(v) for v in obj[: max(1, len(obj) // 2)]]
                return obj

            fields = _halve(fields)
            minimal["fields"] = fields
        final_tokens = est.estimate(answer_payload)
        if final_tokens > contract.max_tokens:
            reason = (
                DeliveryFailureReason.CONTEXT_BUDGET_INFEASIBLE
                if contract.is_required() or required_set
                else DeliveryFailureReason.PROJECTION_INFEASIBLE
            )
            raise PayloadProjectionInfeasible(
                f"PayloadProjectionInfeasible: final_estimated_tokens "
                f"{final_tokens} > max_tokens {contract.max_tokens} "
                f"for payload {contract.payload_id}",
                reason=reason,
            )

    assert final_tokens <= contract.max_tokens

    answer = FinalAnswerArtifact(
        answer=json.dumps(answer_payload, sort_keys=True, ensure_ascii=False),
        source_node=f"comm:{contract.payload_id}",
        raw_output=None,
        extraction_status="ok",
    )
    projected_art = create_artifact(
        answer,
        producer_node_id=f"comm:{contract.payload_id}",
        task_id=task_id,
        parent_artifact_ids=[source.artifact_id],
    )
    # Token estimate is for the projected payload content (answer_payload),
    # which is what the contract max_tokens governs.
    return PayloadProjectionResult(
        payload_id=contract.payload_id,
        source_artifact_id=source.artifact_id,
        projected_artifact=projected_art,
        estimated_tokens=final_tokens,
        truncated=truncated or bool(omitted_paths) or final_tokens < original,
        omitted_fields=sorted(set(omitted_fields)),
        omitted_paths=sorted(set(omitted_paths)),
        projection_policy_version=PROJECTION_POLICY_VERSION,
        original_estimated_tokens=original,
        final_estimated_tokens=final_tokens,
    )
