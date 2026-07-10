from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from orchestra.config import SandboxLimits
from orchestra.schemas.task import AgentVisibleLCBTask


class WorkerMode(StrEnum):
    PUBLIC = "public"
    PRIVATE_FINAL = "private_final"


class WorkerTestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str
    output: str
    testtype: str = "stdin"


class BaseWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: WorkerMode
    code: str
    per_test_timeout_seconds: float
    repository_path: str
    num_process_evaluate: Literal[1] = 1
    limits: SandboxLimits


class PublicWorkerRequest(BaseWorkerRequest):
    mode: Literal[WorkerMode.PUBLIC] = WorkerMode.PUBLIC
    task: AgentVisibleLCBTask


class PrivateFinalWorkerRequest(BaseWorkerRequest):
    mode: Literal[WorkerMode.PRIVATE_FINAL] = WorkerMode.PRIVATE_FINAL
    question_id: str
    test_cases: list[WorkerTestCase]
    function_name: str | None = None


class FinalWorkerResult(BaseModel):
    passed: bool
    pass_at_1: float
    worker_metadata: dict


# Compatibility alias for callers/tests created with the first public-only backend.
WorkerRequest = PublicWorkerRequest
