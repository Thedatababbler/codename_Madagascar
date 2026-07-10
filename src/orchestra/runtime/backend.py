from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from orchestra.ir.artifacts import ArtifactBundle
from orchestra.ir.compiler import CompiledGraph
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.state import GraphExecutionResult


@dataclass(frozen=True)
class RunContext:
    run_id: str
    task_id: str
    run_dir: Path
    limits: RuntimeLimits
    semaphores: RuntimeSemaphores
    contract_hash: str
    allow_config_drift: bool = False


class RuntimeBackend(ABC):
    @abstractmethod
    async def execute(
        self,
        *,
        graph: CompiledGraph,
        initial_artifacts: ArtifactBundle,
        context: RunContext,
    ) -> GraphExecutionResult: ...
