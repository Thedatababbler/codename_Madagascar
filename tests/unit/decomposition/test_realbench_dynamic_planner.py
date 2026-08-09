"""Risk-first dynamic milestone planning + generated milestone subgraphs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from orchestra.cli.validate_graph import build_compiler
from orchestra.decomposition.realbench_plan import build_plan_from_draft
from orchestra.decomposition.schemas import DecompositionLimits, TaskPlan
from orchestra.decomposition.validator import validate_task_plan
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.realbench.milestone_planner import (
    MilestonePlanError,
    build_planner_prompt,
    parse_plan_payload,
    plan_milestones,
    planner_enabled,
    sanitize_contract_checks,
)
from orchestra.realbench.subgraph_builder import (
    generated_contracts_dir,
    materialize_milestone_subgraph,
    prepare_generated_root,
)

CONTRACTS = "configs/contracts"


def _harness_command(tmp_path: Path, level: str = "implementation") -> list[str]:
    return [
        "python",
        str(tmp_path / "harness" / "adamas_public_check.py"),
        "--manifest",
        str(tmp_path / "harness" / "adamas_public_harness.json"),
        "--level",
        level,
    ]


def _risk_payload() -> dict:
    return {
        "rationale": "CRSIndex is imported by every accessor module.",
        "milestones": [
            {
                "milestone_id": "freeze-crs-index",
                "title": "Freeze CRS index contract",
                "objective": "Implement CRSIndex so accessors can rely on it.",
                "risk_rationale": (
                    "Every accessor imports CRSIndex; a wrong base class breaks all "
                    "downstream modules."
                ),
                "role": "implementation",
                "focus_paths": ["proj_clean/xproj/index.py"],
                "acceptance": {
                    "criteria": ["CRSIndex behaves as an xarray Index"],
                    "corner_cases": ["non-scalar coordinate raises ValueError"],
                    "checks": [
                        {
                            "type": "callable_or_class",
                            "module": "xproj.index",
                            "symbol": "CRSIndex",
                            "required_levels": ["implementation", "integration"],
                        },
                        {"type": "shell", "command": "rm -rf /"},
                        {"type": "module_file_exists", "path": "../escape.py"},
                    ],
                },
                "agents": [
                    {
                        "role_id": "index_owner",
                        "title": "Index contract owner",
                        "mandate": "Implement CRSIndex and its invariants.",
                        "max_tokens": 999_999,
                        "max_steps": 400,
                        "timeout_seconds": 900,
                    }
                ],
            },
            {
                "milestone_id": "implement-accessors",
                "title": "Implement accessors",
                "objective": "Implement the accessor surface on top of CRSIndex.",
                "risk_rationale": "",
                "role": "integration",
                "depends_on": ["freeze-crs-index"],
                "agents": [
                    {
                        "role_id": "accessor_dev",
                        "title": "Accessor implementer",
                        "mandate": "Implement accessors without redesigning CRSIndex.",
                    }
                ],
            },
        ],
    }


def test_parse_plan_payload_keeps_risk_gate_and_sanitizes() -> None:
    draft = parse_plan_payload(_risk_payload())

    assert [m.milestone_id for m in draft.milestones] == [
        "freeze_crs_index",
        "implement_accessors",
    ]
    assert draft.split is True
    gate = draft.milestones[0]
    assert gate.risk_rationale
    # The dataset tree root is stripped: it does not exist in the workspace.
    assert gate.focus_paths == ["xproj/index.py"]
    # Unsafe check types and path escapes never reach the workspace.
    assert [c["type"] for c in gate.acceptance.checks] == ["callable_or_class"]
    assert gate.acceptance.corner_cases == ["non-scalar coordinate raises ValueError"]
    agent = gate.agents[0]
    assert agent.max_tokens == 16384
    assert agent.max_steps == 24
    # Dependencies are rewritten to the normalized ids.
    assert draft.milestones[1].depends_on == ["freeze_crs_index"]


def test_parse_plan_payload_collapses_split_without_risk() -> None:
    payload = {
        "rationale": "split by package",
        "milestones": [
            {
                "milestone_id": "implement_pkg_a",
                "objective": "Implement package a.",
                "role": "implementation",
                "agents": [{"role_id": "dev", "mandate": "write package a"}],
            },
            {
                "milestone_id": "implement_pkg_b",
                "objective": "Implement package b and finish the repo.",
                "role": "integration",
                "agents": [{"role_id": "dev", "mandate": "write package b"}],
            },
        ],
    }

    draft = parse_plan_payload(payload)

    assert draft.split is False
    assert [m.milestone_id for m in draft.milestones] == ["implement_pkg_b"]


def test_parse_plan_payload_rejects_unusable_payloads() -> None:
    with pytest.raises(MilestonePlanError):
        parse_plan_payload("no json here")
    with pytest.raises(MilestonePlanError):
        parse_plan_payload({"milestones": []})
    with pytest.raises(MilestonePlanError):
        parse_plan_payload({"milestones": [{"title": "missing objective"}]})


def test_terminal_milestone_is_graded_at_integration() -> None:
    draft = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "build_everything",
                    "objective": "Implement the whole repository.",
                    "role": "implementation",
                    "agents": [{"role_id": "dev", "mandate": "write it"}],
                }
            ]
        }
    )

    # Otherwise the freeze gate never requires UML exports to be importable.
    assert draft.milestones[-1].role == "integration"


def test_parse_plan_payload_defaults_missing_agents() -> None:
    draft = parse_plan_payload(
        {"milestones": [{"milestone_id": "solo", "objective": "Build it all."}]}
    )

    assert len(draft.milestones) == 1
    assert [a.role for a in draft.milestones[0].agents] == ["implementer"]


def test_sanitize_contract_checks_drops_unknown_shapes() -> None:
    checks = sanitize_contract_checks(
        [
            {"type": "import", "module": "pkg.mod"},
            {"type": "import", "module": "not a module"},
            {"type": "export", "module": "pkg.mod"},
            {"type": "eval", "module": "pkg.mod"},
        ]
    )

    assert checks == [
        {"type": "import", "module": "pkg.mod", "required_levels": ["integration"]}
    ]


def test_risk_first_planning_is_the_default(monkeypatch) -> None:
    """Template segmentation is a fallback, so it must never win by default."""
    monkeypatch.delenv("ADAMAS_REALBENCH_DYNAMIC_PLAN", raising=False)
    assert planner_enabled() is True

    monkeypatch.setenv("ADAMAS_REALBENCH_DYNAMIC_PLAN", "1")
    assert planner_enabled() is True

    monkeypatch.setenv("ADAMAS_REALBENCH_DYNAMIC_PLAN", "0")
    assert planner_enabled() is False
    assert planner_enabled(True) is True


def test_planner_returns_none_when_explicitly_disabled(tmp_path: Path) -> None:
    assert (
        plan_milestones(
            task_id="demo",
            workspace=tmp_path,
            agent_backend="codex_sdk",
            enable=False,
        )
        is None
    )


def test_planner_prompt_uses_public_inputs_only(tmp_path: Path) -> None:
    (tmp_path / "TASK.md").write_text("build xproj", encoding="utf-8")
    (tmp_path / "REQUIREMENTS.md").write_text("crs handling", encoding="utf-8")
    design = tmp_path / "public_design"
    design.mkdir()
    (design / "tree.txt").write_text("xproj/\n    index.py\n", encoding="utf-8")

    prompt = build_planner_prompt(
        task_id="benbovy_xproj", workspace=tmp_path, agent_backend="codex_sdk"
    )

    assert "risk gate" in prompt
    assert "Splitting by directory" in prompt
    assert "xproj.index" in prompt
    assert "proj_with_test" not in prompt


@pytest.mark.parametrize(
    ("backend", "expected_backend"),
    [("codex_sdk", "codex_sdk"), ("smolagents_code", "smolagents_code")],
)
def test_generated_milestone_subgraph_compiles(
    tmp_path: Path, backend: str, expected_backend: str
) -> None:
    draft = parse_plan_payload(_risk_payload())
    root = prepare_generated_root(tmp_path, base_contracts_dir=CONTRACTS)

    graph_path, roster = materialize_milestone_subgraph(
        generated_root=root,
        milestone=draft.milestones[0],
        agent_backend=backend,
        harness_command=_harness_command(tmp_path),
        model_name="test-model",
    )

    graph = load_graph(graph_path)
    compiled = build_compiler(str(generated_contracts_dir(root))).compile(graph)
    agents = [n for n in compiled.graph.nodes if isinstance(n, AgentNodeSpec)]

    assert {n.resolved_backend().type for n in agents} == {expected_backend}
    assert graph.metadata["agent_roster"] == roster
    entry = roster[0]
    assert entry["max_tokens"] == 16384
    assert entry["prompt_chars"] > 0
    assert entry["prompt_sha256"]
    contract = yaml.safe_load(Path(entry["contract_path"]).read_text(encoding="utf-8"))
    assert contract["max_tokens"] == 16384
    assert contract["role"] == "Index contract owner"
    system_prompt = contract["system_prompt_template"]
    assert "Every accessor imports CRSIndex" in system_prompt
    # Acceptance context lives in the prompt, never as workspace scaffolding.
    assert "MILESTONE.md" not in system_prompt
    assert "ADAMAS_CHANGELOG.md" not in system_prompt
    assert "scripts/adamas_public_check.py" not in system_prompt


def test_multi_agent_milestone_chains_into_single_gate(tmp_path: Path) -> None:
    payload = _risk_payload()
    payload["milestones"][0]["agents"].append(
        {
            "role_id": "hardener",
            "title": "Contract hardener",
            "mandate": "Cover the corner cases for CRSIndex.",
        }
    )
    draft = parse_plan_payload(payload)
    root = prepare_generated_root(tmp_path, base_contracts_dir=CONTRACTS)

    graph_path, roster = materialize_milestone_subgraph(
        generated_root=root,
        milestone=draft.milestones[0],
        agent_backend="codex_sdk",
        harness_command=_harness_command(tmp_path),
    )
    graph = load_graph(graph_path)
    compiled = build_compiler(str(generated_contracts_dir(root))).compile(graph)

    assert [entry["role_id"] for entry in roster] == ["index_owner", "hardener"]
    agent_ids = [n.node_id for n in compiled.graph.nodes if isinstance(n, AgentNodeSpec)]
    assert len(agent_ids) == 2
    # Only the terminal agent feeds the acceptance harness and the freeze gate.
    into_harness = [
        e.source_node for e in graph.edges if e.destination_node == "repository_tests"
    ]
    assert into_harness == [agent_ids[-1]]
    assert compiled.waves[0] == [agent_ids[0]]


def test_build_plan_from_draft_validates_as_taskplan(tmp_path: Path) -> None:
    draft = parse_plan_payload(_risk_payload())
    root = prepare_generated_root(tmp_path, base_contracts_dir=CONTRACTS)
    workspace = _public_workspace(tmp_path)
    harness_dir = tmp_path / "harness"

    payload = build_plan_from_draft(
        task_id="benbovy_xproj",
        draft=draft,
        workspace=workspace,
        generated_root=root,
        harness_dir=harness_dir,
        agent_backend="codex_sdk",
    )
    plan = TaskPlan.model_validate(payload)
    validate_task_plan(
        plan,
        limits=DecompositionLimits(min_subtasks=1, max_subtasks=6),
        require_graph_files=True,
        require_public_keystone_harness=True,
    )

    gate, terminal = plan.subtasks
    assert terminal.dependencies == [gate.subtask_id]
    assert plan.final_aggregation.terminal_subtask_id == terminal.subtask_id
    assert plan.metadata["decomposition_source"] == "llm"
    acceptance = gate.metadata["acceptance"]
    assert acceptance["checks"][0]["symbol"] == "CRSIndex"
    brief = gate.metadata["milestone_brief"]
    assert "Why this milestone gates the rest" in brief
    assert "Corner cases" in brief
    assert "Agent roster" in brief
    assert gate.budget.max_llm_calls == len(gate.metadata["agent_roster"])
    # Contracts are frozen beside the run; the workspace keeps dataset files only.
    assert (harness_dir / "freeze_crs_index.contracts.json").is_file()
    assert not (workspace / "adamas_milestone_contracts.json").exists()
    assert not (workspace / "MILESTONE.md").exists()


def _public_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    design = workspace / "public_design"
    design.mkdir(parents=True)
    (workspace / "TASK.md").write_text("build xproj", encoding="utf-8")
    (workspace / "REQUIREMENTS.md").write_text("crs handling", encoding="utf-8")
    (design / "tree.txt").write_text(
        "xproj/\n    __init__.py\n    index.py\n", encoding="utf-8"
    )
    return workspace


def test_runner_uses_planner_draft_and_generated_contracts(
    tmp_path: Path, monkeypatch
) -> None:
    from orchestra.cli import run_realbench_codex_decomp_baseline as runner

    draft = parse_plan_payload(_risk_payload())
    monkeypatch.setattr(runner, "plan_milestones", lambda **_: draft)

    plan, contracts_dir = runner.build_dynamic_task_plan(
        "benbovy_xproj",
        workspace=_public_workspace(tmp_path),
        plan_path=tmp_path / "run" / "plan.yaml",
        harness_dir=tmp_path / "run" / "harness",
        experiment={"decomposition": {"min_subtasks": 1, "max_subtasks": 6}},
        agent_backend="codex_sdk",
        contracts_dir=CONTRACTS,
    )

    assert plan.decomposition_status.value == "ok"
    assert [s.subtask_id for s in plan.subtasks] == [
        "freeze_crs_index",
        "implement_accessors",
    ]
    assert plan.metadata["decomposition_source"] == "llm"
    # Generated contracts live beside the run and still carry the base catalog.
    contracts_path = Path(contracts_dir)
    assert contracts_path.is_relative_to(tmp_path)
    names = {p.stem for p in contracts_path.glob("*.yaml")}
    assert "codex_realbench_milestone" in names
    assert any(name.startswith("rbdyn_freeze_crs_index") for name in names)
    assert json.loads(
        (tmp_path / "run" / "milestone_plan_draft.json").read_text(encoding="utf-8")
    )["split"]


def test_runner_falls_back_to_public_design_plan(tmp_path: Path, monkeypatch) -> None:
    from orchestra.cli import run_realbench_codex_decomp_baseline as runner

    monkeypatch.setattr(runner, "plan_milestones", lambda **_: None)
    workspace = _public_workspace(tmp_path)

    plan, contracts_dir = runner.build_dynamic_task_plan(
        "benbovy_xproj",
        workspace=workspace,
        plan_path=tmp_path / "run" / "plan.yaml",
        harness_dir=tmp_path / "run" / "harness",
        experiment={"decomposition": {"min_subtasks": 1}},
        agent_backend="codex_sdk",
        contracts_dir=CONTRACTS,
    )

    assert Path(contracts_dir).is_relative_to(tmp_path)
    assert plan.metadata["decomposition_source"] == "public_design"
    assert [s.subtask_id for s in plan.subtasks] == ["implement_repository"]
    # The fallback plan also runs the runner-owned harness, not a repo script.
    graph_path = plan.subtasks[0].local_graph_template
    assert Path(graph_path).is_relative_to(tmp_path)
    assert not (workspace / "scripts").exists()


def test_plan_draft_json_is_serializable(tmp_path: Path) -> None:
    draft = parse_plan_payload(_risk_payload())

    text = json.dumps(draft.to_dict())

    assert "freeze_crs_index" in text
