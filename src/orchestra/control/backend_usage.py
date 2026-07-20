"""Normalized backend usage telemetry for M5/M6 objective accounting."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.base import BackendSessionRef
from orchestra.runtime.state import GraphExecutionResult, NodeUsageSnapshot

CostQuality = Literal["exact", "derived", "approximate", "unavailable"]
DimQuality = Literal["exact", "approximate", "unavailable", "derived"]


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
    # None = unavailable; 0.0 = measured zero-duration edge case.
    latency_seconds: float | None = None

    # None = unavailable; 0 = provider reported zero.
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    tool_calls: int | None = None
    sandbox_seconds: float | None = None

    estimated_cost_usd: float | None = None
    cost_quality: CostQuality = "unavailable"
    accounting_source: str
    status: str
    session_ref_identity: str = ""
    model_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObjectiveAccountingQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend_calls: DimQuality = "unavailable"
    tokens: DimQuality = "unavailable"
    cost: DimQuality = "unavailable"
    latency: DimQuality = "unavailable"


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_million_usd: float | None = None
    output_per_million_usd: float | None = None
    cached_input_per_million_usd: float | None = None
    source: str = "configured"


class PricingRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pricing_version: str
    models: dict[str, ModelPrice] = Field(default_factory=dict)


@lru_cache(maxsize=4)
def load_pricing_registry(path: str | None = None) -> PricingRegistry:
    root = Path(__file__).resolve().parents[3]
    cfg = Path(path) if path else root / "configs" / "pricing" / "backend_models.yaml"
    if not cfg.exists():
        return PricingRegistry(pricing_version="missing", models={})
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    models = {
        name: ModelPrice.model_validate(spec)
        for name, spec in (raw.get("models") or {}).items()
    }
    return PricingRegistry(
        pricing_version=str(raw.get("pricing_version") or "unknown"),
        models=models,
    )


def derive_cost_usd(
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None,
    model_name: str | None,
    provider_cost_usd: float | None,
    pricing: PricingRegistry | None = None,
) -> tuple[float | None, CostQuality]:
    if provider_cost_usd is not None:
        return float(provider_cost_usd), "exact"
    if prompt_tokens is None or completion_tokens is None:
        return None, "unavailable"
    registry = pricing or load_pricing_registry()
    if not model_name or model_name not in registry.models:
        return None, "unavailable"
    price = registry.models[model_name]
    if price.input_per_million_usd is None or price.output_per_million_usd is None:
        return None, "unavailable"
    cached = cached_tokens or 0
    billable_input = max(0, prompt_tokens - cached) if cached_tokens is not None else prompt_tokens
    cached_rate = (
        price.cached_input_per_million_usd
        if price.cached_input_per_million_usd is not None
        else price.input_per_million_usd
    )
    cost = (
        billable_input * price.input_per_million_usd
        + cached * cached_rate
        + completion_tokens * price.output_per_million_usd
    ) / 1_000_000.0
    return float(cost), "derived"


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


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
    pricing: PricingRegistry | None = None,
) -> list[BackendUsageRecord]:
    """Normalize provider usage from RuntimeState snapshots / metadata."""
    finished = finished_at or datetime.now(UTC)
    started = started_at or finished
    wall_latency = max(0.0, (finished - started).total_seconds())
    records: list[BackendUsageRecord] = []
    state = result.state
    node_ids = sorted(
        set(state.node_usage_snapshots)
        | set(state.node_latencies_ms)
        | set(state.node_backend_metadata)
    )
    registry = pricing or load_pricing_registry()

    for node_id in node_ids:
        snap: NodeUsageSnapshot | None = state.node_usage_snapshots.get(node_id)
        meta = dict(state.node_backend_metadata.get(node_id) or {})
        raw_ref = meta.get("session_ref")
        session_ref = BackendSessionRef.model_validate(raw_ref) if raw_ref else None
        backend_id = str(
            (snap.backend_id if snap else None)
            or meta.get("backend_id")
            or (session_ref.backend_id if session_ref else "unknown")
        )
        if snap is not None:
            prompt = snap.prompt_tokens
            completion = snap.completion_tokens
            cached = snap.cached_tokens
            provider_cost = snap.provider_cost_usd
            model_name = snap.model_name or meta.get("model_name") or meta.get("model")
            tool_calls = snap.tool_calls
            sandbox = snap.sandbox_seconds
            lat_ms = snap.latency_ms
            node_status = snap.backend_status or str(
                meta.get("backend_status") or meta.get("status") or status
            )
        else:
            usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
            # Do not coerce missing → 0.
            prompt = _optional_int(usage.get("prompt_tokens")) if usage else None
            completion = _optional_int(usage.get("completion_tokens")) if usage else None
            cached = _optional_int(usage.get("cached_tokens")) if usage else None
            if usage and "prompt_tokens" not in usage and "completion_tokens" not in usage:
                prompt = completion = cached = None
            provider_cost = usage.get("estimated_cost_usd") if usage else None
            if provider_cost is not None:
                provider_cost = float(provider_cost)
            model_name = meta.get("model_name") or meta.get("model")
            tool_calls = _optional_int(meta.get("tool_calls") or usage.get("tool_calls"))
            sandbox_raw = meta.get("sandbox_seconds") or usage.get("sandbox_seconds")
            sandbox = float(sandbox_raw) if sandbox_raw is not None else None
            if node_id in state.node_latencies_ms:
                lat_ms = int(state.node_latencies_ms[node_id])
            elif meta.get("latency_ms") is not None:
                lat_ms = int(meta["latency_ms"])
            else:
                lat_ms = int(wall_latency * 1000)
            node_status = str(meta.get("backend_status") or meta.get("status") or status)

        cost, cost_quality = derive_cost_usd(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
            model_name=str(model_name) if model_name else None,
            provider_cost_usd=provider_cost,
            pricing=registry,
        )
        sess_id = session_ref_identity(session_ref)
        cand = candidate_id or "main"
        usage_id = (
            f"{task_id}:{subtask_id}:{node_id}:{attempt_id}:{cand}:{backend_id}:{sess_id}"
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
                latency_seconds=float(lat_ms) / 1000.0,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cached_tokens=cached,
                tool_calls=tool_calls,
                sandbox_seconds=sandbox,
                estimated_cost_usd=cost,
                cost_quality=cost_quality,
                accounting_source=accounting_source,
                status=node_status,
                session_ref_identity=sess_id,
                model_name=str(model_name) if model_name else None,
                metadata={
                    k: v
                    for k, v in meta.items()
                    if k in {"provider_request_id", "model", "model_name"}
                },
            )
        )
    return records


def exception_usage_record(
    *,
    task_id: str,
    subtask_id: str,
    attempt_id: int,
    node_id: str = "__control_plane__",
    backend_id: str = "unknown",
    candidate_id: str | None = None,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    accounting_source: str,
) -> BackendUsageRecord:
    """Record a known invocation that failed before provider usage was available."""
    cand = candidate_id or "main"
    latency = max(0.0, (finished_at - started_at).total_seconds())
    usage_id = (
        f"{task_id}:{subtask_id}:{node_id}:{attempt_id}:{cand}:{backend_id}:exception"
    )
    return BackendUsageRecord(
        usage_id=usage_id,
        task_id=task_id,
        subtask_id=subtask_id,
        node_id=node_id,
        backend_id=backend_id,
        attempt_id=attempt_id,
        candidate_id=candidate_id,
        started_at=started_at,
        finished_at=finished_at,
        latency_seconds=latency,
        prompt_tokens=None,
        completion_tokens=None,
        cached_tokens=None,
        estimated_cost_usd=None,
        cost_quality="unavailable",
        accounting_source=accounting_source,
        status=status,
        session_ref_identity="exception",
    )


def append_usage_records(
    existing: list[BackendUsageRecord] | list[Any],
    new_records: list[BackendUsageRecord],
) -> list[BackendUsageRecord]:
    """Deduplicate by usage_id for checkpoint resume safety."""
    out: list[BackendUsageRecord] = []
    seen: set[str] = set()
    for raw in list(existing or []) + list(new_records or []):
        rec = (
            raw
            if isinstance(raw, BackendUsageRecord)
            else BackendUsageRecord.model_validate(raw)
        )
        if rec.usage_id in seen:
            continue
        seen.add(rec.usage_id)
        out.append(rec)
    return out


def summarize_objective_accounting(
    records: list[BackendUsageRecord],
) -> ObjectiveAccountingQuality:
    if not records:
        return ObjectiveAccountingQuality()
    tokens_q: DimQuality = "exact"
    for r in records:
        if r.prompt_tokens is None or r.completion_tokens is None:
            tokens_q = "unavailable"
            break
    # Prefer explicit cost_quality; fall back to presence of estimated_cost_usd
    # for records constructed before cost_quality was stamped.
    effective: set[CostQuality] = set()
    for r in records:
        if r.cost_quality != "unavailable":
            effective.add(r.cost_quality)
        elif r.estimated_cost_usd is not None:
            effective.add("exact")
        else:
            effective.add("unavailable")
    if effective == {"exact"}:
        cost_q: DimQuality = "exact"
    elif effective <= {"exact", "derived"} and "unavailable" not in effective:
        cost_q = "derived"
    elif "unavailable" in effective and effective - {"unavailable"}:
        cost_q = "approximate"
    elif effective == {"unavailable"}:
        cost_q = "unavailable"
    else:
        cost_q = "approximate"
    latency_q: DimQuality = (
        "exact"
        if all(r.latency_seconds is not None for r in records)
        else "unavailable"
    )
    return ObjectiveAccountingQuality(
        backend_calls="exact",
        tokens=tokens_q,
        cost=cost_q,
        latency=latency_q,
    )
