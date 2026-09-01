"""LLM diagnosis classifies; it does not invent edits, and it fails closed."""

from __future__ import annotations

import json
from pathlib import Path

from orchestra.control.fast_loop.llm_diagnosis import (
    ACCOUNTING_SOURCE,
    DiagnosisCompletion,
    DiagnosisConfig,
    build_diagnosis_prompt,
    refine_diagnosis,
)
from orchestra.control.fast_loop.schemas import FailureDiagnosis
from orchestra.control.task_state import (
    SubtaskAttempt,
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec
from orchestra.ir.graph import load_graph
from orchestra.roles.pool import load_role_pool

GRAPH = "configs/graphs/codex_single_implementer.yaml"


class _Client:
    def __init__(self, content: str, *, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls = 0

    def complete(self, *, model: str, messages: list[dict[str, str]]) -> DiagnosisCompletion:
        self.calls += 1
        self.messages = messages
        if self.error is not None:
            raise self.error
        return DiagnosisCompletion(
            content=self.content,
            prompt_tokens=12,
            completion_tokens=8,
            model=model,
        )


def _lookup(**overrides) -> FailureDiagnosis:
    payload = dict(
        reason=SubtaskFailureReason.HARNESS,
        retryable=True,
        concise_feedback="FAIL spec_tests: test_merge_keeps_the_later_timestamp",
        furthest_stage="spec_tests",
        behaviour_failures=["test_merge_keeps_the_later_timestamp"],
        primary_failed_node_id="coder",
        failed_node_ids=["coder"],
    )
    payload.update(overrides)
    return FailureDiagnosis(**payload)


def _sub() -> SubtaskState:
    return SubtaskState(
        spec=SubtaskSpec(
            subtask_id="s1",
            title="t",
            objective="o",
            dependencies=[],
            keystone_harness_id="repository_test_harness",
            local_graph_template=GRAPH,
            budget=BudgetSpec(max_llm_calls=1, max_steps=1, timeout_seconds=60),
        ),
        status=SubtaskStatus.HARNESS_FAILED,
        failure_reason=SubtaskFailureReason.HARNESS,
        failure_message="FAIL spec_tests: test_merge_keeps_the_later_timestamp",
        attempts=[
            SubtaskAttempt(
                attempt_id=1,
                status=SubtaskStatus.HARNESS_FAILED,
                metadata={
                    "furthest_stage": "spec_tests",
                    "harness_stages": [
                        {
                            "stage": "spec_tests",
                            "passed_units": 3,
                            "total_units": 4,
                            "failed_tests": ["test_merge_keeps_the_later_timestamp"],
                        }
                    ],
                },
            )
        ],
    )


def _classify(content: dict, **kwargs):
    return refine_diagnosis(
        lookup=kwargs.pop("lookup", _lookup()),
        graph=load_graph(GRAPH),
        subtask_state=_sub(),
        config=kwargs.pop("config", DiagnosisConfig(mode="llm", min_confidence=0.5)),
        client=_Client(json.dumps(content)),
        pool=load_role_pool("configs/roles"),
        task_id="t",
        subtask_id="s1",
        attempt_id=1,
        **kwargs,
    )


def test_a_confident_class_is_applied_and_accounted() -> None:
    diagnosis, call = _classify(
        {
            "failure_class": "functional",
            "confidence": 0.9,
            "target_node_id": "",
            "rationale": "named tests failed after the structure passed",
            "evidence": ["spec_tests"],
        }
    )
    assert call.applied is True
    assert diagnosis.failure_class == "functional"
    assert diagnosis.diagnosis_source == "llm"
    assert diagnosis.diagnosis_confidence == 0.9
    assert call.usage is not None
    assert call.usage.accounting_source == ACCOUNTING_SOURCE
    assert call.usage.prompt_tokens == 12


def test_low_confidence_keeps_the_lookup() -> None:
    diagnosis, call = _classify(
        {
            "failure_class": "design",
            "confidence": 0.2,
            "target_node_id": "",
            "rationale": "guessing",
            "evidence": [],
        }
    )
    assert call.applied is False
    assert "low_confidence" in call.fallback_reason
    assert diagnosis.diagnosis_source == "lookup"
    assert diagnosis.failure_class == ""


def test_an_unknown_class_falls_back() -> None:
    diagnosis, call = _classify(
        {
            "failure_class": "vibes",
            "confidence": 0.9,
            "target_node_id": "",
            "rationale": "no",
            "evidence": [],
        }
    )
    assert call.applied is False
    assert diagnosis.diagnosis_source == "lookup"


def test_a_missing_model_does_not_fail_the_milestone() -> None:
    diagnosis, call = refine_diagnosis(
        lookup=_lookup(),
        graph=load_graph(GRAPH),
        subtask_state=_sub(),
        config=DiagnosisConfig(mode="llm"),
        client=_Client("", error=RuntimeError("OPENAI_API_KEY is missing")),
        pool=load_role_pool("configs/roles"),
        task_id="t",
        subtask_id="s1",
        attempt_id=1,
    )
    assert call.applied is False
    assert diagnosis.retryable is True
    assert diagnosis.diagnosis_source == "lookup"
    assert call.usage is not None
    assert call.usage.accounting_source == ACCOUNTING_SOURCE


def test_deterministic_mode_does_not_call_the_model() -> None:
    client = _Client(json.dumps({"failure_class": "budget", "confidence": 1}))
    diagnosis, call = refine_diagnosis(
        lookup=_lookup(),
        graph=load_graph(GRAPH),
        subtask_state=_sub(),
        config=DiagnosisConfig(mode="deterministic"),
        client=client,
        pool=load_role_pool("configs/roles"),
    )
    assert client.calls == 0
    assert call.applied is False
    assert diagnosis.diagnosis_source == "lookup"


def test_the_prompt_sees_the_gate_and_never_a_held_out_suite(tmp_path: Path) -> None:
    lookup = _lookup(
        concise_feedback="FAIL spec_tests: test_a (see proj_with_test/hidden)"
    )
    prompt = build_diagnosis_prompt(
        lookup=lookup,
        graph=load_graph(GRAPH),
        subtask_state=_sub(),
        remaining={"candidates": 3},
        playbook_history=[],
        pool=load_role_pool("configs/roles"),
    )
    assert "test_merge_keeps_the_later_timestamp" in prompt
    assert "spec_tests" in prompt
    assert "proj_with_test" not in prompt
    assert "hidden_test" not in prompt.lower()


def test_the_call_is_written_beside_the_candidates(tmp_path: Path) -> None:
    _, call = _classify(
        {
            "failure_class": "functional",
            "confidence": 0.8,
            "target_node_id": "",
            "rationale": "ok",
            "evidence": [],
        },
        artifact_dir=tmp_path,
    )
    assert call.applied is True
    payload = json.loads((tmp_path / "s1_1.json").read_text(encoding="utf-8"))
    assert payload["applied"] is True
    assert payload["final_class"] == "functional"
    assert payload["temperature"] == 0
    assert "You classify why a repository milestone failed" in payload["prompt"]["system"]
