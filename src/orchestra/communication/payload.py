"""Typed payload contracts for inter-subtask communication."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_id: str
    source_subtask_id: str
    target_subtask_id: str
    artifact_type: str
    required_fields: list[str] = Field(default_factory=list)
    max_tokens: int = 2048
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeliveryRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    payload_id: str
    condition: str = "on_commit"
    forward_only: bool = True
