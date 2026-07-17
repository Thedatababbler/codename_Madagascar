"""Delivery ledger records for idempotent communication delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    SKIPPED = "skipped"
    FAILED = "failed"


class DeliveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    communication_plan_version: int
    payload_id: str
    source_subtask_id: str
    target_subtask_id: str
    source_artifact_id: str
    projected_artifact_id: str
    delivered_at_state_version: int
    status: DeliveryStatus = DeliveryStatus.DELIVERED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def already_delivered(
    ledger: list[DeliveryRecord],
    *,
    payload_id: str,
    communication_plan_version: int,
    source_artifact_id: str,
    target_subtask_id: str,
) -> bool:
    for rec in ledger:
        if (
            rec.status is DeliveryStatus.DELIVERED
            and rec.payload_id == payload_id
            and rec.communication_plan_version == communication_plan_version
            and rec.source_artifact_id == source_artifact_id
            and rec.target_subtask_id == target_subtask_id
        ):
            return True
    return False
