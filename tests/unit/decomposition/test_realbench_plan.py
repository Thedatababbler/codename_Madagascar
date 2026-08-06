"""Tests for RealBench dynamic TaskPlan + public harness binding."""

from __future__ import annotations

from pathlib import Path

from orchestra.decomposition.decomposer import TaskDecomposer
from orchestra.decomposition.realbench_plan import (
    build_realbench_candidate_plan,
    should_split_milestones,
)
from orchestra.decomposition.schemas import DecompositionLimits
from orchestra.decomposition.validator import (
    validate_subtask_graph_harness,
    validate_task_plan,
)
from orchestra.realbench.milestone_contracts import (
    build_deterministic_contracts,
    materialize_milestone_contracts,
)
from orchestra.realbench.public_harness import (
    materialize_public_harness,
    parse_expected_modules,
)
from orchestra.realbench.workspace_memory import (
    CHANGELOG_NAME,
    append_changelog_entry,
    memory_brief_for_milestone,
    read_changelog,
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


def _complex_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "complex"
    public = ws / "public_design"
    public.mkdir(parents=True)
    # Build a tree large enough to trigger adaptive split.
    body = "proj_clean/\n└── bigpkg/\n"
    body += "    ├── __init__.py\n"
    for i in range(10):
        body += f"    ├── mod{i}.py\n"
        body += f"    └── grp{i}/\n"
        body += "        ├── __init__.py\n"
        body += "        ├── sub0.py\n"
        body += "        └── sub1.py\n"
    (public / "tree.txt").write_text(body, encoding="utf-8")
    exports = [{"name": f"mod{i}", "type": "package", "exports": [f"Api{i}"]} for i in range(10)]
    import json

    (public / "package.json").write_text(
        json.dumps({"packageDiagram": {"packages": exports}}), encoding="utf-8"
    )
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
    assert (ws / "adamas_milestone_contracts.json").is_file()
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


def test_adaptive_plan_skips_split_for_simple_repo(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    materialize_public_harness(ws)
    payload = build_realbench_candidate_plan(task_id="demo_task", workspace=ws)
    assert payload["metadata"]["milestone_split"] is False
    assert len(payload["subtasks"]) == 1
    assert payload["subtasks"][0]["subtask_id"] == "implement_repository"
    assert payload["subtasks"][0]["metadata"]["role"] == "integration"
    assert CHANGELOG_NAME in payload["subtasks"][0]["metadata"]["milestone_brief"]

    decomposer = TaskDecomposer(
        enabled=True,
        default_graph_template="configs/graphs/codex_realbench_public_integration.yaml",
        keystone_harness_id="repository_test_harness",
        limits=DecompositionLimits(min_subtasks=1, max_subtasks=6),
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
    assert validate_subtask_graph_harness(plan.subtasks[0], require_public_keystone=True) == []


def test_force_split_keeps_multi_milestone(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    materialize_public_harness(ws)
    payload = build_realbench_candidate_plan(
        task_id="demo_task", workspace=ws, force_split=True
    )
    assert payload["metadata"]["milestone_split"] is True
    assert payload["subtasks"][0]["subtask_id"] == "map_public_api"
    assert payload["subtasks"][-1]["subtask_id"] == "public_contract_harden"
    assert len(payload["subtasks"]) >= 3


def test_should_split_on_complex_tree(tmp_path: Path) -> None:
    ws = _complex_workspace(tmp_path)
    payload = build_realbench_candidate_plan(task_id="big", workspace=ws)
    assert should_split_milestones(
        modules=["a"] * 20,
        packages=["pkg"],
        exports={f"m{i}": ["X"] for i in range(30)},
    )
    assert payload["metadata"]["milestone_split"] is True
    assert len(payload["subtasks"]) >= 3


def test_workspace_changelog_memory(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    import subprocess

    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.email", "t@local"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(ws), "commit", "-m", "base", "--allow-empty"],
        check=True,
        capture_output=True,
    )
    append_changelog_entry(
        ws,
        subtask_id="implement_repository",
        role="integration",
        changed_files=["demo_pkg/core.py", "NOTES.md"],
        revision="abc123",
        summary="implemented Widget surface",
        git_commit=True,
    )
    text = read_changelog(ws)
    assert "implement_repository" in text
    assert "demo_pkg/core.py" in text
    brief = memory_brief_for_milestone(ws)
    assert CHANGELOG_NAME in brief
    assert "implement_repository" in brief


def test_milestone_contracts_materialize(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    contracts = materialize_milestone_contracts(
        ws, role="integration", milestone_id="m1", enable_llm=False
    )
    assert contracts["generator"] == "deterministic"
    assert (ws / "adamas_milestone_contracts.json").is_file()
    assert (ws / "tests_public" / "test_milestone_contracts.py").is_file()
    det = build_deterministic_contracts(ws, role="discovery")
    assert any(c["type"] == "module_file_exists" for c in det["checks"])
