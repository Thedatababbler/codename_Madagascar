"""Tests for RealBench dynamic TaskPlan + public harness binding."""

from __future__ import annotations

from pathlib import Path

from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.realbench_plan import build_realbench_candidate_plan
from orchestra.decomposition.schemas import DecompositionLimits
from orchestra.decomposition.validator import (
    validate_subtask_graph_harness,
    validate_task_plan,
)
from orchestra.realbench.public_harness import (
    materialize_public_harness,
    parse_expected_modules,
)


def _mini_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    public = ws / "public_design"
    public.mkdir(parents=True)
    (public / "tree.txt").write_text(
        "proj_clean/\n"
        "└── demo_pkg/\n"
        "    ├── __init__.py\n"
        "    └── core.py\n",
        encoding="utf-8",
    )
    (public / "package.json").write_text(
        """
        {"packageDiagram":{"packages":[
          {"name":"core","type":"package","exports":["Widget"]},
          {"name":"__init__","type":"package","exports":[]}
        ]}}
        """,
        encoding="utf-8",
    )
    (ws / "demo_pkg").mkdir()
    (ws / "demo_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "demo_pkg" / "core.py").write_text("", encoding="utf-8")
    return ws


def test_parse_expected_modules_from_tree() -> None:
    tree = (
        "proj_clean/\n"
        "└── nodeflow/\n"
        "    ├── __init__.py\n"
        "    └── adapter/\n"
        "        └── pipeline.py\n"
    )
    mods = parse_expected_modules(tree)
    assert "nodeflow" in mods
    assert "nodeflow.adapter.pipeline" in mods


def test_materialize_public_harness_and_discovery_passes(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    manifest = materialize_public_harness(ws)
    assert (ws / "scripts" / "adamas_public_check.py").is_file()
    assert (ws / "adamas_public_harness.json").is_file()
    assert "demo_pkg" in manifest.top_level_packages
    import subprocess

    proc = subprocess.run(
        ["python", "scripts/adamas_public_check.py", "--level", "discovery"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_dynamic_plan_binds_public_keystone_and_gates(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    materialize_public_harness(ws)
    payload = build_realbench_candidate_plan(task_id="demo_task", workspace=ws)
    assert payload["subtasks"][0]["subtask_id"] == "map_public_api"
    assert payload["subtasks"][-1]["subtask_id"] == "public_contract_harden"
    assert all(
        s["keystone_harness_id"] == "repository_test_harness"
        for s in payload["subtasks"]
    )
    assert all("milestone_brief" in s["metadata"] for s in payload["subtasks"])

    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template="configs/graphs/codex_realbench_public_integration.yaml",
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
    validate_task_plan(
        plan,
        require_graph_files=True,
        require_public_keystone_harness=True,
    )
    for sub in plan.subtasks:
        assert validate_subtask_graph_harness(sub, require_public_keystone=True) == []
