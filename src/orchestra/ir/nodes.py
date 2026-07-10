from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class NodeKind(StrEnum):
    AGENT = "agent"
    HARNESS = "harness"
    TRANSFORM = "transform"
    SELECTOR = "selector"


class BaseNodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    node_kind: NodeKind
    input_slots: dict[str, str] = Field(default_factory=dict)
    output_slots: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = None


class AgentNodeSpec(BaseNodeSpec):
    node_kind: Literal[NodeKind.AGENT] = NodeKind.AGENT
    contract_id: str


class HarnessNodeSpec(BaseNodeSpec):
    node_kind: Literal[NodeKind.HARNESS] = NodeKind.HARNESS
    harness_id: str
    visibility: Literal["public"] = "public"


class TransformNodeSpec(BaseNodeSpec):
    node_kind: Literal[NodeKind.TRANSFORM] = NodeKind.TRANSFORM
    transform_id: str


class SelectorNodeSpec(BaseNodeSpec):
    node_kind: Literal[NodeKind.SELECTOR] = NodeKind.SELECTOR
    selector_id: str


NodeSpec = Annotated[
    AgentNodeSpec | HarnessNodeSpec | TransformNodeSpec | SelectorNodeSpec,
    Field(discriminator="node_kind"),
]
