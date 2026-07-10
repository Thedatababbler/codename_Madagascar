"""Pre-run healthchecks for backends referenced by a compiled graph."""

from __future__ import annotations

from orchestra.backends.errors import BackendInitializationError
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.ir.compiler import CompiledGraph
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.telemetry.events import TelemetryEvent


def used_backend_ids(graph: CompiledGraph) -> list[str]:
    ids: set[str] = set()
    for node in graph.graph.nodes:
        if isinstance(node, AgentNodeSpec):
            ids.add(node.resolved_backend().type)
    return sorted(ids)


async def healthcheck_used_backends(
    *,
    registry: AgentBackendRegistry,
    graph: CompiledGraph,
    event_writer: AppendOnlyEventWriter,
    run_id: str,
    graph_id: str,
) -> None:
    """Fail closed if any backend used by the graph fails healthcheck."""
    for backend_id in used_backend_ids(graph):
        await event_writer.append(
            TelemetryEvent(
                run_id=run_id,
                task_id=None,
                graph_id=graph_id,
                event_type="backend_healthcheck_started",
                metadata={"backend_id": backend_id},
            )
        )
        if not registry.has(backend_id):
            detail = f"Backend {backend_id!r} is not registered"
            await event_writer.append(
                TelemetryEvent(
                    run_id=run_id,
                    task_id=None,
                    graph_id=graph_id,
                    event_type="backend_healthcheck_failed",
                    status="failed",
                    metadata={"backend_id": backend_id, "detail": detail},
                )
            )
            raise BackendInitializationError(detail)
        health = await registry.get(backend_id).healthcheck()
        if not health.healthy:
            detail = health.detail or "healthcheck failed"
            await event_writer.append(
                TelemetryEvent(
                    run_id=run_id,
                    task_id=None,
                    graph_id=graph_id,
                    event_type="backend_healthcheck_failed",
                    status="failed",
                    metadata={"backend_id": backend_id, "detail": detail},
                )
            )
            raise BackendInitializationError(
                f"Backend {backend_id!r} healthcheck failed: {detail}"
            )
        await event_writer.append(
            TelemetryEvent(
                run_id=run_id,
                task_id=None,
                graph_id=graph_id,
                event_type="backend_healthcheck_passed",
                status="passed",
                metadata={"backend_id": backend_id, "detail": health.detail},
            )
        )
