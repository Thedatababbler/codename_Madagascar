"""Communication plan and delivery engine (Milestone 5)."""

from orchestra.communication.aggregation import (
    AggregationRule,
    AggregationSpec,
    AggregationStrategy,
)
from orchestra.communication.compiler import (
    CommunicationPlanCompiler,
    CompiledCommunicationPlan,
)
from orchestra.communication.ledger import (
    DeliveryFailureReason,
    DeliveryRecord,
    DeliveryStatus,
)
from orchestra.communication.payload import (
    DeliveryCondition,
    DeliveryRule,
    DeliveryTrigger,
    PayloadContract,
)
from orchestra.communication.plan import CommunicationPlan

__all__ = [
    "AggregationRule",
    "AggregationSpec",
    "AggregationStrategy",
    "CompiledCommunicationPlan",
    "CommunicationPlan",
    "CommunicationPlanCompiler",
    "DeliveryCondition",
    "DeliveryFailureReason",
    "DeliveryRecord",
    "DeliveryRule",
    "DeliveryStatus",
    "DeliveryTrigger",
    "PayloadContract",
]


def __getattr__(name: str):
    if name == "CommunicationDeliveryEngine":
        from orchestra.communication.delivery import CommunicationDeliveryEngine

        return CommunicationDeliveryEngine
    raise AttributeError(name)
