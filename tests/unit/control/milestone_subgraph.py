"""The subgraph shape the builder actually emits, for tests that rewire it.

Every edit and every selection rule in the fast loop is about a graph of this
shape: agents chained by ``upstream_change``, an acceptance harness reading the
last agent's output, and a freeze transform gated on the harness passing. A
two-node toy would let a broken edit pass.
"""

from __future__ import annotations

from orchestra.backends.base import ModelSpec, OutputContract
from orchestra.ir.edges import EdgeCondition, EdgeSpec
from orchestra.ir.graph import OrchestraGraph
from orchestra.ir.nodes import (
    AgentNodeSpec,
    CodexSDKBackendConfig,
    HarnessNodeSpec,
    TransformNodeSpec,
)

AUTHOR = "agent_1_author_contract_author"
REVIEWER = "agent_2_reviewer_spec_auditor"
FIXER = "agent_3_fixer_implementer"


def agent(node_id: str, *, edits: bool, upstream: bool) -> AgentNodeSpec:
    slots: dict[str, str] = {"problem": "ProblemArtifact"}
    if upstream:
        slots["upstream_change"] = "RepositoryChangeArtifact"
    return AgentNodeSpec(
        node_id=node_id,
        contract_id=f"contract_{node_id}",
        backend=CodexSDKBackendConfig(require_git_diff=edits, max_steps=1),
        model=ModelSpec(provider="openai_compatible", name="gpt-5.4", max_tokens=8192),
        output_contract=OutputContract(
            parser_id="repository_change", output_schema="RepositoryChangeArtifact"
        ),
        input_slots=slots,
        output_slots={"repository_change": "RepositoryChangeArtifact"},
        timeout_seconds=1200.0,
    )


def review_then_fix_graph() -> OrchestraGraph:
    return OrchestraGraph(
        graph_id="rb_dynamic_freeze",
        version="1.0",
        initial_artifact_slots={"problem": "ProblemArtifact"},
        final_output_slot="final_change",
        nodes=[
            agent(AUTHOR, edits=True, upstream=False),
            agent(REVIEWER, edits=False, upstream=True),
            agent(FIXER, edits=True, upstream=True),
            HarnessNodeSpec(
                node_id="repository_tests",
                harness_id="repository_test_harness",
                visibility="public",
                command=["true"],
                timeout_seconds=900,
                input_slots={"repository_change": "RepositoryChangeArtifact"},
                output_slots={"result": "RepositoryHarnessResultArtifact"},
            ),
            TransformNodeSpec(
                node_id="freeze_change",
                transform_id="freeze_repository_change",
                input_slots={
                    "repository_change": "RepositoryChangeArtifact",
                    "gate": "RepositoryHarnessResultArtifact",
                },
                output_slots={"final_change": "RepositoryChangeArtifact"},
            ),
        ],
        edges=[
            EdgeSpec(
                edge_id="link_author_to_reviewer",
                source_node=AUTHOR,
                source_output="repository_change",
                destination_node=REVIEWER,
                destination_input="upstream_change",
            ),
            EdgeSpec(
                edge_id="link_reviewer_to_fixer",
                source_node=REVIEWER,
                source_output="repository_change",
                destination_node=FIXER,
                destination_input="upstream_change",
            ),
            EdgeSpec(
                edge_id="agent_to_tests",
                source_node=FIXER,
                source_output="repository_change",
                destination_node="repository_tests",
                destination_input="repository_change",
            ),
            EdgeSpec(
                edge_id="agent_to_freeze",
                source_node=FIXER,
                source_output="repository_change",
                destination_node="freeze_change",
                destination_input="repository_change",
            ),
            EdgeSpec(
                edge_id="tests_pass_to_freeze",
                source_node="repository_tests",
                source_output="result",
                destination_node="freeze_change",
                destination_input="gate",
                condition=EdgeCondition(source_field="passed", operator="is_true"),
            ),
        ],
    )
