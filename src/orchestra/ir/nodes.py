from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class NodeKind(StrEnum):
    AGENT = "agent"
    HARNESS = "harness"
    TRANSFORM = "transform"
    SELECTOR = "selector"


class StructuredLLMBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["structured_llm"] = "structured_llm"


# Milestone 1 only registers structured_llm. Additional backend configs are
# added in later milestones without changing the default LiveCodeBench path.
AgentBackendConfig = Annotated[
    StructuredLLMBackendConfig,
    Field(discriminator="type"),
]


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
    backend: AgentBackendConfig | None = None

    def resolved_backend(self) -> StructuredLLMBackendConfig:
        """Old graphs without backend default to structured_llm."""
        if self.backend is None:
            return StructuredLLMBackendConfig()
        return self.backend


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


def normalize_agent_backend_config(raw_node: dict[str, Any]) -> dict[str, Any]:
    """Deterministically convert legacy agent nodes to structured_llm backend."""
    if raw_node.get("node_kind") != "agent":
        return raw_node
    node = dict(raw_node)
    backend = node.get("backend")
    if backend is None:
        node["backend"] = {"type": "structured_llm"}
    elif isinstance(backend, str):
        node["backend"] = {"type": backend}
    elif isinstance(backend, dict) and "type" not in backend:
        node["backend"] = {"type": "structured_llm", **backend}
    return node
