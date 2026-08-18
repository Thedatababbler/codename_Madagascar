"""RealBench's authored-suite quality axis: graded, frozen, non-gating.

The public check used to emit no stages at all, so a quality trigger reading
`behaviour_score` always saw `None` and never fired. These tests cover the
stage that asks whether the symbols behave, copied from the CodeProjectEval
gate rather than invented for this dataset. Hidden `proj_with_test` fixtures
are never referenced.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orchestra.harness.progress import behaviour_score
from orchestra.realbench.public_harness import (
    materialize_public_harness,
    public_check_command,
    spec_tests_path_for,
)

REFERENCE = (
    "class Widget:\n    value = 1\n\n    @staticmethod\n    def add(a, b):\n        return a + b\n"
)
WRONG_ADD = (
    "class Widget:\n    value = 1\n\n    @staticmethod\n    def add(a, b):\n        return 0\n"
)
AUTHORED_SUITE = """from demo_pkg import Widget


def test_value_is_one():
    assert Widget.value == 1


def test_add_sums():
    assert Widget.add(1, 2) == 3


def test_add_handles_zero():
    assert Widget.add(0, 0) == 0


def test_add_larger_operands():
    assert Widget.add(2, 5) == 7
"""


def _workspace(tmp_path: Path) -> Path:
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
        json.dumps(
            {
                "packageDiagram": {
                    "packages": [
                        {"name": "core", "type": "package", "exports": ["Widget"]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (ws / "demo_pkg").mkdir()
    (ws / "demo_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "demo_pkg" / "core.py").write_text("", encoding="utf-8")
    return ws


class Harness:
    def __init__(self, tmp_path: Path, *, level: str = "implementation") -> None:
        self.ws = _workspace(tmp_path)
        self.harness_dir = tmp_path / "harness"
        materialize_public_harness(self.ws, harness_dir=self.harness_dir)
        self.frozen = spec_tests_path_for(self.harness_dir, "m1")
        self.command = public_check_command(
            harness_dir=self.harness_dir,
            level=level,
            spec_tests_path=self.frozen,
        )
        self.command_without_spec = public_check_command(
            harness_dir=self.harness_dir,
            level=level,
        )

    def implement(self, source: str = REFERENCE) -> None:
        (self.ws / "demo_pkg" / "__init__.py").write_text(
            "from demo_pkg.core import Widget\n", encoding="utf-8"
        )
        (self.ws / "demo_pkg" / "core.py").write_text(source, encoding="utf-8")

    def author(self, suite: str = AUTHORED_SUITE, name: str = "test_spec.py") -> None:
        target = self.ws / "spec_tests"
        target.mkdir(exist_ok=True)
        (target / name).write_text(suite, encoding="utf-8")

    def custody(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.command, "--take-custody"],
            cwd=self.ws,
            capture_output=True,
            text=True,
            check=False,
        )

    def run(self, *, with_spec: bool = True) -> tuple[int, dict]:
        command = self.command if with_spec else self.command_without_spec
        proc = subprocess.run(
            command, cwd=self.ws, capture_output=True, text=True, check=False
        )
        report: dict = {}
        for line in proc.stdout.splitlines():
            if line.startswith("ADAMAS_HARNESS_SCORE "):
                report = json.loads(line.split(" ", 1)[1])
        report["stdout"] = proc.stdout
        report["stderr"] = proc.stderr
        return proc.returncode, report

    @staticmethod
    def stage(report: dict, name: str) -> dict | None:
        return next((s for s in report.get("stages") or [] if s["stage"] == name), None)


def test_a_milestone_that_authored_nothing_scores_exactly_as_before(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.implement()

    code_with, with_flag = harness.run(with_spec=True)
    code_without, without_flag = harness.run(with_spec=False)

    assert code_with == code_without == 0
    assert with_flag["score"] == without_flag["score"]
    assert Harness.stage(with_flag, "spec_tests") is None
    assert behaviour_score(with_flag.get("stages")) is None


def test_a_structural_pass_without_a_suite_emits_stages_but_no_behaviour(
    tmp_path: Path,
) -> None:
    """The previous RealBench gate printed nothing a quality trigger could read."""
    harness = Harness(tmp_path)
    harness.implement()

    code, report = harness.run(with_spec=False)

    assert code == 0
    assert report.get("stages")
    assert Harness.stage(report, "compile") is not None
    assert Harness.stage(report, "imports") is not None
    assert behaviour_score(report.get("stages")) is None
    assert "NOTE no authored suite" not in report["stdout"]


def test_a_milestone_that_lost_its_suite_says_so(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.implement()

    code, report = harness.run()

    assert code == 0
    assert Harness.stage(report, "spec_tests") is None
    assert "NOTE no authored suite at spec_tests" in report["stdout"]


def test_the_authored_suite_is_graded_but_never_gates(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.implement(WRONG_ADD)
    harness.author()

    code, report = harness.run()

    assert code == 0, "the authored suite must not gate the milestone"
    spec = Harness.stage(report, "spec_tests")
    assert spec is not None and spec["weight"] == 0.4
    assert (spec["passed_units"], spec["total_units"]) == (2, 4)
    assert report["score"] < 1.0
    assert "SPEC spec_tests 2/4" in report["stdout"]
    assert behaviour_score(report["stages"]) == 0.5


def test_two_designs_tied_on_structure_separate_on_behaviour(tmp_path: Path) -> None:
    good = Harness(tmp_path / "good")
    good.implement(REFERENCE)
    good.author()
    bad = Harness(tmp_path / "bad")
    bad.implement(WRONG_ADD)
    bad.author()

    good_code, good_report = good.run()
    bad_code, bad_report = bad.run()

    assert good_code == bad_code == 0
    structural = lambda r: [  # noqa: E731
        (s["stage"], s["passed_units"], s["total_units"])
        for s in r["stages"]
        if s["stage"] != "spec_tests"
    ]
    assert structural(good_report) == structural(bad_report)
    assert good_report["score"] > bad_report["score"] + 0.02
    assert behaviour_score(good_report["stages"]) == 1.0
    assert behaviour_score(bad_report["stages"]) == 0.5


def test_failing_assertions_are_not_printed(tmp_path: Path) -> None:
    """The pytest tail would hand the implementer the hidden suite's text."""
    harness = Harness(tmp_path)
    harness.implement(WRONG_ADD)
    harness.author()

    _code, report = harness.run()

    assert "Widget.add(1, 2) == 3" not in report["stdout"]
    assert "AssertionError" not in report["stdout"]
    spec = Harness.stage(report, "spec_tests")
    assert spec is not None
    named = spec.get("failed_tests") or []
    assert named and all("::" in nodeid for nodeid in named)


def test_custody_takes_the_suite_out_of_the_workspace_without_grading(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.author()

    proc = harness.custody()

    assert proc.returncode == 0
    assert (harness.frozen / "test_spec.py").is_file()
    assert not (harness.ws / "spec_tests").exists()
    assert "ADAMAS_HARNESS_SCORE" not in proc.stdout, "custody must not score"


def test_the_suite_is_frozen_so_a_later_agent_cannot_mark_its_own_exam(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.implement(WRONG_ADD)
    harness.author()

    _, first = harness.run()
    assert (harness.frozen / "test_spec.py").is_file()

    harness.author(
        suite="def test_trivially_true():\n    assert True\n", name="test_spec.py"
    )
    _, second = harness.run()

    assert Harness.stage(second, "spec_tests") == Harness.stage(first, "spec_tests")
    assert second["score"] == first["score"]
    assert not (harness.ws / "spec_tests").exists()


def test_binder_passes_the_spec_tests_path(tmp_path: Path) -> None:
    from orchestra.decomposition.realbench_plan import realbench_harness_binder
    from orchestra.realbench.milestone_planner import parse_plan_payload

    ws = _workspace(tmp_path)
    harness_dir = tmp_path / "harness"
    materialize_public_harness(ws, harness_dir=harness_dir)
    draft = parse_plan_payload(
        {
            "milestones": [
                {
                    "milestone_id": "m1",
                    "objective": "build it",
                    "role": "implementation",
                    "agents": [{"role_id": "dev", "mandate": "implement"}],
                }
            ]
        }
    )
    command = realbench_harness_binder(workspace=ws, harness_dir=harness_dir)(
        draft.milestones[0]
    )

    assert "--spec-tests" in command
    assert str(spec_tests_path_for(harness_dir, "m1")) in command
