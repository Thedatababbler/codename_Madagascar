"""Aggregation rules for combining subtask outputs into a final result."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AggregationRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    source_subtask_ids: list[str] = Field(default_factory=list)
    strategy: Literal["identity", "select_first", "merge_artifacts"] = "identity"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AggregationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: Literal["identity", "select_first", "merge_artifacts"] = "identity"
    terminal_subtask_id: str | None = None
    rules: list[AggregationRule] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
