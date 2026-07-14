"""CommunicationPlan schema (delivery engine lands in Milestone 5)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.aggregation import AggregationRule
from orchestra.communication.payload import DeliveryRule, PayloadContract


class CommunicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_contracts: list[PayloadContract] = Field(default_factory=list)
    context_budgets: dict[str, int] = Field(default_factory=dict)
    delivery_schedule: list[DeliveryRule] = Field(default_factory=list)
    aggregation_rules: list[AggregationRule] = Field(default_factory=list)
    version: int = 1
