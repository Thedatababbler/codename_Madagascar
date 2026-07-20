from orchestra.ir.graph import OrchestraGraph
from orchestra.runtime.scheduler import Scheduler
from orchestra.runtime.state import (
    NodeExecutionResult,
    NodeStatus,
    NodeUsageSnapshot,
    RuntimeState,
)
from orchestra.storage.artifacts import ArtifactStore


def _snapshot_from_result(result: NodeExecutionResult) -> NodeUsageSnapshot:
    """Persist NodeExecutionResult usage without inventing missing tokens as zero."""
    usage = result.usage
    empty = (
        usage.prompt_tokens == 0
        and usage.completion_tokens == 0
        and usage.cached_tokens == 0
        and usage.total_tokens == 0
        and usage.estimated_cost_usd is None
    )
    # Failed/exception default LLMUsage → unavailable tokens/cost.
    if empty and not result.succeeded:
        prompt = completion = cached = None
        provider_cost = None
        usage_source = "unavailable"
    else:
        prompt = int(usage.prompt_tokens)
        completion = int(usage.completion_tokens)
        cached = int(usage.cached_tokens)
        provider_cost = usage.estimated_cost_usd
        usage_source = "provider" if provider_cost is not None else "node_execution_result"
    meta = result.backend_metadata or {}
    model_name = None
    for key in ("model_name", "model", "model_id"):
        if meta.get(key):
            model_name = str(meta[key])
            break
    tool_calls = meta.get("tool_calls")
    sandbox = meta.get("sandbox_seconds")
    return NodeUsageSnapshot(
        node_id=result.node_id,
        backend_id=result.backend_id,
        backend_status=(
            result.backend_status.value if result.backend_status is not None else None
        ),
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        provider_cost_usd=provider_cost,
        latency_ms=int(result.latency_ms),
        tool_calls=int(tool_calls) if tool_calls is not None else None,
        sandbox_seconds=float(sandbox) if sandbox is not None else None,
        model_name=model_name,
        usage_source=usage_source,
    )


class WaveCommitter:
    def __init__(self, artifact_store: ArtifactStore, scheduler: Scheduler) -> None:
        self.artifact_store = artifact_store
        self.scheduler = scheduler

    async def commit_wave(
        self,
        *,
        graph: OrchestraGraph,
        previous_state: RuntimeState,
        results: list[NodeExecutionResult],
    ) -> tuple[RuntimeState, list[str], list[str], list[str]]:
        state = previous_state.model_copy(deep=True)
        committed: list[str] = []
        activated: list[str] = []
        disabled: list[str] = []

        # Persist all validated artifacts before exposing any output in state.
        for result in results:
            if result.succeeded:
                for artifact in result.outputs.values():
                    artifact.validate_payload()
                    await self.artifact_store.put(artifact)

        for result in sorted(results, key=lambda item: item.node_id):
            state.node_latencies_ms[result.node_id] = result.latency_ms
            snapshot = _snapshot_from_result(result)
            state.node_usage_snapshots[result.node_id] = snapshot
            meta = dict(result.backend_metadata)
            if result.backend_id is not None:
                meta.setdefault("backend_id", result.backend_id)
            if result.backend_status is not None:
                meta.setdefault("backend_status", result.backend_status.value)
            if result.error:
                meta.setdefault("error", result.error)
            meta["latency_ms"] = result.latency_ms
            # Mirror typed snapshot into metadata for legacy readers.
            meta["usage"] = {
                "prompt_tokens": snapshot.prompt_tokens,
                "completion_tokens": snapshot.completion_tokens,
                "cached_tokens": snapshot.cached_tokens,
                "estimated_cost_usd": snapshot.provider_cost_usd,
            }
            state.node_backend_metadata[result.node_id] = meta
            if not result.succeeded:
                state.node_status[result.node_id] = NodeStatus.FAILED
                continue
            state.node_status[result.node_id] = NodeStatus.SUCCEEDED
            state.node_outputs[result.node_id] = {
                slot: artifact.artifact_id for slot, artifact in result.outputs.items()
            }
            committed.extend(artifact.artifact_id for artifact in result.outputs.values())
            for slot, artifact in result.outputs.items():
                if slot == graph.final_output_slot and not state.frozen:
                    state.final_output_artifact_id = artifact.artifact_id
                    state.frozen = True
            for edge in graph.edges:
                if edge.source_node != result.node_id or edge.condition is None:
                    continue
                source_artifact = result.outputs.get(edge.source_output)
                enabled = bool(
                    source_artifact and edge.condition.evaluate(source_artifact.payload)
                )
                if enabled:
                    state.active_edges.add(edge.edge_id)
                    activated.append(edge.edge_id)
                else:
                    state.inactive_edges.add(edge.edge_id)
                    disabled.append(edge.edge_id)

        for node_id in self.scheduler.impossible_nodes(graph, state):
            state.node_status[node_id] = NodeStatus.SKIPPED
        state.wave_id += 1
        state.checkpoint_count += 1
        return state, committed, activated, disabled
