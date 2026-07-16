import time

from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.ir.nodes import TransformNodeSpec
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult
from orchestra.schemas.artifacts import (
    AlgorithmPlanArtifact,
    CodeArtifact,
    CombinedPlanArtifact,
    EdgeCaseArtifact,
    FinalCodeArtifact,
    RepairArtifact,
    RepositoryChangeArtifact,
    RepositoryHarnessResultArtifact,
)


class TransformExecutor:
    async def execute(
        self,
        node: TransformNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        if node.transform_id == "merge_analysis_artifacts":
            plan = AlgorithmPlanArtifact.model_validate(inputs["plan"].payload)
            edge = EdgeCaseArtifact.model_validate(inputs["edge_cases"].payload)
            payload = CombinedPlanArtifact(
                problem_summary=plan.problem_summary,
                algorithm=plan.algorithm,
                correctness_argument=plan.correctness_argument,
                time_complexity=plan.time_complexity,
                space_complexity=plan.space_complexity,
                data_structures=plan.data_structures,
                edge_cases=list(dict.fromkeys([*plan.edge_cases, *edge.edge_cases])),
                implementation_risks=list(
                    dict.fromkeys(
                        [
                            *edge.overflow_risks,
                            *edge.indexing_risks,
                            *edge.interface_concerns,
                            *edge.likely_failure_modes,
                        ]
                    )
                ),
            )
        elif node.transform_id == "freeze_code":
            code_envelope = inputs["code"]
            code = CodeArtifact.model_validate(code_envelope.payload)
            payload = FinalCodeArtifact(
                code=code.code, source_artifact_id=code_envelope.artifact_id
            )
        elif node.transform_id == "identity_code":
            payload = CodeArtifact.model_validate(inputs["code"].payload).model_copy(
                update={"source_node": node.node_id}
            )
        elif node.transform_id == "repair_to_code":
            repair = RepairArtifact.model_validate(inputs["repair"].payload)
            payload = CodeArtifact(code=repair.revised_code, source_node=node.node_id)
        elif node.transform_id == "freeze_repository_change":
            change_envelope = inputs["repository_change"]
            change = RepositoryChangeArtifact.model_validate(change_envelope.payload)
            gate = inputs.get("gate")
            if gate is not None:
                result = RepositoryHarnessResultArtifact.model_validate(gate.payload)
                if not result.passed:
                    return NodeExecutionResult(
                        node_id=node.node_id,
                        succeeded=False,
                        error="repository harness gate failed",
                        latency_ms=int((time.perf_counter() - started) * 1000),
                    )
            payload = change.model_copy(update={"source_node": node.node_id})
        else:
            raise ValueError(f"Unknown transform: {node.transform_id}")
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
