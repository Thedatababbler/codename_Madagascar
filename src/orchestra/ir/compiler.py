from collections import defaultdict, deque

from pydantic import BaseModel

from orchestra.ir.artifacts import PAYLOAD_SCHEMAS
from orchestra.ir.contracts import AgentContract
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import (
    AgentNodeSpec,
    HarnessNodeSpec,
    SelectorNodeSpec,
    TransformNodeSpec,
)


class GraphCompilationError(ValueError):
    pass


class CompiledGraph(BaseModel):
    graph: OrchestraGraph
    entry_nodes: list[str]
    waves: list[list[str]]
    conditional_branches: int
    final_producers: list[str]


class GraphCompiler:
    def __init__(
        self,
        *,
        contracts: dict[str, AgentContract],
        harness_ids: set[str],
        transform_ids: set[str],
        selector_ids: set[str],
    ) -> None:
        self.contracts = contracts
        self.harness_ids = harness_ids
        self.transform_ids = transform_ids
        self.selector_ids = selector_ids

    def compile(self, graph: OrchestraGraph) -> CompiledGraph:
        node_ids = [node.node_id for node in graph.nodes]
        edge_ids = [edge.edge_id for edge in graph.edges]
        if len(set(node_ids)) != len(node_ids):
            raise GraphCompilationError("Duplicate node IDs")
        if len(set(edge_ids)) != len(edge_ids):
            raise GraphCompilationError("Duplicate edge IDs")
        nodes = {node.node_id: node for node in graph.nodes}

        for node in graph.nodes:
            if isinstance(node, AgentNodeSpec) and node.contract_id not in self.contracts:
                raise GraphCompilationError(f"Missing contract: {node.contract_id}")
            if isinstance(node, AgentNodeSpec) and node.contract_id in self.contracts:
                contract = self.contracts[node.contract_id]
                if contract.output_schema not in node.output_slots.values():
                    raise GraphCompilationError(
                        f"Contract {contract.contract_id} output {contract.output_schema} "
                        f"is not declared by node {node.node_id}"
                    )
                if (
                    contract.input_schema != "ArtifactBundle"
                    and contract.input_schema not in node.input_slots.values()
                ):
                    raise GraphCompilationError(
                        f"Contract {contract.contract_id} input {contract.input_schema} "
                        f"is not declared by node {node.node_id}"
                    )
            if isinstance(node, HarnessNodeSpec) and node.harness_id not in self.harness_ids:
                raise GraphCompilationError(f"Missing harness: {node.harness_id}")
            if isinstance(node, TransformNodeSpec) and node.transform_id not in self.transform_ids:
                raise GraphCompilationError(f"Missing transform: {node.transform_id}")
            if isinstance(node, SelectorNodeSpec) and node.selector_id not in self.selector_ids:
                raise GraphCompilationError(f"Missing selector: {node.selector_id}")

        adjacency: dict[str, list[str]] = defaultdict(list)
        indegree = {node_id: 0 for node_id in node_ids}
        incoming_slots: dict[tuple[str, str], int] = defaultdict(int)
        for edge in graph.edges:
            if edge.source_node not in nodes or edge.destination_node not in nodes:
                raise GraphCompilationError(f"Unknown node in edge {edge.edge_id}")
            source = nodes[edge.source_node]
            destination = nodes[edge.destination_node]
            if edge.source_output not in source.output_slots:
                raise GraphCompilationError(
                    f"Unknown source slot {edge.source_node}.{edge.source_output}"
                )
            if edge.destination_input not in destination.input_slots:
                raise GraphCompilationError(
                    f"Unknown destination slot {edge.destination_node}.{edge.destination_input}"
                )
            source_type = source.output_slots[edge.source_output]
            destination_type = destination.input_slots[edge.destination_input]
            if source_type != destination_type:
                raise GraphCompilationError(
                    f"Incompatible edge {edge.edge_id}: {source_type} -> {destination_type}"
                )
            if edge.condition is not None:
                schema = PAYLOAD_SCHEMAS.get(source_type)
                field = edge.condition.source_field.split(".", 1)[0]
                if schema is None or field not in schema.model_fields:
                    raise GraphCompilationError(
                        f"Conditional edge {edge.edge_id} references missing field "
                        f"{source_type}.{field}"
                    )
            adjacency[edge.source_node].append(edge.destination_node)
            indegree[edge.destination_node] += 1
            incoming_slots[(edge.destination_node, edge.destination_input)] += 1

        entry_nodes = []
        for node in graph.nodes:
            initial_match = all(
                slot in graph.initial_artifact_slots
                and graph.initial_artifact_slots[slot] == schema
                for slot, schema in node.input_slots.items()
            )
            if initial_match:
                entry_nodes.append(node.node_id)
            for slot in node.input_slots:
                if slot not in graph.initial_artifact_slots and not incoming_slots[
                    (node.node_id, slot)
                ]:
                    raise GraphCompilationError(f"Unbound input slot: {node.node_id}.{slot}")
        if not entry_nodes:
            raise GraphCompilationError("No node can consume initial artifacts")

        queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
        waves = []
        visited = 0
        current = list(queue)
        while current:
            waves.append(current)
            next_nodes = []
            for node_id in current:
                visited += 1
                for target in adjacency[node_id]:
                    indegree[target] -= 1
                    if indegree[target] == 0:
                        next_nodes.append(target)
            current = sorted(next_nodes)
        if visited != len(nodes):
            raise GraphCompilationError("Graph contains a cycle")

        final_producers = [
            node.node_id
            for node in graph.nodes
            if graph.final_output_slot in node.output_slots
        ]
        if not final_producers:
            raise GraphCompilationError(
                f"No node produces final output slot {graph.final_output_slot}"
            )
        reachable = set(entry_nodes)
        changed = True
        while changed:
            changed = False
            for source in tuple(reachable):
                for target in adjacency[source]:
                    if target not in reachable:
                        reachable.add(target)
                        changed = True
        if not any(node in reachable for node in final_producers):
            raise GraphCompilationError("Declared final output is unreachable")

        return CompiledGraph(
            graph=graph,
            entry_nodes=entry_nodes,
            waves=waves,
            conditional_branches=sum(edge.condition is not None for edge in graph.edges),
            final_producers=final_producers,
        )
