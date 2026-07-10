import pytest

from orchestra.cli.validate_graph import build_compiler
from orchestra.ir.compiler import GraphCompilationError
from orchestra.ir.graph import load_graph


def test_all_baseline_graphs_compile():
    compiler = build_compiler("configs/contracts")
    for name in ("b0_direct", "b1_single_harness", "b2_fixed_mas"):
        compiled = compiler.compile(load_graph(f"configs/graphs/{name}.yaml"))
        assert compiled.final_producers
    b2 = compiler.compile(load_graph("configs/graphs/b2_fixed_mas.yaml"))
    assert {"algorithm_analyst", "edge_case_analyst"} <= set(b2.waves[0])


def test_cycle_is_rejected():
    graph = load_graph("configs/graphs/b0_direct.yaml")
    graph.edges.append(
        graph.edges[0].model_copy(
            update={
                "edge_id": "cycle",
                "source_node": "freeze",
                "source_output": "final_code",
                "destination_node": "direct_coder",
                "destination_input": "problem",
            }
        )
    )
    # The edge is also schema-incompatible, and must fail clearly before execution.
    with pytest.raises(GraphCompilationError):
        build_compiler("configs/contracts").compile(graph)


def test_unknown_contract_is_rejected():
    graph = load_graph("configs/graphs/b0_direct.yaml")
    graph.nodes[0] = graph.nodes[0].model_copy(update={"contract_id": "missing"})
    with pytest.raises(GraphCompilationError, match="Missing contract"):
        build_compiler("configs/contracts").compile(graph)
