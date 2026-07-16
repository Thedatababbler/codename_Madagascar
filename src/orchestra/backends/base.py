"""Agent backend protocol and shared request/result schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestra.backends.capabilities import BackendCapabilities
from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.llm.usage import LLMUsage


class AgentRunStatus(StrEnum):
    SUCCESS = "success"
    INVALID_REQUEST = "invalid_request"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BACKEND_INIT_FAILURE = "backend_init_failure"
    MODEL_FAILURE = "model_failure"
    ACTION_PARSE_FAILURE = "action_parse_failure"
    TOOL_FAILURE = "tool_failure"
    MAX_STEPS_EXCEEDED = "max_steps_exceeded"
    OUTPUT_CONTRACT_FAILURE = "output_contract_failure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INFRA_ERROR = "infra_error"


class AgentSessionPolicy(StrEnum):
    """Backend session lifecycle policy. M3.5 only implements FRESH."""

    FRESH = "fresh"
    RESUME = "resume"
    FORK = "fork"


class BackendSessionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_id: str
    session_id: str
    parent_session_id: str | None = None


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    slot: str
    artifact_id: str
    artifact_type: str


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = "openai_compatible"
    name: str
    temperature: float = 0.2
    max_tokens: int = 4096


class OutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    parser_id: str
    output_schema: str
    answer_format: str | None = None
    type: str | None = None


class AgentTraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: str
    index: int | None = None
    timestamp: datetime | None = None
    summary: str | None = None
    message: str | None = None
    payload_ref: str | None = None
    token_usage: dict[str, int] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: AgentRunStatus
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class BackendHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    healthy: bool
    backend_id: str
    detail: str | None = None


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    task_id: str
    subtask_id: str | None = None
    node_id: str
    role: str
    instruction: str
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    rendered_context: str
    model: ModelSpec
    tools: list[str] = Field(default_factory=list)
    max_steps: int = 1
    timeout_seconds: float
    output_contract: OutputContract
    backend_config: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, str]] = Field(default_factory=list)
    contract_id: str | None = None
    session_policy: AgentSessionPolicy = AgentSessionPolicy.FRESH
    session_ref: BackendSessionRef | None = None


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    backend_id: str
    status: AgentRunStatus
    final_output: str | None = None
    output_artifacts: list[ArtifactEnvelope] = Field(default_factory=list)
    trace_events: list[AgentTraceEvent] = Field(default_factory=list)
    usage: LLMUsage = Field(default_factory=LLMUsage)
    latency_ms: int = 0
    step_count: int = 0
    error: AgentError | None = None
    backend_metadata: dict[str, Any] = Field(default_factory=dict)
    session_ref: BackendSessionRef | None = None


class BackendExecutionContext(BaseModel):
    """Narrow, immutable execution context visible to backends."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    task_id: str
    subtask_id: str | None = None
    node_id: str
    deadline: datetime | None = None
    workspace_ref: str | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    trace_dir: str | None = None

    @field_validator("artifact_refs")
    @classmethod
    def _freeze_refs(cls, value: list[ArtifactRef]) -> list[ArtifactRef]:
        return list(value)


@runtime_checkable
class AgentBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    @property
    def capabilities(self) -> BackendCapabilities: ...

    async def run(
        self,
        request: AgentRequest,
        context: BackendExecutionContext,
    ) -> AgentResult: ...

    async def healthcheck(self) -> BackendHealth: ...
