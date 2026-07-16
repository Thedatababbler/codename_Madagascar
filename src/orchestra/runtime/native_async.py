import asyncio
import time

from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle
from orchestra.ir.compiler import CompiledGraph
from orchestra.runtime.backend import RunContext, RuntimeBackend
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.committer import WaveCommitter
from orchestra.runtime.errors import GraphDeadlockError
from orchestra.runtime.scheduler import Scheduler
from orchestra.runtime.state import (
    GraphExecutionResult,
    NodeExecutionResult,
    NodeStatus,
    RuntimeState,
)
from orchestra.storage.artifacts import ArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.telemetry.events import TelemetryEvent


class NativeAsyncRuntime(RuntimeBackend):
    def __init__(
        self,
        *,
        executors: NodeExecutorRegistry,
        artifact_store: ArtifactStore,
        checkpoint_store: CheckpointStore,
        event_writer: AppendOnlyEventWriter,
    ) -> None:
        self.executors = executors
        self.artifact_store = artifact_store
        self.checkpoint_store = checkpoint_store
        self.event_writer = event_writer

    async def _event(
        self, context: RunContext, graph_id: str, event_type: str, **kwargs
    ) -> None:
        await self.event_writer.append(
            TelemetryEvent(
                run_id=context.run_id,
                task_id=context.task_id,
                graph_id=graph_id,
                event_type=event_type,
                **kwargs,
            )
        )

    async def _load_inputs(
        self,
        graph: CompiledGraph,
        state: RuntimeState,
        node_id: str,
        scheduler: Scheduler,
    ):
        ids = scheduler.resolve_input_ids(graph.graph, state, node_id)
        return {slot: await self.artifact_store.get(aid) for slot, aid in ids.items()}

    async def execute(
        self,
        *,
        graph: CompiledGraph,
        initial_artifacts: ArtifactBundle,
        context: RunContext,
    ) -> GraphExecutionResult:
        started = time.perf_counter()
        scheduler = Scheduler(context.limits.max_parallel_nodes_per_task)
        committer = WaveCommitter(self.artifact_store, scheduler)
        state = await self.checkpoint_store.load(
            context.task_id,
            graph_hash=graph.graph.content_hash,
            contract_hash=context.contract_hash,
            allow_config_drift=context.allow_config_drift,
        )
        if state is None:
            for artifact in initial_artifacts.slots.values():
                await self.artifact_store.put(artifact)
            state = RuntimeState(
                run_id=context.run_id,
                task_id=context.task_id,
                graph_id=graph.graph.graph_id,
                graph_hash=graph.graph.content_hash,
                contract_hash=context.contract_hash,
                node_status={
                    node.node_id: NodeStatus.PENDING for node in graph.graph.nodes
                },
                initial_artifacts={
                    slot: artifact.artifact_id
                    for slot, artifact in initial_artifacts.slots.items()
                },
                active_edges={
                    edge.edge_id for edge in graph.graph.edges if edge.condition is None
                },
            )
        else:
            referenced_ids = [
                *state.initial_artifacts.values(),
                *(
                    artifact_id
                    for outputs in state.node_outputs.values()
                    for artifact_id in outputs.values()
                ),
            ]
            for artifact_id in referenced_ids:
                await self.artifact_store.get(artifact_id)
        if state.frozen:
            return self._result(graph, state, started)

        node_map = {node.node_id: node for node in graph.graph.nodes}
        while not state.frozen:
            for node_id in scheduler.impossible_nodes(graph.graph, state):
                state.node_status[node_id] = NodeStatus.SKIPPED
                await self._event(
                    context,
                    graph.graph.graph_id,
                    "NODE_SKIPPED",
                    wave_id=state.wave_id,
                    node_id=node_id,
                )
            ready = scheduler.find_ready_nodes(graph.graph, state)
            if not ready:
                if scheduler.can_finalize(state):
                    state.frozen = True
                    break
                # Exhausted graph without a final output (e.g. harness gate failed
                # and freeze was skipped). Return unfrozen state for control-plane
                # classification instead of raising — sessions/metadata stay intact.
                terminal = {
                    NodeStatus.SUCCEEDED,
                    NodeStatus.FAILED,
                    NodeStatus.SKIPPED,
                    NodeStatus.CANCELLED,
                }
                if state.node_status and all(
                    status in terminal for status in state.node_status.values()
                ):
                    await self.checkpoint_store.save(state)
                    break
                report = {
                    node_id: status.value for node_id, status in state.node_status.items()
                }
                raise GraphDeadlockError(f"No ready nodes and no final output: {report}")

            await self._event(
                context,
                graph.graph.graph_id,
                "WAVE_STARTED",
                wave_id=state.wave_id,
                metadata={"ready_nodes": ready},
            )
            task_map: dict[str, asyncio.Task[NodeExecutionResult]] = {}
            inputs = {}
            for node_id in ready:
                state.node_status[node_id] = NodeStatus.RUNNING
                inputs[node_id] = await self._load_inputs(
                    graph, state, node_id, scheduler
                )
                await self._event(
                    context,
                    graph.graph.graph_id,
                    "NODE_READY",
                    wave_id=state.wave_id,
                    node_id=node_id,
                )
                await self._event(
                    context,
                    graph.graph.graph_id,
                    "NODE_STARTED",
                    wave_id=state.wave_id,
                    node_id=node_id,
                )
            async with asyncio.TaskGroup() as group:
                for node_id in ready:
                    task_map[node_id] = group.create_task(
                        self.executors.execute_safely(
                            node_map[node_id], inputs[node_id], context
                        ),
                        name=f"{context.task_id}:{node_id}",
                    )
            results = [task_map[node_id].result() for node_id in ready]
            state, committed, activated, disabled = await committer.commit_wave(
                graph=graph.graph, previous_state=state, results=results
            )
            for result in results:
                if result.backend_id is not None:
                    await self._event(
                        context,
                        graph.graph.graph_id,
                        "backend_run_started",
                        wave_id=state.wave_id - 1,
                        node_id=result.node_id,
                        metadata={"backend_id": result.backend_id},
                    )
                    for event in result.trace_events:
                        await self._event(
                            context,
                            graph.graph.graph_id,
                            "backend_step",
                            wave_id=state.wave_id - 1,
                            node_id=result.node_id,
                            prompt_tokens=event.token_usage.get("prompt_tokens"),
                            completion_tokens=event.token_usage.get(
                                "completion_tokens"
                            ),
                            metadata={
                                "backend_id": result.backend_id,
                                "event_type": event.event_type,
                                "index": event.index,
                                "summary": event.summary or event.message,
                                "payload_ref": event.payload_ref,
                                "raw_trace_path": result.backend_metadata.get(
                                    "raw_trace_path"
                                ),
                            },
                        )
                    terminal = (
                        "backend_run_completed"
                        if result.succeeded
                        else "backend_run_failed"
                    )
                    await self._event(
                        context,
                        graph.graph.graph_id,
                        terminal,
                        wave_id=state.wave_id - 1,
                        node_id=result.node_id,
                        latency_ms=result.latency_ms,
                        status=(
                            result.backend_status.value
                            if result.backend_status is not None
                            else None
                        ),
                        metadata={
                            "backend_id": result.backend_id,
                            "backend_status": (
                                result.backend_status.value
                                if result.backend_status is not None
                                else None
                            ),
                            "trace_summary": result.backend_metadata.get(
                                "trace_summary", []
                            ),
                            "raw_trace_path": result.backend_metadata.get(
                                "raw_trace_path"
                            ),
                            "error": result.error,
                        },
                    )
                await self._event(
                    context,
                    graph.graph.graph_id,
                    "NODE_COMPLETED" if result.succeeded else "NODE_FAILED",
                    wave_id=state.wave_id - 1,
                    node_id=result.node_id,
                    artifact_ids=[
                        artifact.artifact_id for artifact in result.outputs.values()
                    ],
                    latency_ms=result.latency_ms,
                    prompt_tokens=result.usage.prompt_tokens,
                    completion_tokens=result.usage.completion_tokens,
                    estimated_cost_usd=result.usage.estimated_cost_usd,
                    status="succeeded" if result.succeeded else "failed",
                    metadata={
                        **({"error": result.error} if result.error else {}),
                        "backend_id": result.backend_id,
                        "backend_status": (
                            result.backend_status.value
                            if result.backend_status is not None
                            else None
                        ),
                    },
                )
                for artifact in result.outputs.values():
                    await self._event(
                        context,
                        graph.graph.graph_id,
                        "ARTIFACT_COMMITTED",
                        wave_id=state.wave_id - 1,
                        node_id=result.node_id,
                        artifact_ids=[artifact.artifact_id],
                    )
            for edge_id in activated:
                await self._event(
                    context,
                    graph.graph.graph_id,
                    "CONDITIONAL_EDGE_ACTIVATED",
                    wave_id=state.wave_id - 1,
                    metadata={"edge_id": edge_id},
                )
            for edge_id in disabled:
                await self._event(
                    context,
                    graph.graph.graph_id,
                    "CONDITIONAL_EDGE_DISABLED",
                    wave_id=state.wave_id - 1,
                    metadata={"edge_id": edge_id},
                )
            await self.checkpoint_store.save(state)
            await self._event(
                context,
                graph.graph.graph_id,
                "WAVE_COMMITTED",
                wave_id=state.wave_id - 1,
                artifact_ids=committed,
            )
            await self._event(
                context,
                graph.graph.graph_id,
                "CHECKPOINT_SAVED",
                wave_id=state.wave_id - 1,
            )
        if state.frozen:
            await self._event(
                context, graph.graph.graph_id, "FINAL_OUTPUT_FROZEN"
            )
        return self._result(graph, state, started)

    def _result(
        self, graph: CompiledGraph, state: RuntimeState, started: float
    ) -> GraphExecutionResult:
        wall = int((time.perf_counter() - started) * 1000)
        total = sum(state.node_latencies_ms.values())
        latency = state.node_latencies_ms
        longest: dict[str, int] = {}
        for wave in graph.waves:
            for node_id in wave:
                parents = [
                    edge.source_node
                    for edge in graph.graph.edges
                    if edge.destination_node == node_id
                ]
                longest[node_id] = latency.get(node_id, 0) + max(
                    (longest.get(parent, 0) for parent in parents), default=0
                )
        critical = max(longest.values(), default=0)
        return GraphExecutionResult(
            state=state,
            wall_latency_ms=wall,
            sum_node_latency_ms=total,
            critical_path_latency_ms=critical,
            parallel_node_count=sum(
                len(wave) for wave in graph.waves if len(wave) > 1
            ),
            concurrency_speedup=total / wall if wall else 1.0,
            failed_node_count=sum(
                status is NodeStatus.FAILED for status in state.node_status.values()
            ),
            skipped_node_count=sum(
                status is NodeStatus.SKIPPED for status in state.node_status.values()
            ),
            checkpoint_count=state.checkpoint_count,
        )
