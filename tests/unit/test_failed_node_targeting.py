"""PromptFeedbackEdit must target the actual failed node."""

from __future__ import annotations

from orchestra.backends.base import AgentRunStatus
from orchestra.backends.catalog import KNOWN_BACKEND_CAPABILITIES
from orchestra.control.fast_loop.candidate_generator import RuleBasedLocalCandidateGenerator
from orchestra.control.fast_loop.diagnosis import diagnose_subtask_failure
from orchestra.control.fast_loop.schemas import FastLoopBudget
from orchestra.control.task_state import (
    SubtaskFailureReason,
    SubtaskState,
    SubtaskStatus,
)
from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec
from orchestra.ir.edges import EdgeSpec
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import (
    AgentNodeSpec,
    StructuredLLMBackendConfig,
)
from orchestra.runtime.state import NodeExecutionResult


def _multi_agent_graph() -> OrchestraGraph:
    return OrchestraGraph(
        graph_id="multi",
        version="1",
        initial_artifact_slots={"problem": "ProblemArtifact"},
        final_output_slot="out",
        nodes=[
            AgentNodeSpec(
                node_id="planner",
                contract_id="codex_implementer",
                backend=StructuredLLMBackendConfig(),
                input_slots={"problem": "ProblemArtifact"},
                output_slots={"plan": "ProblemArtifact"},
            ),
            AgentNodeSpec(
                node_id="coder",
                contract_id="codex_implementer",
                backend=StructuredLLMBackendConfig(),
                input_slots={"plan": "ProblemArtifact"},
                output_slots={"repository_change": "RepositoryChangeArtifact"},
            ),
            AgentNodeSpec(
                node_id="verifier",
                contract_id="codex_implementer",
                backend=StructuredLLMBackendConfig(),
                input_slots={"repository_change": "RepositoryChangeArtifact"},
                output_slots={"out": "RepositoryChangeArtifact"},
            ),
        ],
        edges=[
            EdgeSpec(
                edge_id="p2c",
                source_node="planner",
                source_output="plan",
                destination_node="coder",
                destination_input="plan",
            ),
            EdgeSpec(
                edge_id="c2v",
                source_node="coder",
                source_output="repository_change",
                destination_node="verifier",
                destination_input="repository_change",
            ),
        ],
    )


def test_feedback_targets_failed_coder_not_planner():
    graph = _multi_agent_graph()
    sub = SubtaskState(
        spec=SubtaskSpec(
            subtask_id="s",
            title="t",
            objective="o",
            keystone_harness_id="repository_test_harness",
            local_graph_template="configs/graphs/codex_single_implementer.yaml",
            budget=BudgetSpec(),
        ),
        status=SubtaskStatus.FAILED,
        failure_reason=SubtaskFailureReason.MODEL,
        failure_message="coder blew up",
    )
    diagnosis = diagnose_subtask_failure(
        subtask_state=sub,
        graph=graph,
        node_results={
            "planner": NodeExecutionResult(node_id="planner", succeeded=True),
            "coder": NodeExecutionResult(
                node_id="coder",
                succeeded=False,
                error="model failure",
                backend_status=AgentRunStatus.MODEL_FAILURE,
            ),
        },
    )
    assert diagnosis.primary_failed_node_id == "coder"
    cands = RuleBasedLocalCandidateGenerator().generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
    )
    feedback = next(c for c in cands if c.candidate_id == "cand_feedback")
    assert any(
        e.type == "prompt_feedback" and e.node_id == "coder" for e in feedback.edits
    )
    assert not any(
        e.type == "prompt_feedback" and e.node_id == "planner" for e in feedback.edits
    )


def test_feedback_targets_verifier_on_contract_failure():
    graph = _multi_agent_graph()
    sub = SubtaskState(
        spec=SubtaskSpec(
            subtask_id="s",
            title="t",
            objective="o",
            keystone_harness_id="repository_test_harness",
            local_graph_template="configs/graphs/codex_single_implementer.yaml",
            budget=BudgetSpec(),
        ),
        status=SubtaskStatus.FAILED,
        failure_reason=SubtaskFailureReason.OUTPUT_CONTRACT,
        failure_message="bad schema",
    )
    diagnosis = diagnose_subtask_failure(
        subtask_state=sub,
        graph=graph,
        node_results={
            "planner": NodeExecutionResult(node_id="planner", succeeded=True),
            "coder": NodeExecutionResult(node_id="coder", succeeded=True),
            "verifier": NodeExecutionResult(
                node_id="verifier",
                succeeded=False,
                error="output contract",
                backend_status=AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
            ),
        },
    )
    assert diagnosis.primary_failed_node_id == "verifier"
    cands = RuleBasedLocalCandidateGenerator().generate(
        graph=graph,
        diagnosis=diagnosis,
        budget=FastLoopBudget(),
        capabilities=KNOWN_BACKEND_CAPABILITIES,
    )
    feedback = next(c for c in cands if c.candidate_id == "cand_feedback")
    assert any(
        e.type == "prompt_feedback" and e.node_id == "verifier" for e in feedback.edits
    )
