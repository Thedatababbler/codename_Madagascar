"""The authored-suite quality axis: graded, frozen, non-gating, vacuity-filtered.

EXP-20260810-03 measured the acceptance gate ranking nine designs identically at
1.0 while their held-out pass rates spanned 0.307-0.375. The gate asks whether
symbols exist; these tests cover the stage that asks whether they behave.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from orchestra.codeprojecteval import (
    build_agent_workspace,
    check_command,
    load_task,
    materialize_check_harness,
)
from orchestra.codeprojecteval.harness import (
    build_deterministic_contracts,
    contracts_path_for,
    spec_tests_path_for,
)

REFERENCE = (
    "class Widget:\n    value = 1\n\n    @staticmethod\n    def add(a, b):\n        return a + b\n"
)
# Structurally identical to REFERENCE — same module, same symbols, same signature.
# The acceptance gate cannot tell these two apart; that is the whole problem.
WRONG_ADD = (
    "class Widget:\n    value = 1\n\n    @staticmethod\n    def add(a, b):\n        return 0\n"
)

# Four behavioural assertions, none of which can pass without an implementation.
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


def _dataset(tmp_path: Path) -> Path:
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
    (root / "docs" / "PRD.md").write_text("a widget adds numbers", encoding="utf-8")
    (root / "docs" / "UML.md").write_text("classDiagram", encoding="utf-8")
    (root / "docs" / "architecture_design.md").write_text("layers", encoding="utf-8")
    (root / "docs" / "directory_tree.txt").write_text(
        "├── demo_pkg\n│   ├── __init__.py\n│   └── core.py\n", encoding="utf-8"
    )
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (root / "demo_pkg" / "__init__.py").write_text(
        "from demo_pkg.core import Widget\n", encoding="utf-8"
    )
    (root / "demo_pkg" / "core.py").write_text(REFERENCE, encoding="utf-8")
    (root / "check_tests" / "test_widget.py").write_text(
        "from demo_pkg import Widget\n\n\ndef test_value():\n    assert Widget.value == 1\n",
        encoding="utf-8",
    )
    (root / "unit_tests" / "test_hidden.py").write_text(
        "def test_secret():\n    assert False\n", encoding="utf-8"
    )
    return root.parent


class Harness:
    """One materialised harness plus the workspace it grades."""

    def __init__(self, tmp_path: Path, *, level: str = "implementation") -> None:
        self.task = load_task("demo", dataset_root=_dataset(tmp_path))
        self.ws = build_agent_workspace(self.task, tmp_path / "ws")
        self.harness_dir = tmp_path / "harness"
        materialize_check_harness(
            self.task, harness_dir=self.harness_dir, env_python=Path(sys.executable)
        )
        contracts = build_deterministic_contracts(self.task, role=level, milestone_id="m1")
        path = contracts_path_for(self.harness_dir, "m1")
        path.write_text(json.dumps(contracts), encoding="utf-8")
        self.frozen = spec_tests_path_for(self.harness_dir, "m1")
        self.command = check_command(
            harness_dir=self.harness_dir,
            level=level,
            env_python=Path(sys.executable),
            contracts_path=path,
            spec_tests_path=self.frozen,
        )
        self.command_without_spec = check_command(
            harness_dir=self.harness_dir,
            level=level,
            env_python=Path(sys.executable),
            contracts_path=path,
        )

    def implement(self, source: str = REFERENCE) -> None:
        (self.ws / "demo_pkg").mkdir(exist_ok=True)
        (self.ws / "demo_pkg" / "__init__.py").write_text(
            "from demo_pkg.core import Widget\n", encoding="utf-8"
        )
        (self.ws / "demo_pkg" / "core.py").write_text(source, encoding="utf-8")

    def author(self, suite: str = AUTHORED_SUITE, name: str = "test_spec.py") -> None:
        target = self.ws / "spec_tests"
        target.mkdir(exist_ok=True)
        (target / name).write_text(suite, encoding="utf-8")

    def run(self, *, with_spec: bool = True) -> tuple[int, dict]:
        command = self.command if with_spec else self.command_without_spec
        proc = subprocess.run(command, cwd=self.ws, capture_output=True, text=True, check=False)
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
    """The flag has to be inert when no suite exists, or every historical score moves."""
    harness = Harness(tmp_path)
    harness.implement()

    code_with, with_flag = harness.run(with_spec=True)
    code_without, without_flag = harness.run(with_spec=False)

    assert code_with == code_without == 0
    assert with_flag["score"] == without_flag["score"] == 1.0
    assert Harness.stage(with_flag, "spec_tests") is None
    assert [s["weight"] for s in with_flag["stages"]] == [0.3, 0.4, 0.3]


def test_the_authored_suite_is_graded_but_never_gates(tmp_path: Path) -> None:
    """A milestone that fails its own tests still has to be able to freeze.

    Parts of an authored suite may be unsatisfiable or simply wrong, so it moves
    the score and not the exit code; gating on it would deadlock the milestone.
    """
    harness = Harness(tmp_path)
    harness.implement(WRONG_ADD)
    harness.author()

    code, report = harness.run()

    assert code == 0, "the authored suite must not gate the milestone"
    spec = Harness.stage(report, "spec_tests")
    assert spec is not None and spec["weight"] == 0.4
    # value_is_one and add_handles_zero pass under WRONG_ADD; the two real sums do not.
    assert (spec["passed_units"], spec["total_units"]) == (2, 4)
    assert report["score"] < 1.0
    assert "SPEC spec_tests 2/4" in report["stdout"]


def test_two_designs_tied_on_structure_separate_on_behaviour(tmp_path: Path) -> None:
    """The defect this stage exists to fix: EXP-20260810-03's nine 1.0s."""
    good = Harness(tmp_path / "good")
    good.implement(REFERENCE)
    good.author()
    bad = Harness(tmp_path / "bad")
    bad.implement(WRONG_ADD)
    bad.author()

    good_code, good_report = good.run()
    bad_code, bad_report = bad.run()

    assert good_code == bad_code == 0
    # Identical on every structural stage — which is all the gate could ever see.
    structural = lambda r: [  # noqa: E731
        (s["stage"], s["passed_units"], s["total_units"])
        for s in r["stages"]
        if s["stage"] != "spec_tests"
    ]
    assert structural(good_report) == structural(bad_report)
    assert good_report["score"] > bad_report["score"] + 0.02, (
        "must separate by more than the quality epsilon"
    )
    assert good_report["score"] == 1.0 and bad_report["score"] == 0.8


def test_the_suite_is_frozen_so_a_later_agent_cannot_mark_its_own_exam(
    tmp_path: Path,
) -> None:
    """Rewriting the workspace copy must not move the score."""
    harness = Harness(tmp_path)
    harness.implement(WRONG_ADD)
    harness.author()

    _, first = harness.run()
    assert (harness.frozen / "test_spec.py").is_file(), "suite was not frozen out"

    harness.author(suite="def test_trivially_true():\n    assert True\n", name="test_spec.py")
    _, second = harness.run()

    assert Harness.stage(second, "spec_tests") == Harness.stage(first, "spec_tests")
    assert second["score"] == first["score"]
    # And the workspace copy is put back, so an implementer reads the real yardstick.
    assert "test_add_sums" in (harness.ws / "spec_tests" / "test_spec.py").read_text()


def test_the_frozen_suite_survives_a_deleted_workspace_copy(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.implement(WRONG_ADD)
    harness.author()
    _, first = harness.run()

    for leftover in (harness.ws / "spec_tests").iterdir():
        leftover.unlink()
    (harness.ws / "spec_tests").rmdir()
    _, second = harness.run()

    assert Harness.stage(second, "spec_tests") == Harness.stage(first, "spec_tests")


def test_vacuous_tests_leave_the_yardstick(tmp_path: Path) -> None:
    """A test that passes with no implementation measures nothing."""
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author()
    harness.author(
        suite="import pathlib\n\n\ndef test_docs_exist():\n"
        "    assert pathlib.Path('docs/PRD.md').is_file()\n",
        name="test_docs.py",
    )

    _, report = harness.run()

    spec = Harness.stage(report, "spec_tests")
    assert spec is not None
    # Five collected, one passes against the pristine repository, four are real.
    assert (spec["passed_units"], spec["total_units"]) == (4, 4)
    assert "vacuous 1 excluded" in report["stdout"]
    assert report["score"] == 1.0


def test_a_vacuous_test_is_only_caught_when_its_module_imports_cleanly(
    tmp_path: Path,
) -> None:
    """A known limit, asserted so it cannot change silently.

    The baseline runs against a repository with no source, so a file whose
    top-level import fails errors during collection and none of its tests run —
    including any vacuous one sharing the file. The filter is therefore inert
    for import-coupled files and only bites on tests that stand alone, which is
    the safe direction: the dangerous case, a suite of assertions that never
    touch the implementation, is exactly the case it catches.
    """
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author(suite=AUTHORED_SUITE + "\n\ndef test_trivially_true():\n    assert True\n")

    _, report = harness.run()

    spec = Harness.stage(report, "spec_tests")
    assert spec is not None
    assert spec["total_units"] == 5, "the shielded vacuous test stays in the yardstick"
    assert "vacuous 0 excluded" in report["stdout"]


def test_a_wholly_vacuous_suite_falls_back_instead_of_scoring_zero(
    tmp_path: Path,
) -> None:
    """Otherwise authoring junk tests would rank below authoring none at all."""
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author(suite="def test_nothing():\n    assert True\n")

    code, report = harness.run()
    _, baseline = harness.run(with_spec=False)

    assert code == 0
    assert Harness.stage(report, "spec_tests") is None
    assert report["score"] == baseline["score"] == 1.0
    assert "nothing gradable" in report["stdout"]


def test_behaviour_is_reported_even_when_a_structural_stage_failed(
    tmp_path: Path,
) -> None:
    """The base attempt of EXP-20260810-02 failed contracts at 34/36.

    Grading has to continue past that, or the one attempt most in need of a
    gradient is the one that reports none.
    """
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author()
    contracts_path = contracts_path_for(harness.harness_dir, "m1")
    contracts = json.loads(contracts_path.read_text())
    contracts["checks"].append(
        {
            "type": "export",
            "module": "demo_pkg",
            "symbol": "NeverImplemented",
            "required_levels": ["implementation"],
        }
    )
    contracts_path.write_text(json.dumps(contracts), encoding="utf-8")

    code, report = harness.run()

    assert code == 1, "a missing contract still fails the gate"
    contract_stage = Harness.stage(report, "contracts")
    assert contract_stage is not None
    assert contract_stage["passed_units"] < contract_stage["total_units"]
    spec = Harness.stage(report, "spec_tests")
    assert spec == {
        "stage": "spec_tests",
        "passed_units": 4,
        "total_units": 4,
        "weight": 0.4,
    }
    assert report["score"] > 0.4, "behaviour has to survive a failed gate"


def test_the_axis_is_monotone_across_progressively_better_designs(
    tmp_path: Path,
) -> None:
    """What the Pareto search actually needs: an ordering, not a pass/fail."""
    sources = {
        "no_add": "class Widget:\n    value = 1\n",
        "wrong_add": WRONG_ADD,
        "correct": REFERENCE,
    }
    scores = {}
    for name, source in sources.items():
        harness = Harness(tmp_path / name)
        harness.implement(source)
        harness.author()
        _, report = harness.run()
        scores[name] = report["score"]

    assert scores["no_add"] < scores["wrong_add"] < scores["correct"]
    assert scores["correct"] == 1.0
    gaps = [
        scores["wrong_add"] - scores["no_add"],
        scores["correct"] - scores["wrong_add"],
    ]
    assert all(gap > 0.02 for gap in gaps), f"every step must clear the quality epsilon, got {gaps}"


def test_an_unimportable_file_does_not_flatten_the_rest_of_the_suite(
    tmp_path: Path,
) -> None:
    """pytest aborts collection on the first bad import unless told not to."""
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author()
    harness.author(
        suite="from demo_pkg.not_built_yet import Later\n\n\n"
        "def test_later():\n    assert Later is not None\n",
        name="test_later.py",
    )

    _, report = harness.run()

    spec = Harness.stage(report, "spec_tests")
    assert spec is not None
    # Four real tests pass; the unimportable file counts as one failed unit.
    assert (spec["passed_units"], spec["total_units"]) == (4, 5)
    assert 0.7 < report["score"] < 1.0


def test_the_pristine_baseline_withholds_the_reference_implementation(
    tmp_path: Path,
) -> None:
    """Using the dataset root would call every correct test vacuous."""
    harness = Harness(tmp_path)
    pristine = harness.harness_dir / "pristine_repo"

    assert pristine.is_dir()
    assert not (pristine / "demo_pkg").exists()
    assert not (pristine / "unit_tests").exists()
    assert (pristine / "docs" / "PRD.md").is_file()
