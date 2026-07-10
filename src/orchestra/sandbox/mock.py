import asyncio

from orchestra.sandbox.base import CodeSandbox
from orchestra.sandbox.result import SandboxExecutionResult, VisibleTestResult
from orchestra.schemas.artifacts import ProblemArtifact


class MockSandbox(CodeSandbox):
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.active = 0
        self.max_active = 0

    async def evaluate_public(
        self, problem: ProblemArtifact, code: str
    ) -> SandboxExecutionResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            correct = "CORRECT_SOLUTION" in code or "print(input())" in code
            results = [
                VisibleTestResult(
                    index=index,
                    passed=correct,
                    actual_output=example.output if correct else "wrong",
                )
                for index, example in enumerate(problem.public_examples)
            ]
            return SandboxExecutionResult(
                compiled="SYNTAX_ERROR" not in code,
                passed_count=sum(item.passed for item in results),
                total_count=len(results),
                runtime_errors=int("RUNTIME_ERROR" in code),
                timeouts=int("TIMEOUT" in code),
                per_test_visible_results=results,
                duration_ms=int(self.delay_seconds * 1000),
            )
        finally:
            self.active -= 1
