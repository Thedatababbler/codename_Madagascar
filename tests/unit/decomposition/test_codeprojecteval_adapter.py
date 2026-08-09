"""CodeProjectEval adapter: workspace isolation and gate semantics."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestra.codeprojecteval import (
    build_agent_workspace,
    check_command,
    cpe_brief,
    load_task,
    materialize_check_harness,
)
from orchestra.codeprojecteval.harness import (
    build_deterministic_contracts,
    contracts_path_for,
    expected_modules,
)


def _dataset(tmp_path: Path) -> Path:
    """A miniature repository shaped like a CodeProjectEval entry."""
    root = tmp_path / "dataset" / "demo"
    (root / "docs").mkdir(parents=True)
    (root / "demo_pkg").mkdir()
    (root / "check_tests").mkdir()
    (root / "unit_tests").mkdir()

    (root / "config.json").write_text(
        json.dumps(
            {
                "PRD": "docs/PRD.md",
                "UML": ["docs/UML.md"],
                "dependencies": "requirements.txt",
                "architecture_design": "docs/architecture_design.md",
                "language": "python",
                "source_code": "demo_pkg",
                "unit_tests": "unit_tests",
                "check_tests": "check_tests",
                "required_files": ["requirements.txt"],
            }
        ),
        encoding="utf-8",
    )
    (root / "docs" / "PRD.md").write_text("build a widget store", encoding="utf-8")
    (root / "docs" / "UML.md").write_text("classDiagram", encoding="utf-8")
    (root / "docs" / "architecture_design.md").write_text("layers", encoding="utf-8")
    (root / "docs" / "directory_tree.txt").write_text(
        "├── demo_pkg\n│   ├── __init__.py\n│   └── core.py\n", encoding="utf-8"
    )
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (root / "demo_pkg" / "__init__.py").write_text(
        "from demo_pkg.core import Widget\n", encoding="utf-8"
    )
    (root / "demo_pkg" / "core.py").write_text(
        "class Widget:\n    value = 1\n", encoding="utf-8"
    )
    (root / "check_tests" / "test_widget.py").write_text(
        "from demo_pkg import Widget\n\n\ndef test_value():\n    assert Widget.value == 1\n",
        encoding="utf-8",
    )
    (root / "unit_tests" / "test_hidden.py").write_text(
        "def test_secret():\n    assert False\n", encoding="utf-8"
    )
    return root.parent


def test_workspace_withholds_reference_code_and_scoring_tests(tmp_path: Path) -> None:
    task = load_task("demo", dataset_root=_dataset(tmp_path))
    ws = build_agent_workspace(task, tmp_path / "ws")

    entries = {p.name for p in ws.iterdir()}
    assert {"docs", "check_tests", "requirements.txt"} <= entries
    assert not (ws / "demo_pkg").exists(), "reference implementation leaked"
    assert not (ws / "unit_tests").exists(), "scoring suite leaked"
    assert (ws / "docs" / "PRD.md").is_file()
    assert (ws / ".git").is_dir()


def test_gate_fails_empty_repo_and_passes_reference(tmp_path: Path) -> None:
    dataset_root = _dataset(tmp_path)
    task = load_task("demo", dataset_root=dataset_root)
    ws = build_agent_workspace(task, tmp_path / "ws")
    harness_dir = tmp_path / "harness"
    manifest = materialize_check_harness(
        task, harness_dir=harness_dir, env_python=Path(sys.executable)
    )
    assert manifest.expected_modules == ["demo_pkg", "demo_pkg.core"]

    contracts = build_deterministic_contracts(task, role="integration", milestone_id="m1")
    contracts_path = contracts_path_for(harness_dir, "m1")
    contracts_path.write_text(json.dumps(contracts), encoding="utf-8")
    command = check_command(
        harness_dir=harness_dir,
        level="integration",
        env_python=Path(sys.executable),
        contracts_path=contracts_path,
    )

    empty = subprocess.run(command, cwd=ws, capture_output=True, text=True, check=False)
    assert empty.returncode == 1
    assert "packages exist" in empty.stderr

    subprocess.run(
        ["cp", "-r", str(task.repo_root / "demo_pkg"), str(ws / "demo_pkg")], check=True
    )
    done = subprocess.run(command, cwd=ws, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "OK check_tests" in done.stdout


def test_integration_gate_fails_when_visible_tests_fail(tmp_path: Path) -> None:
    dataset_root = _dataset(tmp_path)
    task = load_task("demo", dataset_root=dataset_root)
    ws = build_agent_workspace(task, tmp_path / "ws")
    (ws / "demo_pkg").mkdir()
    (ws / "demo_pkg" / "__init__.py").write_text(
        "from demo_pkg.core import Widget\n", encoding="utf-8"
    )
    (ws / "demo_pkg" / "core.py").write_text(
        "class Widget:\n    value = 2\n", encoding="utf-8"
    )
    harness_dir = tmp_path / "harness"
    materialize_check_harness(
        task, harness_dir=harness_dir, env_python=Path(sys.executable)
    )

    # Imports resolve, so implementation passes while integration must not.
    for level, expected in (("implementation", 0), ("integration", 1)):
        proc = subprocess.run(
            check_command(
                harness_dir=harness_dir, level=level, env_python=Path(sys.executable)
            ),
            cwd=ws,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == expected, f"{level}: {proc.stdout}{proc.stderr}"


def test_rewriting_the_visible_suite_fails_the_gate(tmp_path: Path) -> None:
    """A milestone must not pass by weakening the tests that judge it."""
    task = load_task("demo", dataset_root=_dataset(tmp_path))
    ws = build_agent_workspace(task, tmp_path / "ws")
    subprocess.run(
        ["cp", "-r", str(task.repo_root / "demo_pkg"), str(ws / "demo_pkg")], check=True
    )
    harness_dir = tmp_path / "harness"
    manifest = materialize_check_harness(
        task, harness_dir=harness_dir, env_python=Path(sys.executable)
    )
    assert manifest.check_tests_digest, "visible suite must be pinned"
    command = check_command(
        harness_dir=harness_dir, level="integration", env_python=Path(sys.executable)
    )
    assert (
        subprocess.run(command, cwd=ws, capture_output=True, text=True, check=False)
    ).returncode == 0

    (ws / "check_tests" / "test_widget.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    weakened = subprocess.run(
        command, cwd=ws, capture_output=True, text=True, check=False
    )
    assert weakened.returncode == 1
    assert "altered" in weakened.stderr

    (ws / "check_tests" / "test_widget.py").unlink()
    deleted = subprocess.run(
        command, cwd=ws, capture_output=True, text=True, check=False
    )
    assert deleted.returncode == 1
    assert "deleted" in deleted.stderr


def test_planning_brief_uses_design_docs_and_flags_hidden_suite(tmp_path: Path) -> None:
    task = load_task("demo", dataset_root=_dataset(tmp_path))
    brief = cpe_brief(task)

    titles = [title for title, _ in brief.documents]
    assert "PRD.md" in titles and "directory_tree.txt" in titles
    assert brief.modules == ["demo_pkg", "demo_pkg.core"]
    assert "held-out" in brief.acceptance_note

    body = "\n".join(text for _, text in brief.documents)
    assert "test_secret" not in body, "scoring suite must not reach the planner"


def test_module_paths_survive_tree_quirks(tmp_path: Path) -> None:
    """`tree` output indents with NBSP and may be rooted at the distribution name."""
    dataset_root = _dataset(tmp_path)
    tree = dataset_root / "demo" / "docs" / "directory_tree.txt"
    tree.write_text(
        "├── demo-project\n│\xa0\xa0 ├── __init__.py\n│\xa0\xa0 └── core.py\n",
        encoding="utf-8",
    )

    task = load_task("demo", dataset_root=dataset_root)
    assert expected_modules(task) == ["demo_pkg", "demo_pkg.core"]


@pytest.mark.parametrize("role", ["discovery", "implementation", "integration"])
def test_check_command_is_absolute_and_pins_the_repo_interpreter(
    tmp_path: Path, role: str
) -> None:
    task = load_task("demo", dataset_root=_dataset(tmp_path))
    harness_dir = tmp_path / "harness"
    materialize_check_harness(
        task, harness_dir=harness_dir, env_python=Path(sys.executable)
    )
    command = check_command(
        harness_dir=harness_dir, level=role, env_python=Path(sys.executable)
    )

    assert command[0] == sys.executable
    assert all(Path(part).is_absolute() for part in command[1:2])
    assert "--root" not in command
