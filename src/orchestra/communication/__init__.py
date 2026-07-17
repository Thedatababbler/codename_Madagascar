"""Communication plan and delivery engine (Milestone 5)."""

from orchestra.communication.aggregation import AggregationRule, AggregationSpec
from orchestra.communication.compiler import (
    CommunicationPlanCompiler,
    CompiledCommunicationPlan,
)
from orchestra.communication.ledger import DeliveryRecord, DeliveryStatus
from orchestra.communication.payload import DeliveryRule, PayloadContract
from orchestra.communication.plan import CommunicationPlan

__all__ = [
    "AggregationRule",
    "AggregationSpec",
    "CompiledCommunicationPlan",
    "CommunicationPlan",
    "CommunicationPlanCompiler",
    "DeliveryRecord",
    "DeliveryRule",
    "DeliveryStatus",
    "PayloadContract",
]


def __getattr__(name: str):
    if name == "CommunicationDeliveryEngine":
        from orchestra.communication.delivery import CommunicationDeliveryEngine

        return CommunicationDeliveryEngine
    raise AttributeError(name)
