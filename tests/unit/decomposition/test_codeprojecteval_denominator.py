"""The held-out denominator must be real or absent, never guessed.

bplustree was scored with a denominator of 59 in some runs and 356 in others: the
collected-case counts were cached under `outputs/`, and when the cache was cold
each module fell back to counting `def test_*`, which undercounts a parametrised
suite roughly sixfold. The published pass rates were 2.54 and 3.12, and the only
reason anyone noticed is that they exceeded 1.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from orchestra.codeprojecteval.ceiling import (
    analyze_ceiling,
    denominator_faults,
    reachable_is_unsound,
)
from orchestra.codeprojecteval.dataset import CpeTask
from orchestra.codeprojecteval.suite_sizes import (
    load_pinned_suite_sizes,
    pinned_total,
    write_pinned_suite_sizes,
)

PARAMETRISED = textwrap.dedent(
    """
    import pytest
    from widget.core import add

    @pytest.mark.parametrize("a,b,want", [(1, 1, 2), (2, 2, 4), (3, 3, 6)])
    def test_add(a, b, want):
        assert add(a, b) == want
    """
)


def _task(tmp_path: Path, *, suites: dict[str, str]) -> CpeTask:
    repo = tmp_path / "widget"
    (repo / "widget").mkdir(parents=True)
    (repo / "widget" / "core.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "unit_tests").mkdir()
    for name, body in suites.items():
        (repo / "unit_tests" / name).write_text(body)
    (repo / "prd.md").write_text("The widget package exposes add() from widget.core.")
    (repo / "architecture.md").write_text("widget/core.py holds add.")
    (repo / "tree.txt").write_text("widget/\n  core.py\n")
    (repo / "requirements.txt").write_text("")
    return CpeTask(
        task_id="widget",
        repo_root=repo,
        language="python",
        source_dir="widget",
        prd_path="prd.md",
        uml_paths=[],
        architecture_path="architecture.md",
        directory_tree_path="tree.txt",
        requirements_path="requirements.txt",
        check_tests="check_tests",
        unit_tests="unit_tests",
    )


def test_a_module_missing_from_collection_is_reported_as_estimated(tmp_path: Path):
    """The silent fallback is what broke bplustree, so it now leaves a mark."""
    task = _task(tmp_path, suites={"test_add.py": PARAMETRISED})

    report = analyze_ceiling(task, collected=None)

    # Static counting sees one `def test_add`; pytest would collect three cases.
    assert report.tests_total == 1
    assert report.estimated is True
    assert report.estimated_modules == ["unit_tests/test_add.py"]


def test_real_collected_counts_are_not_estimated(tmp_path: Path):
    task = _task(tmp_path, suites={"test_add.py": PARAMETRISED})

    report = analyze_ceiling(task, collected={"unit_tests/test_add.py": 3})

    assert report.tests_total == 3
    assert report.estimated is False
    assert report.estimated_modules == []


def test_partial_collection_still_counts_as_estimated(tmp_path: Path):
    """One unreported module is enough to make the total unusable."""
    task = _task(
        tmp_path,
        suites={"test_add.py": PARAMETRISED, "test_more.py": PARAMETRISED},
    )

    report = analyze_ceiling(task, collected={"unit_tests/test_add.py": 3})

    assert report.estimated_modules == ["unit_tests/test_more.py"]
    assert denominator_faults(report, passed=0) == ["1 module(s) counted statically"]


def test_an_estimated_total_is_refused_even_when_the_rate_looks_sane(tmp_path: Path):
    """A plausible rate off a guessed denominator is the dangerous case.

    bplustree only got caught because its rate exceeded 1. A task whose static
    undercount still leaves passed < total would have published a quietly inflated
    number, so the fault is raised on the estimate itself, not on the symptom.
    """
    task = _task(tmp_path, suites={"test_add.py": PARAMETRISED})
    report = analyze_ceiling(task, collected=None)

    assert denominator_faults(report, passed=1) == ["1 module(s) counted statically"]


def test_the_bplustree_failure_is_now_refused(tmp_path: Path):
    """150 passes against a claimed 59 tests must not become a score of 2.54."""
    task = _task(tmp_path, suites={"test_add.py": PARAMETRISED})
    report = analyze_ceiling(task, collected={"unit_tests/test_add.py": 59})

    faults = denominator_faults(report, passed=150)

    assert faults == ["passed 150 of a claimed 59"]


def test_a_sound_denominator_raises_no_fault(tmp_path: Path):
    task = _task(tmp_path, suites={"test_add.py": PARAMETRISED})
    report = analyze_ceiling(task, collected={"unit_tests/test_add.py": 356})

    assert denominator_faults(report, passed=150) == []


def test_an_empty_suite_is_refused_rather_than_divided_by_zero(tmp_path: Path):
    task = _task(tmp_path, suites={})

    report = analyze_ceiling(task, collected={})

    assert report.tests_total == 0
    assert denominator_faults(report, passed=0) == ["suite size unknown"]


def test_reachability_is_flagged_when_the_run_contradicts_it(tmp_path: Path):
    """pyjwt passes ~220 tests against a claimed 105 reachable."""
    task = _task(tmp_path, suites={"test_add.py": PARAMETRISED})
    report = analyze_ceiling(task, collected={"unit_tests/test_add.py": 294})

    assert reachable_is_unsound(report, passed=150) is False

    blocked = _task(tmp_path / "b", suites={"test_add.py": PARAMETRISED})
    (blocked.repo_root / "unit_tests" / "test_hidden.py").write_text(
        "from widget.core import UNDOCUMENTED_CONSTANT\n\ndef test_x():\n    pass\n"
    )
    blocked_report = analyze_ceiling(
        blocked,
        collected={
            "unit_tests/test_add.py": 105,
            "unit_tests/test_hidden.py": 189,
        },
    )
    assert blocked_report.tests_reachable == 105
    assert reachable_is_unsound(blocked_report, passed=220) is True


class TestPinnedSuiteSizes:
    def test_a_missing_file_pins_nothing(self, tmp_path: Path):
        assert load_pinned_suite_sizes(tmp_path / "absent.json") == {}

    def test_round_trip(self, tmp_path: Path):
        target = tmp_path / "sizes.json"
        write_pinned_suite_sizes({"widget": {"unit_tests/test_add.py": 3}}, path=target)

        assert load_pinned_suite_sizes(target) == {"widget": {"unit_tests/test_add.py": 3}}
        assert pinned_total("widget", target) == 3
        assert pinned_total("absent", target) == 0

    def test_a_malformed_file_pins_nothing_rather_than_half(self, tmp_path: Path):
        target = tmp_path / "sizes.json"
        target.write_text(json.dumps({"tasks": "not-a-mapping"}))

        assert load_pinned_suite_sizes(target) == {}

    def test_the_shipped_pins_cover_the_tasks_we_score(self):
        """The tasks in play must have a pinned denominator, not a live count."""
        pinned = load_pinned_suite_sizes()

        for task_id, expected in {
            "bplustree": 357,
            "imapclient": 267,
            "pyjwt": 294,
            "simpy": 149,
        }.items():
            assert sum(pinned[task_id].values()) == expected, task_id

    @pytest.mark.parametrize("task_id", ["bplustree", "imapclient", "pyjwt", "simpy"])
    def test_pinned_totals_exceed_the_static_count(self, task_id: str):
        """Proof the pins carry collected cases, not `def test_*` counts.

        The static count for bplustree is 59 against 357 collected cases; a pin
        that had silently regressed to static counting would fail here.
        """
        assert pinned_total(task_id) >= 100
