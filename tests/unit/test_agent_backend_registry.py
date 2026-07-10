import logging

from orchestra.backends.factory import build_structured_llm_registry
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.backends.structured_llm import StructuredLLMBackend
from orchestra.cli.validate_graph import build_compiler
from orchestra.ir.compiler import GraphCompilationError, GraphCompiler
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import StructuredLLMBackendConfig
from orchestra.llm.mock_async import MockAsyncLLMClient


def test_legacy_graphs_default_to_structured_llm_and_keep_hash(caplog):
    import orchestra.ir.graph as graph_module

    graph_module._WARNED_LEGACY_BACKEND_NODES.clear()
    expected = {
        "b0_direct": "3dfdeb18bae7df96af3e70a68960921b05c2e3c94d0af9d17f6d93e5991b8ae5",
        "b1_single_harness": "b1341b1ec3e4e32ee1e40e409a08fc8bc27ccb83230fd87e0d50d0406bc34613",
        "b2_fixed_mas": "c7dbe5b24f995d60f5a4ea4401fcccdb5bd687c364a71d26dc5ad668dc1334d4",
    }
    with caplog.at_level(logging.WARNING):
        for name, digest in expected.items():
            graph = load_graph(f"configs/graphs/{name}.yaml")
            assert graph.content_hash == digest
            for node in graph.nodes:
                if node.node_kind.value == "agent":
                    assert isinstance(
                        node.resolved_backend(), StructuredLLMBackendConfig
                    )
                    assert node.resolved_backend().type == "structured_llm"
    assert any(
        "defaulting to structured_llm" in record.message for record in caplog.records
    )


def test_backend_registry_rejects_unknown_backend():
    registry = AgentBackendRegistry()
    registry.register(StructuredLLMBackend(MockAsyncLLMClient({})))
    assert registry.has("structured_llm")
    try:
        registry.get("smolagents_code")
        raise AssertionError("expected KeyError")
    except KeyError as exc:
        assert "smolagents_code" in str(exc)


def test_compiler_rejects_unknown_backend():
    contracts = load_contracts("configs/contracts")
    compiler = GraphCompiler(
        contracts=contracts,
        harness_ids={"public_code_harness"},
        transform_ids={
            "identity_code",
            "merge_analysis_artifacts",
            "repair_to_code",
            "freeze_code",
        },
        selector_ids={"deterministic_code_selector"},
        backend_ids=set(),
    )
    graph = load_graph("configs/graphs/b0_direct.yaml")
    try:
        compiler.compile(graph)
        raise AssertionError("expected GraphCompilationError")
    except GraphCompilationError as exc:
        assert "Unknown agent backend" in str(exc)


def test_default_registry_contains_structured_llm_only():
    registry = build_structured_llm_registry(MockAsyncLLMClient({}))
    assert registry.ids() == ["structured_llm"]


def test_existing_graphs_still_compile():
    compiler = build_compiler("configs/contracts")
    for name in ("b0_direct", "b1_single_harness", "b2_fixed_mas"):
        compiled = compiler.compile(load_graph(f"configs/graphs/{name}.yaml"))
        assert compiled.final_producers
