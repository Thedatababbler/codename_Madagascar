import ast
import time

from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.ir.nodes import HarnessNodeSpec
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult
from orchestra.sandbox.base import CodeSandbox
from orchestra.sandbox.result import SandboxExecutionResult
from orchestra.schemas.artifacts import (
    CodeArtifact,
    ProblemArtifact,
    PublicHarnessResultArtifact,
    VisibleFailureSummary,
)


class HarnessNodeExecutor:
    def __init__(self, sandbox: CodeSandbox) -> None:
        self.sandbox = sandbox

    async def execute(
        self,
        node: HarnessNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        problem = ProblemArtifact.model_validate(inputs["problem"].payload)
        code = CodeArtifact.model_validate(inputs["code"].payload)
        try:
            ast.parse(code.code)
        except SyntaxError as exc:
            sandbox_result = SandboxExecutionResult(
                compiled=False,
                passed_count=0,
                total_count=len(problem.public_examples),
                runtime_errors=0,
                timeouts=0,
                stderr_summary=f"{exc.msg} at line {exc.lineno}",
                duration_ms=0,
            )
        else:
            async with context.semaphores.sandbox:
                sandbox_result = await self.sandbox.evaluate_public(problem, code.code)
        failed = next(
            (item for item in sandbox_result.per_test_visible_results if not item.passed),
            None,
        )
        example = problem.public_examples[failed.index] if failed else None
        total = sandbox_result.total_count
        payload = PublicHarnessResultArtifact(
            passed=sandbox_result.compiled
            and sandbox_result.passed_count == total
            and sandbox_result.runtime_errors == 0
            and sandbox_result.timeouts == 0,
            pass_ratio=sandbox_result.passed_count / total if total else 1.0,
            compile_success=sandbox_result.compiled,
            runtime_errors=sandbox_result.runtime_errors,
            timeouts=sandbox_result.timeouts,
            duration_ms=sandbox_result.duration_ms,
            failure_summary=VisibleFailureSummary(
                compile_error=(
                    sandbox_result.stderr_summary if not sandbox_result.compiled else None
                ),
                timeout=sandbox_result.timeouts > 0,
                public_input=example.input if example else None,
                expected_public_output=example.output if example else None,
                actual_public_output=failed.actual_output if failed else None,
                failed_public_test_index=failed.index if failed else None,
                errors=[sandbox_result.stderr_summary]
                if sandbox_result.stderr_summary
                else [],
            ),
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
