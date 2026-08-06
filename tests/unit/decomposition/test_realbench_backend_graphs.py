"""Dynamic plan graph selection for Codex vs smolagents RealBench backends."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestra.cli.run_realbench_codex_decomp_baseline import resolve_graph_catalog
from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.realbench_plan import (
    CODEX_GRAPH_CATALOG,
    SMOLAGENTS_GRAPH_CATALOG,
    build_realbench_candidate_plan,
    graph_catalog_for_backend,
)
from orchestra.decomposition.schemas import DecompositionLimits
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.realbench.public_harness import materialize_public_harness


def _mini_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    public = ws / "public_design"
    public.mkdir(parents=True)
    (public / "tree.txt").write_text(
        "proj_clean/\n└── demo_pkg/\n    ├── __init__.py\n    └── core.py\n",
        encoding="utf-8",
    )
    (public / "package.json").write_text(
        '{"packageDiagram":{"packages":[{"name":"core","type":"package","exports":["W"]}]}}',
        encoding="utf-8",
    )
    (ws / "demo_pkg").mkdir()
    (ws / "demo_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "demo_pkg" / "core.py").write_text("", encoding="utf-8")
    materialize_public_harness(ws)
    return ws


@pytest.mark.parametrize(
    ("role", "expected_id"),
    [
        ("discovery", "smolagents_realbench_public_discovery"),
        ("implementation", "smolagents_realbench_public_implementation"),
        ("integration", "smolagents_realbench_public_integration"),
    ],
)
def test_smolagents_catalog_graph_ids(role: str, expected_id: str) -> None:
    path = SMOLAGENTS_GRAPH_CATALOG[role]  # type: ignore[index]
    graph = load_graph(path)
    assert graph.graph_id == expected_id
    agents = [n for n in graph.nodes if isinstance(n, AgentNodeSpec)]
    assert agents
    assert all(n.resolved_backend().type == "smolagents_code" for n in agents)


@pytest.mark.parametrize(
    ("role", "expected_id"),
    [
        ("discovery", "codex_realbench_public_discovery"),
        ("implementation", "codex_realbench_public_implementation"),
        ("integration", "codex_realbench_public_integration"),
    ],
)
def test_codex_catalog_graph_ids_unchanged(role: str, expected_id: str) -> None:
    path = CODEX_GRAPH_CATALOG[role]  # type: ignore[index]
    graph = load_graph(path)
    assert graph.graph_id == expected_id
    agents = [n for n in graph.nodes if isinstance(n, AgentNodeSpec)]
    assert all(n.resolved_backend().type == "codex_sdk" for n in agents)


def test_smolagents_dynamic_plan_selects_smolagents_graphs(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    catalog = graph_catalog_for_backend("smolagents_code")
    payload = build_realbench_candidate_plan(
        task_id="demo", workspace=ws, graph_catalog=catalog
    )
    roles = {s["metadata"]["role"]: s["local_graph_template"] for s in payload["subtasks"]}
    assert "smolagents_realbench_public_discovery" in roles["discovery"]
    assert "smolagents_realbench_public_implementation" in roles["implementation"]
    assert "smolagents_realbench_public_integration" in roles["integration"]
    assert all("codex_realbench" not in p for p in roles.values())

    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template=catalog["integration"],
        keystone_harness_id="repository_test_harness",
        limits=DecompositionLimits(min_subtasks=2, max_subtasks=6),
        require_graph_files=True,
        require_public_keystone_harness=True,
    )
    plan = decomposer.decompose(
        task_id=payload["task_id"],
        objective=payload["subtasks"][0]["objective"],
        candidate_plan=payload,
    )
    assert plan.decomposition_status.value == "ok"
    for sub in plan.subtasks:
        assert "smolagents_realbench" in sub.local_graph_template
        assert "codex_realbench" not in sub.local_graph_template


def test_codex_dynamic_plan_still_selects_codex_graphs(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    catalog = graph_catalog_for_backend("codex_sdk")
    payload = build_realbench_candidate_plan(
        task_id="demo", workspace=ws, graph_catalog=catalog
    )
    for sub in payload["subtasks"]:
        assert "codex_realbench" in sub["local_graph_template"]
        assert "smolagents_realbench" not in sub["local_graph_template"]


def test_resolve_graph_catalog_fail_closed_on_mismatch() -> None:
    with pytest.raises(SystemExit, match="refuses Codex graph"):
        resolve_graph_catalog(
            {
                "decomposition": {
                    "graph_catalog": dict(CODEX_GRAPH_CATALOG),
                }
            },
            agent_backend="smolagents_code",
        )
    with pytest.raises(SystemExit, match="refuses smolagents graph"):
        resolve_graph_catalog(
            {
                "decomposition": {
                    "graph_catalog": dict(SMOLAGENTS_GRAPH_CATALOG),
                }
            },
            agent_backend="codex_sdk",
        )


def test_graph_catalog_for_backend_unknown() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        graph_catalog_for_backend("structured_llm")
