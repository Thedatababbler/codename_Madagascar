"""Project committed artifacts into delivery payloads (field selection + truncation)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.payload import PayloadContract
from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.schemas.artifacts import FinalAnswerArtifact


class PayloadProjectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_id: str
    source_artifact_id: str
    projected_artifact: ArtifactEnvelope
    estimated_tokens: int
    truncated: bool
    omitted_fields: list[str] = Field(default_factory=list)


def estimate_tokens(obj: Any) -> int:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return max(1, (len(raw) + 3) // 4)


def project_payload(
    *,
    contract: PayloadContract,
    source: ArtifactEnvelope,
    task_id: str,
) -> PayloadProjectionResult:
    payload = dict(source.payload)
    omitted: list[str] = []
    if contract.required_fields:
        projected: dict[str, Any] = {}
        for key in sorted(set(contract.required_fields)):
            if key in payload:
                projected[key] = payload[key]
            else:
                omitted.append(key)
        for key in sorted(payload):
            if key not in projected:
                omitted.append(key)
    else:
        projected = dict(payload)

    truncated = False
    tokens = estimate_tokens(projected)
    if tokens > contract.max_tokens:
        truncated = True
        str_keys = sorted(
            (k for k, v in projected.items() if isinstance(v, str)),
            key=lambda k: (-len(str(projected[k])), k),
        )
        for key in str_keys:
            while estimate_tokens(projected) > contract.max_tokens and isinstance(
                projected.get(key), str
            ):
                text = projected[key]
                if len(text) <= 16:
                    break
                projected[key] = text[: max(16, len(text) // 2)] + "…[truncated]"
            if estimate_tokens(projected) <= contract.max_tokens:
                break
        tokens = estimate_tokens(projected)
        if tokens > contract.max_tokens:
            keep = {
                k: (str(v)[:64] + "…[truncated]" if isinstance(v, str) else v)
                for k, v in projected.items()
                if k in set(contract.required_fields or projected.keys())
            }
            projected = keep
            tokens = estimate_tokens(projected)

    answer = FinalAnswerArtifact(
        answer=json.dumps(
            {
                "payload_id": contract.payload_id,
                "source_artifact_id": source.artifact_id,
                "source_artifact_type": source.artifact_type,
                "fields": projected,
                "truncated": truncated,
                "omitted_fields": omitted,
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
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
    return PayloadProjectionResult(
        payload_id=contract.payload_id,
        source_artifact_id=source.artifact_id,
        projected_artifact=projected_art,
        estimated_tokens=tokens,
        truncated=truncated,
        omitted_fields=sorted(set(omitted)),
    )
