"""What a harness report may say to an agent, once the suite is hidden.

Custody keeps the authored suite out of the workspace, but the report of custody
is itself an artifact, and every input artifact is rendered into the prompt as
JSON. Three of its fields describe the suite: the score line names each failed
test, the custody message gives the absolute path the suite was moved to, and
the changed-file list names its files. An agent that reads any of them has the
yardstick back.

The stored report keeps all of it — the selector ranks on those identities and a
human debugs from that output. Only the copy handed to a prompt is stripped.
"""

from __future__ import annotations

import json

from orchestra.harness.progress import MARKER, parse_progress, redact_hidden_suite

SCORE_LINE = MARKER + json.dumps(
    {
        "score": 0.71,
        "stages": [
            {"stage": "imports", "passed_units": 9, "total_units": 10, "weight": 0.3},
            {
                "stage": "spec_tests",
                "passed_units": 2,
                "total_units": 4,
                "weight": 0.4,
                "failed_tests": ["spec_tests/test_spec.py::test_add_sums"],
            },
        ],
        "furthest_stage": "spec_tests",
    }
)

REPORT = {
    "passed": False,
    "exit_code": 1,
    "duration_ms": 10,
    "stdout_summary": (
        "CUSTODY spec_tests -> /runner/harness/m1.spec_tests (3 file(s))\n"
        "FAILED check_tests/test_widget.py::test_value - AssertionError\n" + SCORE_LINE
    ),
    "stderr_summary": "",
    "changed_files": ["demo_pkg/core.py", "spec_tests/test_spec.py"],
    "score": 0.71,
    "stages": [
        {"stage": "imports", "passed_units": 9, "total_units": 10, "weight": 0.3},
        {
            "stage": "spec_tests",
            "passed_units": 2,
            "total_units": 4,
            "weight": 0.4,
            "failed_tests": ["spec_tests/test_spec.py::test_add_sums"],
        },
    ],
    "furthest_stage": "spec_tests",
}


def test_nothing_about_the_hidden_suite_survives() -> None:
    rendered = json.dumps(redact_hidden_suite(REPORT))

    assert "spec_tests" not in rendered
    assert "test_add_sums" not in rendered
    assert "m1.spec_tests" not in rendered


def test_the_visible_suite_still_reports_its_failures() -> None:
    """A repairer with no failures to read is a repairer that cannot repair."""
    redacted = redact_hidden_suite(REPORT)

    assert "FAILED check_tests/test_widget.py::test_value" in redacted["stdout_summary"]
    assert {"imports"} == {stage["stage"] for stage in redacted["stages"]}
    assert redacted["changed_files"] == ["demo_pkg/core.py"]
    assert redacted["passed"] is False


def test_the_stored_report_is_left_alone() -> None:
    """The selector ranks on the identities this strips, so it cannot mutate."""
    before = json.dumps(REPORT, sort_keys=True)
    redact_hidden_suite(REPORT)

    assert json.dumps(REPORT, sort_keys=True) == before
    score, stages, furthest = parse_progress(REPORT["stdout_summary"])
    assert score == 0.71
    assert furthest == "spec_tests"
    assert any(stage.failed_tests for stage in stages)


def test_a_report_from_a_task_with_no_authored_suite_is_unchanged() -> None:
    plain = {
        "passed": True,
        "exit_code": 0,
        "duration_ms": 1,
        "stdout_summary": "9 passed\n",
        "stderr_summary": "",
        "changed_files": ["demo_pkg/core.py"],
        "stages": [{"stage": "tests", "passed_units": 9, "total_units": 9}],
        "furthest_stage": "tests",
    }

    assert redact_hidden_suite(plain) == plain
