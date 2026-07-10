from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class TelemetryEvent(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str
    task_id: str | None
    graph_id: str
    event_type: str
    wave_id: int | None = None
    node_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_usd: float | None = None
    status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
