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

# Three more cases in a file whose import fails until `demo_pkg.extra` exists.
EXTRA_SUITE = """from demo_pkg.extra import triple


def test_triple_of_one():
    assert triple(1) == 3


def test_triple_of_two():
    assert triple(2) == 6


def test_triple_of_zero():
    assert triple(0) == 0
"""

# The style pytest collects by base class rather than by name.
UNITTEST_SUITE = """import unittest

from demo_pkg.extra import triple


class TripleBehaviour(unittest.TestCase):
    def test_triple_of_one(self):
        self.assertEqual(triple(1), 3)

    def test_triple_of_two(self):
        self.assertEqual(triple(2), 6)
"""

PARAMETRIZED_SUITE = """import pytest

from demo_pkg import Widget


@pytest.mark.parametrize("a,b,expected", [(1, 2, 3), (2, 5, 7), (0, 0, 0)])
def test_add_cases(a, b, expected):
    assert Widget.add(a, b) == expected
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

    def custody(self) -> subprocess.CompletedProcess[str]:
        """The step the graph runs between the author and the implementer."""
        return subprocess.run(
            [*self.command, "--take-custody"],
            cwd=self.ws,
            capture_output=True,
            text=True,
            check=False,
        )

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
    # compile / imports / cross_imports / contracts. The cross-import stage
    # joined the implementation level in 2026-09; what this test guards is that
    # the spec flag is inert without an authored suite, not the exact split.
    assert [s["weight"] for s in with_flag["stages"]] == [0.25, 0.3, 0.15, 0.3]
    assert sum(s["weight"] for s in with_flag["stages"]) == 1.0


def test_a_milestone_that_lost_its_suite_says_so(tmp_path: Path) -> None:
    """The first test-first run scored no behaviour at all and looked healthy.

    Each builder deleted `spec_tests/` before the gate could copy it out, so the
    stage was simply absent and the milestone passed. Absence and "nothing was
    ever authored" are indistinguishable in the score, so the gate log has to
    separate them.
    """
    harness = Harness(tmp_path)
    harness.implement()

    code, report = harness.run()

    assert code == 0
    assert Harness.stage(report, "spec_tests") is None
    assert "NOTE no authored suite at spec_tests" in report["stdout"]


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


def test_the_failing_tests_are_named_not_just_counted(tmp_path: Path) -> None:
    """A tuning search needs to know which tests moved, not how many.

    Two designs failing the same count of tests may be failing different tests, and
    a design's advantage may sit entirely inside a handful that vary while dozens of
    others pass for everybody. Counts cannot express either.
    """
    harness = Harness(tmp_path)
    harness.implement(WRONG_ADD)
    harness.author()

    _code, report = harness.run()
    stage = Harness.stage(report, "spec_tests")

    assert stage is not None
    named = stage.get("failed_tests") or []
    assert named, "the behavioural stage reported no identities"
    assert len(named) == stage["total_units"] - stage["passed_units"]
    assert all("::" in nodeid for nodeid in named)
    assert any("add" in nodeid for nodeid in named)


def test_a_fully_passing_behavioural_stage_names_nothing(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author()

    _code, report = harness.run()
    stage = Harness.stage(report, "spec_tests")

    assert stage is not None
    assert stage["passed_units"] == stage["total_units"]
    assert not stage.get("failed_tests")


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
    # And the workspace copy is gone rather than restored: an implementer that
    # can read the yardstick passes all of it and ranks against nothing.
    assert not (harness.ws / "spec_tests").exists()


def test_custody_takes_the_suite_out_of_the_workspace_without_grading(
    tmp_path: Path,
) -> None:
    """It runs before any implementation exists, so it must not run the gate."""
    harness = Harness(tmp_path)
    harness.author()

    proc = harness.custody()

    assert proc.returncode == 0
    assert (harness.frozen / "test_spec.py").is_file()
    assert not (harness.ws / "spec_tests").exists()
    assert "ADAMAS_HARNESS_SCORE" not in proc.stdout, "custody must not score"


def test_a_suite_in_custody_still_scores_the_milestone(tmp_path: Path) -> None:
    """Hiding the yardstick must cost nothing: grading reads the frozen copy."""
    harness = Harness(tmp_path)
    harness.author()
    harness.custody()
    harness.implement(WRONG_ADD)

    _code, report = harness.run()
    stage = Harness.stage(report, "spec_tests")

    assert stage is not None
    assert (stage["passed_units"], stage["total_units"]) == (2, 4)


def test_custody_is_harmless_when_the_author_wrote_nothing(tmp_path: Path) -> None:
    """A milestone whose author failed still has to reach its implementer."""
    harness = Harness(tmp_path)

    proc = harness.custody()

    assert proc.returncode == 0
    assert not harness.frozen.exists()


def test_the_frozen_suite_survives_a_deleted_workspace_copy(tmp_path: Path) -> None:
    """Every run after the first grades a workspace with no suite in it."""
    harness = Harness(tmp_path)
    harness.implement(WRONG_ADD)
    harness.author()
    _, first = harness.run()
    assert not (harness.ws / "spec_tests").exists()

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


def test_an_unimportable_file_costs_every_test_it_holds(tmp_path: Path) -> None:
    """pytest reports a module it cannot import as one error, not as its cases.

    Left alone that shrinks the denominator exactly when the code is broken, so
    the worst work is divided by the smallest exam. EXP-20260811-03 measured one
    milestone's four candidates scored out of 36, 31, 36 and 31 against a single
    frozen suite for this reason.
    """
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author()
    harness.author(suite=EXTRA_SUITE, name="test_extra.py")

    _, report = harness.run()

    spec = Harness.stage(report, "spec_tests")
    assert spec is not None
    assert (spec["passed_units"], spec["total_units"]) == (4, 7)
    assert "SPEC spec_tests 4/7 (vacuous 0 excluded; 3 not collected)" in report["stdout"]
    named = spec.get("failed_tests") or []
    assert any("test_extra.py" in nodeid for nodeid in named), (
        "the file that never ran has to be named, or the search cannot see it"
    )


def test_two_designs_are_graded_against_the_same_denominator(tmp_path: Path) -> None:
    """The invariant the fast loop rests on: one milestone, one exam.

    Candidates fork a workspace but share the frozen suite, so a denominator
    that tracks the candidate's own code makes their scores incomparable — and
    rewards the candidate that broke an import over one that merely failed the
    assertions.
    """
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author()
    harness.author(suite=EXTRA_SUITE, name="test_extra.py")
    extra = harness.ws / "demo_pkg" / "extra.py"
    extra.write_text("def triple(n):\n    return n * 3\n", encoding="utf-8")

    _, whole = harness.run()
    extra.unlink()
    _, broken = harness.run()

    whole_stage = Harness.stage(whole, "spec_tests")
    broken_stage = Harness.stage(broken, "spec_tests")
    assert whole_stage is not None and broken_stage is not None
    assert whole_stage["total_units"] == broken_stage["total_units"] == 7
    assert whole_stage["passed_units"] == 7
    assert broken_stage["passed_units"] == 4
    assert broken["score"] < whole["score"]


def test_unittest_classes_are_counted_whatever_they_are_named(tmp_path: Path) -> None:
    """pytest collects any TestCase subclass; the count has to agree with it.

    Authors write `class ConnectionSetupTests(unittest.TestCase)` as often as
    `class TestConnection`, and a counter that only knows the `Test*` prefix
    reads such a suite as empty.
    """
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author(suite=UNITTEST_SUITE, name="test_case_style.py")

    _, report = harness.run()

    spec = Harness.stage(report, "spec_tests")
    assert spec is not None
    # The file cannot be imported, so its cases are counted from source alone.
    assert (spec["passed_units"], spec["total_units"]) == (0, 2)


def test_a_suite_that_collects_more_than_its_source_shows_keeps_the_larger_size(
    tmp_path: Path,
) -> None:
    """A decorator can multiply one function into several cases.

    Counting source alone would under-report those, so the pin rises to what
    pytest actually collected and is remembered for the runs that follow.
    """
    harness = Harness(tmp_path)
    harness.implement(REFERENCE)
    harness.author(suite=PARAMETRIZED_SUITE, name="test_parametrized.py")

    _, report = harness.run()

    spec = Harness.stage(report, "spec_tests")
    assert spec is not None
    assert (spec["passed_units"], spec["total_units"]) == (3, 3)
    pinned = harness.frozen.parent / (harness.frozen.name + ".size.json")
    assert json.loads(pinned.read_text()) == {"pinned": 3, "static": 1}


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


def test_a_suite_that_imports_itself_survives_being_frozen(tmp_path: Path) -> None:
    """EXP-20260903-03: pyjwt's authored suite scored 0.000 against an
    implementation that passes 25 of its 29 cases.

    The suite is authored inside the repository as `spec_tests/`, so its files
    import shared helpers as `from spec_tests.conftest import ...`. Freezing it
    out of the workspace renamed the directory to `<milestone>.spec_tests` --
    not even a legal module name -- so every file died at collection and the
    behaviour axis read zero for a milestone that was mostly correct.
    """
    from orchestra.codeprojecteval.harness import _check_script_source

    script = tmp_path / "check.py"
    script.write_text(_check_script_source(), encoding="utf-8")
    import importlib.util

    spec = importlib.util.spec_from_file_location("cpe_check_probe", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    frozen = tmp_path / "m1.spec_tests"
    frozen.mkdir()
    (frozen / "__init__.py").write_text("", encoding="utf-8")
    (frozen / "conftest.py").write_text("HELPER = 'shared'\n", encoding="utf-8")
    (frozen / "test_uses_helper.py").write_text(
        "from spec_tests.conftest import HELPER\n\n"
        "def test_helper_is_importable():\n    assert HELPER == 'shared'\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()

    root = mod._spec_import_root(str(frozen))
    passed, collected, ran, _tail, _failed = mod._run_pytest(
        str(repo), str(frozen), timeout=120, import_root=root
    )

    assert (passed, collected, ran) == (1, 1, True)


def test_custody_refuses_a_suite_that_collects_nothing(tmp_path: Path) -> None:
    """A suite that dies at collection would grade nothing for the whole milestone."""
    harness = Harness(tmp_path)
    harness.implement()
    harness.author(
        "import pytest\n\n"
        "@pytest.mark.parametrize('a, b', [(1, 2, 3)])\n"
        "def test_broken(a, b):\n    assert a\n"
    )
    proc = harness.custody()
    assert proc.returncode == 2
    assert "custody refused" in proc.stderr
    assert not harness.frozen.exists()
    assert not (harness.ws / "spec_tests").exists()  # the workspace copy is still taken
    # the next attempt can freeze a suite that collects
    harness.author()
    proc = harness.custody()
    assert proc.returncode == 0, proc.stderr
    assert harness.frozen.exists()
    assert "case(s) collect" in proc.stdout


def test_custody_tolerates_a_project_import_the_implementer_has_not_written_yet(tmp_path: Path) -> None:
    """Custody runs before the implementer: the package missing is not the suite's fault."""
    harness = Harness(tmp_path)
    harness.author("from demo_pkg.core import Widget\n\n\ndef test_widget():\n    assert Widget\n")
    proc = harness.custody()
    assert proc.returncode == 0, proc.stderr
    assert harness.frozen.exists()
    assert "wait for the project package" in proc.stdout
