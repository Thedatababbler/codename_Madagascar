import time

from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.ir.nodes import SelectorNodeSpec
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult
from orchestra.schemas.artifacts import CodeArtifact, PublicHarnessResultArtifact


def _rank(result: PublicHarnessResultArtifact) -> tuple:
    return (
        result.pass_ratio,
        int(result.compile_success),
        -result.runtime_errors,
        -result.timeouts,
        -result.duration_ms,
    )


class SelectorExecutor:
    async def execute(
        self,
        node: SelectorNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        if node.selector_id != "deterministic_code_selector":
            raise ValueError(f"Unknown selector: {node.selector_id}")
        initial = PublicHarnessResultArtifact.model_validate(
            inputs["initial_result"].payload
        )
        repaired = PublicHarnessResultArtifact.model_validate(
            inputs["repaired_result"].payload
        )
        # Initial wins exact ties, as required by the Stage 1 fairness rule.
        selected = inputs["repaired_code"] if _rank(repaired) > _rank(initial) else inputs[
            "initial_code"
        ]
        payload = CodeArtifact.model_validate(selected.payload).model_copy(
            update={"source_node": node.node_id}
        )
        output_slot = next(iter(node.output_slots))
        artifact = create_artifact(
            payload,
            producer_node_id=node.node_id,
            task_id=context.task_id,
            parent_artifact_ids=[item.artifact_id for item in inputs.values()],
        )
        return NodeExecutionResult(
            node_id=node.node_id,
            succeeded=True,
            outputs={output_slot: artifact},
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
