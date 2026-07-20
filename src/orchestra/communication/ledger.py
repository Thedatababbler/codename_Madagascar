"""Delivery ledger records for idempotent communication delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    SKIPPED = "skipped"
    FAILED = "failed"
    SKIPPED_CONDITION_FALSE = "skipped_condition_false"
    SKIPPED_RULE_DISABLED = "skipped_rule_disabled"
    SKIPPED_NO_RULE = "skipped_no_rule"
    SKIPPED_TARGET_NOT_DELIVERABLE = "skipped_target_not_deliverable"


class DeliveryFailureReason(StrEnum):
    REQUIRED_SOURCE_NOT_COMMITTED = "required_source_not_committed"
    REQUIRED_ARTIFACT_MISSING = "required_artifact_missing"
    REQUIRED_FIELD_MISSING = "required_field_missing"
    REQUIRED_RULE_MISSING = "required_rule_missing"
    REQUIRED_CONDITION_UNSATISFIED = "required_condition_unsatisfied"
    CONTEXT_BUDGET_INFEASIBLE = "context_budget_infeasible"
    PROJECTION_INFEASIBLE = "projection_infeasible"
    LEDGER_CORRUPTION = "ledger_corruption"
    AGGREGATION_CONFLICT = "aggregation_conflict"
    AGGREGATION_RULE_MISSING = "aggregation_rule_missing"
    AGGREGATION_REQUIRED_INPUT_MISSING = "aggregation_required_input_missing"
    AMBIGUOUS_AGGREGATION_RULE = "ambiguous_aggregation_rule"
    AGGREGATION_INPUT_SET_MISMATCH = "aggregation_input_set_mismatch"
    UNSUPPORTED_TRIGGER = "unsupported_trigger"
    TARGET_NOT_DELIVERABLE = "target_not_deliverable"


class DeliveryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    communication_plan_version: int
    rule_id: str = ""
    payload_id: str
    source_subtask_id: str
    target_subtask_id: str
    source_artifact_id: str
    projected_artifact_id: str = ""
    projected_artifact_hash: str = ""
    target_slot: str = ""
    delivered_at_state_version: int
    status: DeliveryStatus = DeliveryStatus.DELIVERED
    estimated_tokens: int = 0
    projected_token_count: int | None = None
    aggregated_token_count: int | None = None
    payload_count: int = 1
    target_context_utilization: float | None = None
    truncated: bool = False
    failure_reason: DeliveryFailureReason | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _project_legacy_tokens(self) -> DeliveryRecord:
        if self.projected_token_count is None:
            self.projected_token_count = self.estimated_tokens
        return self

    def idempotency_key(self) -> tuple[Any, ...]:
        return (
            self.communication_plan_version,
            self.rule_id,
            self.payload_id,
            self.source_artifact_id,
            self.target_subtask_id,
            self.target_slot,
        )


def delivery_idempotency_key(
    *,
    communication_plan_version: int,
    rule_id: str,
    payload_id: str,
    source_artifact_id: str,
    target_subtask_id: str,
    target_slot: str,
) -> tuple[Any, ...]:
    return (
        communication_plan_version,
        rule_id,
        payload_id,
        source_artifact_id,
        target_subtask_id,
        target_slot,
    )


def find_delivered_record(
    ledger: list[DeliveryRecord],
    *,
    communication_plan_version: int,
    rule_id: str,
    payload_id: str,
    source_artifact_id: str,
    target_subtask_id: str,
    target_slot: str,
) -> DeliveryRecord | None:
    key = delivery_idempotency_key(
        communication_plan_version=communication_plan_version,
        rule_id=rule_id,
        payload_id=payload_id,
        source_artifact_id=source_artifact_id,
        target_subtask_id=target_subtask_id,
        target_slot=target_slot,
    )
    for rec in ledger:
        if rec.status is DeliveryStatus.DELIVERED and rec.idempotency_key() == key:
            return rec
    return None


def already_delivered(
    ledger: list[DeliveryRecord],
    *,
    payload_id: str,
    communication_plan_version: int,
    source_artifact_id: str,
    target_subtask_id: str,
    rule_id: str = "",
    target_slot: str = "",
) -> bool:
    if rule_id or target_slot:
        return (
            find_delivered_record(
                ledger,
                communication_plan_version=communication_plan_version,
                rule_id=rule_id,
                payload_id=payload_id,
                source_artifact_id=source_artifact_id,
                target_subtask_id=target_subtask_id,
                target_slot=target_slot,
            )
            is not None
        )
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
