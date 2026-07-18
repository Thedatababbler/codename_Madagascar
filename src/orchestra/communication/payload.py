"""Typed payload contracts for inter-subtask communication."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DeliveryTrigger(StrEnum):
    ON_SOURCE_COMMIT = "on_source_commit"
    BEFORE_TARGET_START = "before_target_start"
    MANUAL = "manual"


class DeliveryCondition(BaseModel):
    """Deterministic delivery predicate (M5: always / never only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = "always"  # always | never


class PayloadContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_id: str
    source_subtask_id: str
    target_subtask_id: str
    artifact_type: str
    required: bool = False
    required_fields: list[str] = Field(default_factory=list)
    max_tokens: int = 2048
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_required_from_metadata(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        meta = dict(data.get("metadata") or {})
        if "required" not in data and "required" in meta:
            return {**data, "required": bool(meta["required"])}
        return data

    def is_required(self) -> bool:
        if self.required:
            return True
        return bool(self.metadata.get("required"))


class DeliveryRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    payload_id: str
    trigger: DeliveryTrigger = DeliveryTrigger.ON_SOURCE_COMMIT
    enabled: bool = True
    condition: DeliveryCondition | None = None
    priority: int = 0
    forward_only: bool = True

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_condition(cls, data: Any) -> Any:
        """Map legacy condition='on_commit' string onto trigger / condition."""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        cond = out.get("condition")
        if not isinstance(cond, str):
            return out
        trigger_aliases = {
            "on_commit": DeliveryTrigger.ON_SOURCE_COMMIT,
            "on_source_commit": DeliveryTrigger.ON_SOURCE_COMMIT,
            "before_target_start": DeliveryTrigger.BEFORE_TARGET_START,
            "manual": DeliveryTrigger.MANUAL,
        }
        if cond in trigger_aliases:
            if "trigger" not in out:
                out["trigger"] = trigger_aliases[cond]
            out.pop("condition", None)
            return out
        if cond in {"never", "false"}:
            out["condition"] = {"kind": "never"}
            return out
        if cond in {"always", "true"}:
            out.pop("condition", None)
            return out
        out.pop("condition", None)
        return out

    @field_validator("condition", mode="before")
    @classmethod
    def _coerce_condition(cls, value: Any) -> Any:
        if value is None or isinstance(value, DeliveryCondition):
            return value
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            if value in {"never", "false"}:
                return DeliveryCondition(kind="never")
            return DeliveryCondition(kind="always")
        return value
