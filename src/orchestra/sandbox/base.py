from abc import ABC, abstractmethod

from orchestra.sandbox.result import SandboxExecutionResult
from orchestra.schemas.task import AgentVisibleLCBTask


class SandboxBackend(ABC):
    @abstractmethod
    async def evaluate_public(
        self,
        *,
        task: AgentVisibleLCBTask,
        code: str,
        timeout_seconds: float,
    ) -> SandboxExecutionResult: ...


# Backwards-compatible name for internal imports. SandboxBackend is canonical.
CodeSandbox = SandboxBackend
