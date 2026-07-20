"""Normalized backend usage telemetry for M5/M6 objective accounting."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.base import BackendSessionRef
from orchestra.runtime.state import GraphExecutionResult


def session_ref_identity(session_ref: BackendSessionRef | dict[str, Any] | None) -> str:
    """Stable identity from session_ref (never invent attempt_id='1')."""
    if session_ref is None:
        return "nosession"
    if isinstance(session_ref, BackendSessionRef):
        sid = (session_ref.session_id or "").strip()
        if sid:
            return sid
        dumped = session_ref.model_dump(mode="json")
    elif isinstance(session_ref, dict):
        sid = str(session_ref.get("session_id") or "").strip()
        if sid:
            return sid
        dumped = session_ref
    else:
        dumped = {"raw": str(session_ref)}
    digest = hashlib.sha256(
        json.dumps(dumped, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"sess-{digest[:12]}"


class BackendUsageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage_id: str
    task_id: str
    subtask_id: str
    node_id: str
    backend_id: str
    attempt_id: int
    candidate_id: str | None = None

    started_at: datetime
    finished_at: datetime
    latency_seconds: float

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    tool_calls: int = 0
    sandbox_seconds: float = 0.0

    estimated_cost_usd: float | None = None
    accounting_source: str
    status: str
    session_ref_identity: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObjectiveAccountingQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend_calls: Literal["exact", "approximate", "unavailable"] = "unavailable"
    tokens: Literal["exact", "approximate", "unavailable"] = "unavailable"
    cost: Literal["exact", "approximate", "unavailable"] = "unavailable"
    latency: Literal["exact", "approximate", "unavailable"] = "unavailable"


def collect_usage_from_graph_result(
    *,
    task_id: str,
    subtask_id: str,
    attempt_id: int,
    result: GraphExecutionResult,
    candidate_id: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    status: str = "unknown",
    accounting_source: str = "graph_execution",
) -> list[BackendUsageRecord]:
    """Normalize provider usage from the shared graph-execution boundary."""
    finished = finished_at or datetime.now(UTC)
    started = started_at or finished
    latency = max(0.0, (finished - started).total_seconds())
    records: list[BackendUsageRecord] = []
    for node_id, meta in result.state.node_backend_metadata.items():
        raw_ref = meta.get("session_ref")
        session_ref = None
        if raw_ref:
            session_ref = BackendSessionRef.model_validate(raw_ref)
        backend_id = str(
            meta.get("backend_id")
            or (session_ref.backend_id if session_ref else "unknown")
        )
        usage = meta.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        cached = int(usage.get("cached_tokens") or 0)
        tool_calls = int(usage.get("tool_calls") or meta.get("tool_calls") or 0)
        sandbox = float(usage.get("sandbox_seconds") or meta.get("sandbox_seconds") or 0.0)
        cost = usage.get("estimated_cost_usd")
        if cost is None and (prompt or completion):
            cost = (prompt * 0.15 + completion * 0.60) / 1_000_000
        elif cost is not None:
            cost = float(cost)
        node_latency = meta.get("latency_seconds")
        if node_latency is None and meta.get("latency_ms") is not None:
            node_latency = float(meta["latency_ms"]) / 1000.0
        lat = float(node_latency) if node_latency is not None else latency
        sess_id = session_ref_identity(session_ref)
        cand = candidate_id or "main"
        usage_id = (
            f"{task_id}:{subtask_id}:{node_id}:{attempt_id}:{cand}:{backend_id}:{sess_id}"
        )
        node_status = str(
            meta.get("backend_status") or meta.get("status") or status
        )
        records.append(
            BackendUsageRecord(
                usage_id=usage_id,
                task_id=task_id,
                subtask_id=subtask_id,
                node_id=node_id,
                backend_id=backend_id,
                attempt_id=attempt_id,
                candidate_id=candidate_id,
                started_at=started,
                finished_at=finished,
                latency_seconds=lat,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                tool_calls=tool_calls,
                sandbox_seconds=sandbox,
                estimated_cost_usd=cost,
                accounting_source=accounting_source,
                status=node_status,
                session_ref_identity=sess_id,
                metadata={
                    k: v
                    for k, v in meta.items()
                    if k
                    in {
                        "model",
                        "model_name",
                        "provider_request_id",
                    }
                },
            )
        )
    return records


def summarize_objective_accounting(
    records: list[BackendUsageRecord],
) -> ObjectiveAccountingQuality:
    if not records:
        return ObjectiveAccountingQuality()
    tokens_exact = all(
        isinstance(r.prompt_tokens, int) and isinstance(r.completion_tokens, int)
        for r in records
    )
    cost_exact = all(r.estimated_cost_usd is not None for r in records)
    latency_exact = all(r.latency_seconds is not None for r in records)
    return ObjectiveAccountingQuality(
        backend_calls="exact",
        tokens="exact" if tokens_exact else "approximate",
        cost="exact" if cost_exact else "unavailable",
        latency="exact" if latency_exact else "approximate",
    )
