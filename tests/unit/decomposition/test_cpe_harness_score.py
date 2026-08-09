"""The CodeProjectEval harness grades how far a milestone got, not just pass/fail."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestra.codeprojecteval.harness import _check_script_source


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    script = tmp_path / "check.py"
    script.write_text(_check_script_source(), encoding="utf-8")
    return script


def _manifest(tmp_path: Path, **overrides: object) -> Path:
    payload = {
        "expected_modules": ["pkg.a", "pkg.b", "pkg.c"],
        "top_level_packages": ["pkg"],
        "check_tests_dir": "check_tests",
    }
    payload.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _repo(tmp_path: Path, modules: list[str]) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True, exist_ok=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    for name in modules:
        (repo / "pkg" / f"{name}.py").write_text(f"VALUE = '{name}'", encoding="utf-8")
    return repo


def _run(harness: Path, manifest: Path, repo: Path, level: str = "implementation") -> tuple:
    proc = subprocess.run(
        [sys.executable, str(harness), "--level", level, "--manifest", str(manifest)],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    payload: dict = {}
    for line in proc.stdout.splitlines():
        if line.startswith("ADAMAS_HARNESS_SCORE "):
            payload = json.loads(line.split(" ", 1)[1])
    return proc.returncode, payload


def test_the_score_rises_as_more_of_the_milestone_lands(
    harness: Path, tmp_path: Path
) -> None:
    """A gate cannot express this, which is why the fast loop had nothing to climb."""
    manifest = _manifest(tmp_path)
    scores = []
    for modules in (["a"], ["a", "b"], ["a", "b", "c"]):
        code, payload = _run(harness, manifest, _repo(tmp_path, modules))
        scores.append(payload["score"])

    assert scores[0] < scores[1] < scores[2]
    assert scores[-1] == 1.0
    assert code == 0


def test_a_fully_passing_milestone_scores_exactly_one(
    harness: Path, tmp_path: Path
) -> None:
    """Anything less would make a perfect run look improvable."""
    code, payload = _run(harness, _manifest(tmp_path), _repo(tmp_path, ["a", "b", "c"]))

    assert code == 0
    assert payload["score"] == 1.0


def test_a_repository_with_nothing_in_it_scores_zero(
    harness: Path, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    code, payload = _run(harness, _manifest(tmp_path), empty)

    assert code == 1
    assert payload["score"] == 0.0


def test_stages_report_ratios_not_verdicts(harness: Path, tmp_path: Path) -> None:
    _, payload = _run(harness, _manifest(tmp_path), _repo(tmp_path, ["a", "b"]))
    stages = {s["stage"]: (s["passed_units"], s["total_units"]) for s in payload["stages"]}

    assert stages["imports"] == (2, 3)
    assert payload["furthest_stage"] == "imports"


def test_the_discovery_level_is_graded_on_compilation_alone(
    harness: Path, tmp_path: Path
) -> None:
    """A discovery gate that demanded imports would be an implementation gate."""
    code, payload = _run(harness, _manifest(tmp_path), _repo(tmp_path, ["a"]), "discovery")

    assert code == 0
    assert payload["score"] == 1.0
    assert [s["stage"] for s in payload["stages"]] == ["compile"]


def test_a_syntax_error_stops_the_score_at_compilation(
    harness: Path, tmp_path: Path
) -> None:
    repo = _repo(tmp_path, ["a", "b", "c"])
    (repo / "pkg" / "b.py").write_text("def broken(:\n", encoding="utf-8")

    code, payload = _run(harness, _manifest(tmp_path), repo)

    assert code == 1
    assert payload["score"] == 0.0
    assert [s["stage"] for s in payload["stages"]] == ["compile"]
