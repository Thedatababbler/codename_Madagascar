"""Payload projection field selection and truncation."""

from __future__ import annotations

from orchestra.communication.payload import PayloadContract
from orchestra.communication.projection import estimate_tokens, project_payload
from orchestra.ir.artifacts import create_artifact
from orchestra.schemas.artifacts import FinalAnswerArtifact


def test_projection_keeps_required_fields_only():
    source = create_artifact(
        FinalAnswerArtifact(answer="hello world " * 50, source_node="s1"),
        producer_node_id="s1",
        task_id="t",
    )
    contract = PayloadContract(
        payload_id="p1",
        source_subtask_id="s1",
        target_subtask_id="s2",
        artifact_type="FinalAnswerArtifact",
        required_fields=["answer"],
        max_tokens=10_000,
    )
    result = project_payload(contract=contract, source=source, task_id="t")
    assert result.payload_id == "p1"
    assert result.source_artifact_id == source.artifact_id
    assert "answer" in result.omitted_fields or result.estimated_tokens > 0
    assert result.projected_artifact.parent_artifact_ids == (source.artifact_id,)


def test_projection_truncates_when_over_budget():
    source = create_artifact(
        FinalAnswerArtifact(answer="x" * 5000, source_node="s1"),
        producer_node_id="s1",
        task_id="t",
    )
    contract = PayloadContract(
        payload_id="p1",
        source_subtask_id="s1",
        target_subtask_id="s2",
        artifact_type="FinalAnswerArtifact",
        required_fields=["answer"],
        max_tokens=40,
    )
    result = project_payload(contract=contract, source=source, task_id="t")
    assert result.truncated is True
    assert result.final_estimated_tokens <= contract.max_tokens
    assert result.estimated_tokens <= contract.max_tokens
    assert result.estimated_tokens <= estimate_tokens({"answer": "x" * 5000})
