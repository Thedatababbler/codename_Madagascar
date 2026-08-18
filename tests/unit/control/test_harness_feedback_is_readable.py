"""What a failed gate is allowed to tell the next attempt.

The harness prints a machine-readable score line: one JSON object naming every
stage and every failed test, the authored suite included. The selector parses
it, which is what it is for. It also lands in the harness artifact's captured
output, and from there in the failure message that a repair attempt or the next
fast-loop candidate is prompted with — which would hand that candidate the
identity of the hidden tests it is about to be ranked on (EXP-20260811-01).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestra.control.failure import classify_subtask_outcome
from orchestra.control.task_state import SubtaskFailureReason, SubtaskStatus
from orchestra.harness.progress import MARKER
from orchestra.ir.artifacts import create_artifact
from orchestra.runtime.state import GraphExecutionResult, NodeStatus, RuntimeState
from orchestra.schemas.artifacts import RepositoryHarnessResultArtifact
from orchestra.storage.artifacts import FileArtifactStore

SCORE_LINE = (
    MARKER
    + " "
    + json.dumps(
        {
            "score": 0.6,
            "stages": [
                {
                    "stage": "spec_tests",
                    "passed_units": 2,
                    "total_units": 4,
                    "failed_tests": ["spec_tests/test_spec.py::test_add_sums"],
                }
            ],
        }
    )
)


async def _classify(tmp_path: Path, summary: str) -> tuple[SubtaskStatus, object, str | None]:
    store = FileArtifactStore(tmp_path)
    artifact = create_artifact(
        RepositoryHarnessResultArtifact(
            passed=False,
            exit_code=1,
            duration_ms=1,
            stdout_summary=summary,
            stderr_summary="",
        ),
        producer_node_id="repository_tests",
        task_id="t1",
    )
    await store.put(artifact)
    result = GraphExecutionResult(
        state=RuntimeState(
            run_id="r",
            task_id="t1",
            graph_id="g",
            graph_hash="h",
            contract_hash="c",
            node_status={"repository_tests": NodeStatus.SUCCEEDED},
            node_outputs={"repository_tests": {"result": artifact.artifact_id}},
        ),
        wall_latency_ms=0,
        sum_node_latency_ms=0,
        critical_path_latency_ms=0,
        parallel_node_count=0,
        concurrency_speedup=1.0,
        failed_node_count=0,
        skipped_node_count=0,
        checkpoint_count=0,
    )
    return await classify_subtask_outcome(result=result, artifact_store=store)


@pytest.mark.asyncio
async def test_the_hidden_tests_are_not_named_in_the_failure_message(
    tmp_path: Path,
) -> None:
    status, reason, message = await _classify(
        tmp_path, "FAIL milestone contracts: missing demo.Widget\n" + SCORE_LINE
    )

    assert (status, reason) == (SubtaskStatus.HARNESS_FAILED, SubtaskFailureReason.HARNESS)
    assert message == "FAIL milestone contracts: missing demo.Widget"
    assert "test_add_sums" not in (message or "")


@pytest.mark.asyncio
async def test_a_report_that_is_only_the_score_line_still_says_something(
    tmp_path: Path,
) -> None:
    """Stripping everything would leave a repairer with no failure at all."""
    _status, _reason, message = await _classify(tmp_path, SCORE_LINE)

    assert message == "repository harness reported passed=false"
