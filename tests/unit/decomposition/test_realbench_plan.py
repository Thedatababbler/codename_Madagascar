"""Tests for RealBench dynamic TaskPlan + public harness binding."""

from __future__ import annotations

import json
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


def _runner_dirs(tmp_path: Path) -> tuple[Path, Path]:
    generated_root = tmp_path / "run" / "generated"
    harness_dir = tmp_path / "run" / "harness"
    generated_root.mkdir(parents=True, exist_ok=True)
    harness_dir.mkdir(parents=True, exist_ok=True)
    return generated_root, harness_dir


def _workspace_entries(ws: Path) -> set[str]:
    return {
        str(p.relative_to(ws))
        for p in ws.rglob("*")
        if ".git" not in p.parts and "__pycache__" not in p.parts
    }


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


def test_public_harness_assets_stay_outside_workspace(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    before = _workspace_entries(ws)
    _, harness_dir = _runner_dirs(tmp_path)
    manifest = materialize_public_harness(ws, harness_dir=harness_dir)

    assert _workspace_entries(ws) == before
    assert Path(manifest.script_path).is_file()
    assert Path(manifest.manifest_path).is_file()
    assert "demo_pkg" in manifest.top_level_packages

    import subprocess

    proc = subprocess.run(
        [*manifest.command[:-1], "discovery"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_missing_third_party_dependency_does_not_fail_the_gate(tmp_path: Path) -> None:
    """The eval env installs project requirements; this harness env may not."""
    ws = _mini_workspace(tmp_path)
    (ws / "demo_pkg" / "core.py").write_text(
        "import definitely_not_installed_pkg\n", encoding="utf-8"
    )
    _, harness_dir = _runner_dirs(tmp_path)
    manifest = materialize_public_harness(ws, harness_dir=harness_dir)

    import subprocess

    proc = subprocess.run(
        [*manifest.command[:-1], "implementation"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "definitely_not_installed_pkg" in proc.stdout

    # A broken repository module is still a failure.
    (ws / "demo_pkg" / "core.py").write_text("import demo_pkg.absent\n", encoding="utf-8")
    proc = subprocess.run(
        [*manifest.command[:-1], "implementation"],
        cwd=ws,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1


def test_adaptive_plan_skips_split_for_simple_repo(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    generated_root, harness_dir = _runner_dirs(tmp_path)
    materialize_public_harness(ws, harness_dir=harness_dir)
    before = _workspace_entries(ws)
    payload = build_realbench_candidate_plan(
        task_id="demo_task",
        workspace=ws,
        generated_root=generated_root,
        harness_dir=harness_dir,
    )
    assert payload["metadata"]["milestone_split"] is False
    assert len(payload["subtasks"]) == 1
    sole = payload["subtasks"][0]
    assert sole["subtask_id"] == "implement_repository"
    assert sole["metadata"]["role"] == "integration"
    # Planning writes nothing into the agent workspace.
    assert _workspace_entries(ws) == before
    assert str(generated_root) in sole["local_graph_template"]

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
        objective=sole["objective"],
        candidate_plan=payload,
    )
    assert plan.decomposition_status.value == "ok"
    validate_task_plan(
        plan,
        require_graph_files=True,
        require_public_keystone_harness=True,
    )
    assert validate_subtask_graph_harness(plan.subtasks[0], require_public_keystone=True) == []


def test_materialized_role_graph_uses_absolute_harness(tmp_path: Path) -> None:
    from orchestra.ir.graph import load_graph
    from orchestra.ir.nodes import HarnessNodeSpec

    ws = _mini_workspace(tmp_path)
    generated_root, harness_dir = _runner_dirs(tmp_path)
    materialize_public_harness(ws, harness_dir=harness_dir)
    payload = build_realbench_candidate_plan(
        task_id="demo_task",
        workspace=ws,
        generated_root=generated_root,
        harness_dir=harness_dir,
    )
    graph = load_graph(payload["subtasks"][0]["local_graph_template"])
    harness = next(n for n in graph.nodes if isinstance(n, HarnessNodeSpec))
    command = list(harness.command or [])
    assert all(not part.startswith("scripts/") for part in command)
    assert str(harness_dir) in " ".join(command)
    assert "--contracts" in command
    assert "--spec-tests" in command


def test_force_split_keeps_multi_milestone(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    generated_root, harness_dir = _runner_dirs(tmp_path)
    materialize_public_harness(ws, harness_dir=harness_dir)
    payload = build_realbench_candidate_plan(
        task_id="demo_task",
        workspace=ws,
        generated_root=generated_root,
        harness_dir=harness_dir,
        force_split=True,
    )
    assert payload["metadata"]["milestone_split"] is True
    assert payload["subtasks"][0]["subtask_id"] == "map_public_api"
    assert payload["subtasks"][-1]["subtask_id"] == "public_contract_harden"
    assert len(payload["subtasks"]) >= 3


def test_should_split_on_complex_tree(tmp_path: Path) -> None:
    ws = _complex_workspace(tmp_path)
    generated_root, harness_dir = _runner_dirs(tmp_path)
    payload = build_realbench_candidate_plan(
        task_id="big",
        workspace=ws,
        generated_root=generated_root,
        harness_dir=harness_dir,
    )
    assert should_split_milestones(
        modules=["a"] * 20,
        packages=["pkg"],
        exports={f"m{i}": ["X"] for i in range(30)},
    )
    assert payload["metadata"]["milestone_split"] is True
    assert len(payload["subtasks"]) >= 3


def test_milestone_memory_lives_in_run_dir(tmp_path: Path) -> None:
    memory_dir = tmp_path / "run" / "adamas_memory"
    append_changelog_entry(
        memory_dir,
        subtask_id="implement_repository",
        role="integration",
        changed_files=["demo_pkg/core.py"],
        revision="abc123",
        summary="implemented Widget surface",
    )
    text = read_changelog(memory_dir)
    assert "implement_repository" in text
    assert "demo_pkg/core.py" in text
    brief = memory_brief_for_milestone(memory_dir)
    assert "implement_repository" in brief
    assert "Shared milestone memory" in brief


def test_milestone_contracts_materialize(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    _, harness_dir = _runner_dirs(tmp_path)
    before = _workspace_entries(ws)
    contracts, path = materialize_milestone_contracts(
        ws, harness_dir=harness_dir, role="integration", milestone_id="m1", enable_llm=False
    )
    assert contracts["generator"] == "deterministic"
    assert path.is_file()
    assert path.parent == harness_dir
    assert _workspace_entries(ws) == before
    det = build_deterministic_contracts(ws, role="discovery")
    assert any(c["type"] == "module_file_exists" for c in det["checks"])


def test_single_file_module_is_not_forced_into_a_package(tmp_path: Path) -> None:
    ws = tmp_path / "flat"
    public = ws / "public_design"
    public.mkdir(parents=True)
    (public / "tree.txt").write_text(
        "proj_clean/\n├── SnoopR.py\n└── helper.py\n", encoding="utf-8"
    )
    (public / "package.json").write_text("{}", encoding="utf-8")
    contracts = build_deterministic_contracts(ws, role="discovery")
    paths = {c["path"] for c in contracts["checks"] if c["type"] == "module_file_exists"}
    assert paths == {"SnoopR.py", "helper.py"}


def test_ambiguous_uml_name_requires_the_symbol_in_only_one_module(
    tmp_path: Path,
) -> None:
    """Two `abstract.py` files share one UML entry; both must not be required."""
    ws = tmp_path / "dup"
    public = ws / "public_design"
    public.mkdir(parents=True)
    (public / "tree.txt").write_text(
        "proj_clean/\n"
        "└── nodeflow/\n"
        "    ├── __init__.py\n"
        "    ├── adapter/\n"
        "    │   └── abstract.py\n"
        "    └── node/\n"
        "        └── abstract.py\n",
        encoding="utf-8",
    )
    (public / "package.json").write_text(
        '{"packageDiagram":{"packages":[{"name":"abstract","exports":["Node"]}]}}',
        encoding="utf-8",
    )
    for pkg in ("adapter", "node"):
        (ws / "nodeflow" / pkg).mkdir(parents=True)
        (ws / "nodeflow" / pkg / "__init__.py").write_text("", encoding="utf-8")
        (ws / "nodeflow" / pkg / "abstract.py").write_text("", encoding="utf-8")
    (ws / "nodeflow" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "nodeflow" / "node" / "abstract.py").write_text(
        "class Node:\n    pass\n", encoding="utf-8"
    )

    contracts = build_deterministic_contracts(ws, role="integration")
    any_checks = [c for c in contracts["checks"] if c["type"] == "export_any"]
    assert any_checks and set(any_checks[0]["modules"]) == {
        "nodeflow.adapter.abstract",
        "nodeflow.node.abstract",
    }

    _, harness_dir = _runner_dirs(tmp_path)
    manifest = materialize_public_harness(ws, harness_dir=harness_dir)
    contracts_path = harness_dir / "dup.contracts.json"
    contracts_path.write_text(json.dumps(contracts), encoding="utf-8")

    import subprocess

    proc = subprocess.run(
        [
            *manifest.command[:-1],
            "integration",
            "--contracts",
            str(contracts_path),
        ],
        cwd=ws,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    # Removing the only host is still a failure.
    (ws / "nodeflow" / "node" / "abstract.py").write_text("", encoding="utf-8")
    proc = subprocess.run(
        [*manifest.command[:-1], "integration", "--contracts", str(contracts_path)],
        cwd=ws,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1


def test_milestone_contracts_merge_planner_acceptance(tmp_path: Path) -> None:
    ws = _mini_workspace(tmp_path)
    _, harness_dir = _runner_dirs(tmp_path)
    baseline, _ = materialize_milestone_contracts(
        ws, harness_dir=harness_dir, role="implementation", milestone_id="m1", enable_llm=False
    )

    contracts, _ = materialize_milestone_contracts(
        ws,
        harness_dir=harness_dir,
        role="implementation",
        milestone_id="m1",
        enable_llm=False,
        extra_checks=[
            {
                "type": "callable_or_class",
                "module": "demo_pkg.core",
                "symbol": "Widget",
                "required_levels": ["implementation"],
            },
            # Duplicated baseline check and an unsafe type both get dropped.
            {"type": "import", "module": "demo_pkg.core", "required_levels": ["implementation"]},
            {"type": "shell", "command": "rm -rf /"},
        ],
        acceptance_criteria=["Widget round-trips its payload"],
        corner_cases=["empty payload raises ValueError"],
    )

    assert contracts["generator"] == "deterministic+planner"
    assert contracts["acceptance_criteria"] == ["Widget round-trips its payload"]
    assert contracts["corner_cases"] == ["empty payload raises ValueError"]
    # The baseline already pins Widget at integration level; the planner widens
    # the same check instead of appending a duplicate, and unsafe types vanish.
    widget = [
        c
        for c in contracts["checks"]
        if c["type"] == "callable_or_class" and c.get("symbol") == "Widget"
    ]
    assert widget == [
        {
            "type": "callable_or_class",
            "module": "demo_pkg.core",
            "symbol": "Widget",
            "required_levels": ["implementation", "integration"],
        }
    ]
    assert not any(c["type"] == "shell" for c in contracts["checks"])
    assert len(contracts["checks"]) == len(baseline["checks"])
