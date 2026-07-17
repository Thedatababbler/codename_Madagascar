"""Checkpoint writes must be atomic under concurrent savers."""

from __future__ import annotations

import asyncio

import pytest

from orchestra.control.task_state import TaskExecutionState
from orchestra.decomposition.fallback import build_single_subtask_plan
from orchestra.runtime.task_checkpoint import TaskCheckpointStore


@pytest.mark.asyncio
async def test_concurrent_checkpoint_write_is_atomic(tmp_path):
    store = TaskCheckpointStore(tmp_path)
    plan = build_single_subtask_plan(
        task_id="atomic",
        objective="x",
        local_graph_template="configs/graphs/codex_single_implementer.yaml",
        keystone_harness_id="repository_test_harness",
    )
    base = TaskExecutionState.from_plan(plan)

    async def writer(i: int) -> None:
        state = base.model_copy(deep=True)
        state.state_version = i
        state.subtasks["main"].local_revision = i
        await store.save(state)

    await asyncio.gather(*(writer(i) for i in range(1, 21)))
    loaded = await store.load("atomic")
    assert loaded is not None
    assert 1 <= loaded.state_version <= 20
    # File must be valid JSON / parseable TaskExecutionState (not torn write).
    assert loaded.task_id == "atomic"
