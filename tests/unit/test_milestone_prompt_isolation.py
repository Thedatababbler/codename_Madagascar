"""Milestone context reaches agents through prompts, not workspace files."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from orchestra.control.ready_scheduler import (
    ReadySubtaskScheduler,
    _with_prompt_prelude,
)
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.ir.contracts import AgentContract
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.realbench.workspace_memory import (
    append_changelog_entry,
    memory_dir_for_run,
)
from orchestra.runtime.backend import RunContext
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores

GRAPH = "configs/graphs/codex_realbench_public_integration.yaml"


def test_prelude_carries_brief_and_prior_milestone_memory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    append_changelog_entry(
        memory_dir_for_run(run_dir),
        subtask_id="freeze_index",
        role="implementation",
        changed_files=["xproj/index.py"],
        revision="abc123",
    )

    prelude = ReadySubtaskScheduler._milestone_prompt_prelude(  # noqa: SLF001
        subtask_metadata={"milestone_brief": "# Milestone `implement_accessors`"},
        run_dir=run_dir,
    )

    assert "implement_accessors" in prelude
    assert "freeze_index" in prelude
    assert "xproj/index.py" in prelude


def test_memory_carries_what_the_committing_agent_said_it_decided(
    tmp_path: Path,
) -> None:
    """A file list says which modules moved, never what was settled inside them.

    The changelog used to record a fixed caption, so a later milestone learned
    that `serializer.py` had changed and had to re-derive the format from the
    diff. The committing agent's own closing text is the cheapest statement of
    that, and it already rides along on the frozen change artifact.
    """
    run_dir = tmp_path / "run"
    account = (
        "Froze the record layout: 8-byte big-endian length prefix followed by a "
        "varint payload. Callers must use Entry.from_bytes rather than slicing."
    )
    produced = [
        ArtifactEnvelope(
            artifact_id="a1",
            artifact_type="RepositoryChangeArtifact",
            producer_node_id="freeze_change",
            task_id="t",
            created_at=datetime.now(UTC),
            payload={
                "workspace_ref": "ws",
                "thread_id": "th",
                "changed_files": ["pkg/serializer.py"],
                "patch": "diff",
                "final_response": account,
            },
            content_hash="h",
        )
    ]

    ReadySubtaskScheduler._append_milestone_memory(  # noqa: SLF001
        run_dir=run_dir,
        subtask_id="freeze_records",
        role="implementation",
        change_set=None,
        revision="abc123",
        agent_account=ReadySubtaskScheduler._committing_agent_account(  # noqa: SLF001
            produced
        ),
    )
    prelude = ReadySubtaskScheduler._milestone_prompt_prelude(  # noqa: SLF001
        subtask_metadata={"milestone_brief": "# Milestone `build_tree`"},
        run_dir=run_dir,
    )

    assert "8-byte big-endian length prefix" in prelude
    assert "Entry.from_bytes" in prelude
    assert "canonical commit for subtask" not in prelude


def test_memory_falls_back_to_a_caption_when_the_agent_said_nothing(
    tmp_path: Path,
) -> None:
    """A backend that reports no closing text must not silence the entry."""
    run_dir = tmp_path / "run"

    ReadySubtaskScheduler._append_milestone_memory(  # noqa: SLF001
        run_dir=run_dir,
        subtask_id="freeze_records",
        role="implementation",
        change_set=None,
        revision="abc123",
        agent_account=ReadySubtaskScheduler._committing_agent_account([]),  # noqa: SLF001
    )

    prelude = ReadySubtaskScheduler._milestone_prompt_prelude(  # noqa: SLF001
        subtask_metadata={"milestone_brief": "# Milestone `build_tree`"},
        run_dir=run_dir,
    )
    assert "freeze_records" in prelude
    assert "canonical commit for subtask freeze_records" in prelude


def test_prelude_is_empty_without_a_brief(tmp_path: Path) -> None:
    assert (
        ReadySubtaskScheduler._milestone_prompt_prelude(  # noqa: SLF001
            subtask_metadata={}, run_dir=tmp_path
        )
        == ""
    )


def test_prelude_reaches_the_agent_messages(tmp_path: Path) -> None:
    graph = _with_prompt_prelude(load_graph(GRAPH), "MILESTONE CONTEXT HERE")
    node = next(n for n in graph.nodes if isinstance(n, AgentNodeSpec))
    contract = AgentContract(
        contract_id=node.contract_id,
        role="tester",
        system_prompt_template="system",
        user_prompt_template="artifacts: {artifacts_json}",
        model="m",
        temperature=0.0,
        max_tokens=128,
        timeout_seconds=30.0,
        allowed_tools=[],
        input_schema="ProblemArtifact",
        output_schema="RepositoryChangeArtifact",
        parser_id="repository_change",
        memory_scope="local",
    )
    executor = AgentNodeExecutor({node.contract_id: contract}, backends=None)
    limits = RuntimeLimits(
        max_parallel_benchmark_tasks=1,
        max_parallel_nodes_per_task=1,
        max_parallel_llm_calls=1,
        max_parallel_sandboxes=1,
        max_concurrent_subtasks=1,
    )
    context = RunContext(
        run_id="r",
        task_id="t",
        run_dir=tmp_path,
        limits=limits,
        semaphores=RuntimeSemaphores(limits),
        contract_hash="h",
    )

    request = executor._build_request(node, {}, context=context)  # noqa: SLF001

    roles = [m["role"] for m in request.messages]
    assert roles == ["system", "user", "user"]
    assert request.messages[1]["content"] == "MILESTONE CONTEXT HERE"


def test_prompt_prelude_is_excluded_from_graph_hash() -> None:
    base = load_graph(GRAPH)
    assert _with_prompt_prelude(base, "").content_hash == base.content_hash
    assert _with_prompt_prelude(base, "ctx").content_hash != base.content_hash
