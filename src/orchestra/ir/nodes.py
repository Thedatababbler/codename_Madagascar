from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestra.backends.base import ModelSpec, OutputContract


class NodeKind(StrEnum):
    AGENT = "agent"
    HARNESS = "harness"
    TRANSFORM = "transform"
    SELECTOR = "selector"


class StructuredLLMBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["structured_llm"] = "structured_llm"
    max_steps: int = 1


class SmolagentsCodeBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["smolagents_code"] = "smolagents_code"
    max_steps: int = 8
    executor_type: Literal["local", "e2b", "modal", "blaxel", "docker"] = "local"
    planning_interval: int | None = None
    additional_authorized_imports: list[str] = Field(default_factory=list)
    use_structured_outputs_internally: bool = True
    managed_agents: None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_managed_agents(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("managed_agents") not in (None,):
            raise ValueError(
                "managed_agents is forbidden; AdaMAS owns top-level MAS scheduling"
            )
        return data


class CodexSDKBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["codex_sdk"] = "codex_sdk"
    thread_policy: Literal["fresh"] = "fresh"
    sandbox: Literal["read_only", "workspace_write"] = "workspace_write"
    approval_policy: Literal["never"] = "never"
    require_git_diff: bool = True
    max_steps: int = 1

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_modes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        forbidden = {
            "resume",
            "fork",
            "steer",
            "interrupt",
            "review",
            "danger-full-access",
            "full_access",
            "managed_agents",
        }
        bad = [key for key in data if key in forbidden and data.get(key) not in (None, False)]
        policy = data.get("thread_policy")
        if policy not in (None, "fresh"):
            bad.append(f"thread_policy={policy}")
        sandbox = data.get("sandbox")
        if sandbox in {"danger-full-access", "full_access"}:
            bad.append(f"sandbox={sandbox}")
        if bad:
            raise ValueError(
                "CodexSDKBackendConfig forbids resume/fork/steer/interrupt/review/"
                f"native subagents/danger-full-access; got {bad}"
            )
        return data


AgentBackendConfig = Annotated[
    StructuredLLMBackendConfig | SmolagentsCodeBackendConfig | CodexSDKBackendConfig,
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
    tools: list[str] = Field(default_factory=list)
    model: ModelSpec | None = None
    output_contract: OutputContract | None = None
    # Fast-loop overlays (optional; omitted from content hash when unset).
    prompt_feedback: str | None = None
    session_policy: Literal["fresh", "resume", "fork"] | None = None

    def resolved_backend(self) -> AgentBackendConfig:
        """Old graphs without backend default to structured_llm."""
        if self.backend is None:
            return StructuredLLMBackendConfig()
        return self.backend


class HarnessNodeSpec(BaseNodeSpec):
    node_kind: Literal[NodeKind.HARNESS] = NodeKind.HARNESS
    harness_id: str
    visibility: Literal["public"] = "public"
    command: list[str] | None = None


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
        node["backend"] = {"type": "structured_llm", "max_steps": 1}
    elif isinstance(backend, str):
        node["backend"] = {"type": backend}
    elif isinstance(backend, dict) and "type" not in backend:
        node["backend"] = {"type": "structured_llm", **backend}
    return node
