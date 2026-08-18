"""A held-out suite that never ran is unmeasured, not a repository that failed.

The scoring subprocess ran under a 2 GB address-space cap. Importing pandas maps
past that, so the interpreter died before pytest wrote a line, and a nonzero exit
with no output was published as `pass_rate=0.0`. Official csvs-to-sqlite was
recorded as 0.000 in all three arms on 2026-08-15; the same repositories score
17/25 once the cap clears what the suite reserves.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from orchestra.codeprojecteval.ceiling import CeilingReport
from orchestra.codeprojecteval.dataset import CpeTask

_SPEC = importlib.util.spec_from_file_location(
    "cpe_eval",
    Path(__file__).resolve().parents[3] / "scripts" / "eval_codeprojecteval.py",
)
assert _SPEC and _SPEC.loader
evaluator = importlib.util.module_from_spec(_SPEC)
sys.modules["cpe_eval"] = evaluator
_SPEC.loader.exec_module(evaluator)


def _stub_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace, an interpreter and a suite of 25 cases, all on disk."""
    repo_root = tmp_path / "dataset" / "widget"
    (repo_root / "unit_tests").mkdir(parents=True)
    (repo_root / "unit_tests" / "test_widget.py").write_text("", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "widget").mkdir(parents=True)
    (workspace / "widget" / "__init__.py").write_text("", encoding="utf-8")
    python = tmp_path / "envs" / "widget" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    task = CpeTask(
        task_id="widget",
        repo_root=repo_root,
        language="python",
        source_dir="widget",
        prd_path="docs/prd.md",
        uml_paths=[],
        architecture_path="docs/arch.md",
        directory_tree_path="docs/tree.md",
        requirements_path="requirements.txt",
        check_tests="check_tests",
        unit_tests="unit_tests",
    )
    monkeypatch.setattr(evaluator, "load_task", lambda *a, **k: task)
    monkeypatch.setattr(evaluator, "_suite_sizes", lambda *a, **k: {})
    monkeypatch.setattr(
        evaluator,
        "analyze_ceiling",
        lambda *a, **k: CeilingReport(
            task_id="widget", tests_total=25, tests_reachable=25, tests_blocked=0
        ),
    )
    return workspace


def _score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proc: object) -> dict:
    workspace = _stub_dataset(tmp_path, monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: proc)
    return evaluator.score_task(
        "widget",
        workspace=workspace,
        dataset_root=tmp_path / "dataset",
        env_root=tmp_path / "envs",
        timeout=60,
    )


def test_a_scorer_killed_before_pytest_spoke_is_not_a_scored_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _score(
        tmp_path,
        monkeypatch,
        subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
    )

    assert result["status"] == "timeout"
    assert result["pass_rate"] is None
    assert result["pass_rate_reachable"] is None


def test_a_suite_that_ran_and_failed_everything_is_still_a_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence is the signal, not the exit code: pytest that spoke is a verdict."""
    result = _score(
        tmp_path,
        monkeypatch,
        subprocess.CompletedProcess(
            args=[], returncode=1, stdout="25 failed in 1.10s\n", stderr=""
        ),
    )

    assert result["status"] == "fail"
    assert result["pass_rate"] == 0.0


def test_a_conftest_that_would_not_import_keeps_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pytest reports collection failure on stderr, which the record dropped."""
    result = _score(
        tmp_path,
        monkeypatch,
        subprocess.CompletedProcess(
            args=[],
            returncode=4,
            stdout="",
            stderr="ImportError while loading conftest: no attribute 'LockerType'",
        ),
    )

    assert result["status"] == "fail"
    assert "LockerType" in result["stderr_tail"]


def test_the_address_space_ceiling_clears_a_scientific_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pandas maps past 2 GB on import, so 2048 scored the harness, not the code."""
    seen: dict[str, int] = {}

    def capture(memory_mb: int, cpu_seconds: int):  # noqa: ANN202
        seen["memory_mb"] = memory_mb
        return lambda: None

    monkeypatch.setattr(evaluator, "_resource_caps", capture)
    _score(
        tmp_path,
        monkeypatch,
        subprocess.CompletedProcess(
            args=[], returncode=0, stdout="25 passed in 1.10s\n", stderr=""
        ),
    )

    assert seen["memory_mb"] >= 4096
