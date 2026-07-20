from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    subtask_id: str | None = None
    workspace_ref: str | None = None
    candidate_id: str | None = None
    attempt_id: int | None = None
    # Mapping node_id -> NodeSessionDirective (or dict payload).
    node_session_directives: dict[str, Any] = field(default_factory=dict)


class RuntimeBackend(ABC):
    @abstractmethod
    async def execute(
        self,
        *,
        graph: CompiledGraph,
        initial_artifacts: ArtifactBundle,
        context: RunContext,
    ) -> GraphExecutionResult: ...
