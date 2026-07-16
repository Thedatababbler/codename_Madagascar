from orchestra.ir.graph import OrchestraGraph
from orchestra.runtime.scheduler import Scheduler
from orchestra.runtime.state import NodeExecutionResult, NodeStatus, RuntimeState
from orchestra.storage.artifacts import ArtifactStore


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
            meta = dict(result.backend_metadata)
            if result.backend_id is not None:
                meta.setdefault("backend_id", result.backend_id)
            if result.backend_status is not None:
                meta.setdefault("backend_status", result.backend_status.value)
            if result.error:
                meta.setdefault("error", result.error)
            if meta:
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
