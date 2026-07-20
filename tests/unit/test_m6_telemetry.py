"""M6-A telemetry: WaveCommitter usage persistence and pricing quality."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from orchestra.control.backend_usage import (
    ModelPrice,
    PricingRegistry,
    append_usage_records,
    collect_usage_from_graph_result,
    derive_cost_usd,
    exception_usage_record,
)
from orchestra.ir.artifacts import create_artifact
from orchestra.ir.graph import OrchestraGraph
from orchestra.llm.usage import LLMUsage
from orchestra.runtime.committer import WaveCommitter
from orchestra.runtime.scheduler import Scheduler
from orchestra.runtime.state import (
    GraphExecutionResult,
    NodeExecutionResult,
    NodeStatus,
    RuntimeState,
)
from orchestra.schemas.artifacts import FinalAnswerArtifact
from orchestra.storage.artifacts import FileArtifactStore


def _minimal_graph() -> OrchestraGraph:
    # Load a real small graph used elsewhere in the repo.
    from orchestra.ir.graph import load_graph

    return load_graph("configs/graphs/codex_single_implementer.yaml")


def _runtime_state(graph: OrchestraGraph) -> RuntimeState:
    return RuntimeState(
        run_id="r",
        task_id="t",
        graph_id=graph.graph_id,
        graph_hash=graph.content_hash,
        contract_hash="c",
        node_status={n.node_id: NodeStatus.RUNNING for n in graph.nodes},
    )


@pytest.mark.asyncio
async def test_wave_committer_persists_node_usage(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    graph = _minimal_graph()
    node_id = graph.nodes[0].node_id
    art = create_artifact(
        FinalAnswerArtifact(answer="ok", source_node=node_id),
        producer_node_id=node_id,
        task_id="t",
    )
    result = NodeExecutionResult(
        node_id=node_id,
        succeeded=True,
        outputs={graph.final_output_slot: art},
        latency_ms=42,
        usage=LLMUsage(prompt_tokens=11, completion_tokens=7, cached_tokens=2),
        backend_id="fake",
        backend_metadata={"model_name": "fake-test-model"},
    )
    committer = WaveCommitter(store, Scheduler(max_parallel_nodes=2))
    state, *_ = await committer.commit_wave(
        graph=graph, previous_state=_runtime_state(graph), results=[result]
    )
    snap = state.node_usage_snapshots[node_id]
    assert snap.prompt_tokens == 11
    assert snap.completion_tokens == 7
    assert snap.latency_ms == 42
    assert state.node_latencies_ms[node_id] == 42


@pytest.mark.asyncio
async def test_node_usage_reads_real_prompt_completion_tokens(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    graph = _minimal_graph()
    node_id = graph.nodes[0].node_id
    art = create_artifact(
        FinalAnswerArtifact(answer="ok", source_node=node_id),
        producer_node_id=node_id,
        task_id="t",
    )
    result = NodeExecutionResult(
        node_id=node_id,
        succeeded=True,
        outputs={graph.final_output_slot: art},
        latency_ms=10,
        usage=LLMUsage(prompt_tokens=100, completion_tokens=20),
        backend_id="fake",
    )
    state, *_ = await WaveCommitter(store, Scheduler(max_parallel_nodes=2)).commit_wave(
        graph=graph, previous_state=_runtime_state(graph), results=[result]
    )
    gre = GraphExecutionResult(
        state=state,
        wall_latency_ms=10,
        sum_node_latency_ms=10,
        critical_path_latency_ms=10,
        parallel_node_count=1,
        concurrency_speedup=1.0,
        failed_node_count=0,
        skipped_node_count=0,
        checkpoint_count=1,
    )
    records = collect_usage_from_graph_result(
        task_id="t", subtask_id="s1", attempt_id=1, result=gre
    )
    assert records[0].prompt_tokens == 100
    assert records[0].completion_tokens == 20


@pytest.mark.asyncio
async def test_missing_tokens_remain_none(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    graph = _minimal_graph()
    node_id = graph.nodes[0].node_id
    result = NodeExecutionResult(
        node_id=node_id,
        succeeded=False,
        error="boom",
        latency_ms=5,
        usage=LLMUsage(),
        backend_id="fake",
    )
    state, *_ = await WaveCommitter(store, Scheduler(max_parallel_nodes=2)).commit_wave(
        graph=graph, previous_state=_runtime_state(graph), results=[result]
    )
    snap = state.node_usage_snapshots[node_id]
    assert snap.prompt_tokens is None
    assert snap.completion_tokens is None


@pytest.mark.asyncio
async def test_zero_tokens_are_distinct_from_unavailable(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    graph = _minimal_graph()
    node_id = graph.nodes[0].node_id
    art = create_artifact(
        FinalAnswerArtifact(answer="ok", source_node=node_id),
        producer_node_id=node_id,
        task_id="t",
    )
    result = NodeExecutionResult(
        node_id=node_id,
        succeeded=True,
        outputs={graph.final_output_slot: art},
        latency_ms=1,
        usage=LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        backend_id="fake",
    )
    state, *_ = await WaveCommitter(store, Scheduler(max_parallel_nodes=2)).commit_wave(
        graph=graph, previous_state=_runtime_state(graph), results=[result]
    )
    assert state.node_usage_snapshots[node_id].prompt_tokens == 0
    assert state.node_usage_snapshots[node_id].completion_tokens == 0


@pytest.mark.asyncio
async def test_per_node_latency_not_graph_latency_fallback(tmp_path: Path):
    store = FileArtifactStore(tmp_path)
    graph = _minimal_graph()
    node_id = graph.nodes[0].node_id
    art = create_artifact(
        FinalAnswerArtifact(answer="ok", source_node=node_id),
        producer_node_id=node_id,
        task_id="t",
    )
    result = NodeExecutionResult(
        node_id=node_id,
        succeeded=True,
        outputs={graph.final_output_slot: art},
        latency_ms=123,
        usage=LLMUsage(prompt_tokens=1, completion_tokens=1),
        backend_id="fake",
    )
    state, *_ = await WaveCommitter(store, Scheduler(max_parallel_nodes=2)).commit_wave(
        graph=graph, previous_state=_runtime_state(graph), results=[result]
    )
    gre = GraphExecutionResult(
        state=state,
        wall_latency_ms=9999,
        sum_node_latency_ms=123,
        critical_path_latency_ms=123,
        parallel_node_count=1,
        concurrency_speedup=1.0,
        failed_node_count=0,
        skipped_node_count=0,
        checkpoint_count=1,
    )
    started = datetime(2026, 1, 1, tzinfo=UTC)
    finished = datetime(2026, 1, 1, 0, 0, 9, tzinfo=UTC)
    rec = collect_usage_from_graph_result(
        task_id="t",
        subtask_id="s1",
        attempt_id=1,
        result=gre,
        started_at=started,
        finished_at=finished,
    )[0]
    assert rec.latency_seconds == pytest.approx(0.123)


def test_exception_call_records_exact_call_and_latency():
    started = datetime(2026, 1, 1, tzinfo=UTC)
    finished = datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC)
    rec = exception_usage_record(
        task_id="t",
        subtask_id="s1",
        attempt_id=3,
        started_at=started,
        finished_at=finished,
        status="exception",
        accounting_source="test",
    )
    assert rec.latency_seconds == 2.0
    assert rec.prompt_tokens is None
    assert rec.estimated_cost_usd is None
    assert rec.cost_quality == "unavailable"


def test_exception_call_marks_tokens_cost_unavailable():
    now = datetime.now(UTC)
    rec = exception_usage_record(
        task_id="t",
        subtask_id="s1",
        attempt_id=1,
        started_at=now,
        finished_at=now,
        status="timeout",
        accounting_source="test",
    )
    assert rec.prompt_tokens is None
    assert rec.completion_tokens is None
    assert rec.cost_quality == "unavailable"


def test_usage_resume_deduplicates():
    now = datetime.now(UTC)
    a = exception_usage_record(
        task_id="t",
        subtask_id="s1",
        attempt_id=1,
        started_at=now,
        finished_at=now,
        status="x",
        accounting_source="a",
    )
    merged = append_usage_records([a], [a])
    assert len(merged) == 1


def test_unknown_model_price_does_not_invent_cost():
    cost, quality = derive_cost_usd(
        prompt_tokens=10,
        completion_tokens=5,
        cached_tokens=0,
        model_name="unknown-model",
        provider_cost_usd=None,
        pricing=PricingRegistry(pricing_version="t", models={}),
    )
    assert cost is None
    assert quality == "unavailable"


def test_configured_price_produces_derived_cost():
    pricing = PricingRegistry(
        pricing_version="t",
        models={
            "fake-test-model": ModelPrice(
                input_per_million_usd=0.15,
                output_per_million_usd=0.60,
            )
        },
    )
    cost, quality = derive_cost_usd(
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
        cached_tokens=0,
        model_name="fake-test-model",
        provider_cost_usd=None,
        pricing=pricing,
    )
    assert quality == "derived"
    assert cost == pytest.approx(0.75)


def test_fast_loop_candidate_usage_recorded():
    # Structural: exception + graph collectors both produce usage_ids with candidate.
    now = datetime.now(UTC)
    rec = exception_usage_record(
        task_id="t",
        subtask_id="s1",
        attempt_id=2,
        candidate_id="cand-1",
        started_at=now,
        finished_at=now,
        status="exception",
        accounting_source="fast_loop_candidate_exception",
    )
    assert rec.candidate_id == "cand-1"
    assert "cand-1" in rec.usage_id


def test_fast_loop_infra_retry_usage_recorded():
    now = datetime.now(UTC)
    rec = exception_usage_record(
        task_id="t",
        subtask_id="s1",
        attempt_id=1,
        candidate_id=None,
        started_at=now,
        finished_at=now,
        status="infra_failed",
        accounting_source="ready_scheduler_exception",
    )
    assert rec.accounting_source.endswith("exception")
