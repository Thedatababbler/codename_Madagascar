import argparse
import json

from orchestra.ir.compiler import GraphCompiler
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.settings import load_env_file


def build_compiler(contracts_dir: str) -> GraphCompiler:
    backend_ids = {"structured_llm"}
    try:
        import smolagents  # noqa: F401

        backend_ids.add("smolagents_code")
    except ImportError:
        pass
    return GraphCompiler(
        contracts=load_contracts(contracts_dir),
        harness_ids={"public_code_harness"},
        transform_ids={
            "identity_code",
            "merge_analysis_artifacts",
            "repair_to_code",
            "freeze_code",
        },
        selector_ids={"deterministic_code_selector"},
        backend_ids=backend_ids,
    )


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph", required=True)
    parser.add_argument("--contracts-dir", default="configs/contracts")
    args = parser.parse_args()
    compiled = build_compiler(args.contracts_dir).compile(load_graph(args.graph))
    print(
        json.dumps(
            {
                "graph_id": compiled.graph.graph_id,
                "node_count": len(compiled.graph.nodes),
                "edge_count": len(compiled.graph.edges),
                "entry_nodes": compiled.entry_nodes,
                "parallel_waves": compiled.waves,
                "conditional_branches": compiled.conditional_branches,
                "reachable_final_output": bool(compiled.final_producers),
                "schema_validation": "passed",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
