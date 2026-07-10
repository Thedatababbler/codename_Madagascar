from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.base import AgentRunStatus, AgentTraceEvent
from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.llm.usage import LLMUsage


class NodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RuntimeState(BaseModel):
    run_id: str
    task_id: str
    graph_id: str
    graph_hash: str
    contract_hash: str
    node_status: dict[str, NodeStatus]
    node_outputs: dict[str, dict[str, str]] = Field(default_factory=dict)
    initial_artifacts: dict[str, str] = Field(default_factory=dict)
    active_edges: set[str] = Field(default_factory=set)
    inactive_edges: set[str] = Field(default_factory=set)
    final_output_artifact_id: str | None = None
    frozen: bool = False
    wave_id: int = 0
    checkpoint_count: int = 0
    node_latencies_ms: dict[str, int] = Field(default_factory=dict)


class NodeExecutionResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    node_id: str
    succeeded: bool
    outputs: dict[str, ArtifactEnvelope] = Field(default_factory=dict)
    error: str | None = None
    latency_ms: int = 0
    usage: LLMUsage = Field(default_factory=LLMUsage)
    backend_id: str | None = None
    backend_status: AgentRunStatus | None = None
    trace_events: list[AgentTraceEvent] = Field(default_factory=list)
    backend_metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def failed(cls, node_id: str, error: Exception, latency_ms: int = 0):
        return cls(
            node_id=node_id,
            succeeded=False,
            error=f"{type(error).__name__}: {error}",
            latency_ms=latency_ms,
        )


class GraphExecutionResult(BaseModel):
    state: RuntimeState
    wall_latency_ms: int
    sum_node_latency_ms: int
    critical_path_latency_ms: int
    parallel_node_count: int
    concurrency_speedup: float
    failed_node_count: int
    skipped_node_count: int
    checkpoint_count: int
