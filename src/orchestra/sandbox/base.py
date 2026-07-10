from abc import ABC, abstractmethod

from orchestra.sandbox.result import SandboxExecutionResult
from orchestra.schemas.artifacts import ProblemArtifact


class CodeSandbox(ABC):
    @abstractmethod
    async def evaluate_public(
        self, problem: ProblemArtifact, code: str
    ) -> SandboxExecutionResult: ...
