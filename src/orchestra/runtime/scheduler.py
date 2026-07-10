from collections import defaultdict

from orchestra.ir.graph import OrchestraGraph
from orchestra.runtime.state import NodeStatus, RuntimeState

TERMINAL = {
    NodeStatus.SUCCEEDED,
    NodeStatus.FAILED,
    NodeStatus.SKIPPED,
    NodeStatus.CANCELLED,
}


class Scheduler:
    def __init__(self, max_parallel_nodes: int) -> None:
        self.max_parallel_nodes = max_parallel_nodes

    def incoming(self, graph: OrchestraGraph):
        result = defaultdict(list)
        for edge in graph.edges:
            result[(edge.destination_node, edge.destination_input)].append(edge)
        return result

    def resolve_input_ids(
        self, graph: OrchestraGraph, state: RuntimeState, node_id: str
    ) -> dict[str, str]:
        node = next(node for node in graph.nodes if node.node_id == node_id)
        incoming = self.incoming(graph)
        resolved = {}
        for slot in node.input_slots:
            if slot in state.initial_artifacts:
                resolved[slot] = state.initial_artifacts[slot]
                continue
            for edge in incoming[(node_id, slot)]:
                if edge.edge_id not in state.active_edges:
                    continue
                artifact_id = state.node_outputs.get(edge.source_node, {}).get(
                    edge.source_output
                )
                if artifact_id:
                    resolved[slot] = artifact_id
                    break
        return resolved

    def find_ready_nodes(
        self, graph: OrchestraGraph, state: RuntimeState
    ) -> list[str]:
        ready = []
        for node in sorted(graph.nodes, key=lambda item: item.node_id):
            if state.node_status[node.node_id] is not NodeStatus.PENDING:
                continue
            resolved = self.resolve_input_ids(graph, state, node.node_id)
            if set(resolved) == set(node.input_slots):
                ready.append(node.node_id)
        return ready[: self.max_parallel_nodes]

    def impossible_nodes(
        self, graph: OrchestraGraph, state: RuntimeState
    ) -> list[str]:
        incoming = self.incoming(graph)
        impossible = []
        for node in graph.nodes:
            if state.node_status[node.node_id] is not NodeStatus.PENDING:
                continue
            for slot in node.input_slots:
                if slot in state.initial_artifacts:
                    continue
                edges = incoming[(node.node_id, slot)]
                if edges and all(
                    edge.edge_id in state.inactive_edges
                    or state.node_status[edge.source_node] in {
                        NodeStatus.FAILED,
                        NodeStatus.SKIPPED,
                        NodeStatus.CANCELLED,
                    }
                    for edge in edges
                ):
                    impossible.append(node.node_id)
                    break
        return impossible

    def can_finalize(self, state: RuntimeState) -> bool:
        return state.final_output_artifact_id is not None
