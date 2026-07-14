"""Map smolagents exceptions to AgentRunStatus without string matching."""

from __future__ import annotations

from orchestra.backends.base import AgentRunStatus
from orchestra.backends.errors import (
    BackendInitializationError,
    ModelInvocationError,
    OutputContractValidationError,
    OutputParseError,
)


def _direct_map(exc: BaseException) -> AgentRunStatus | None:
    if isinstance(exc, BackendInitializationError):
        return AgentRunStatus.BACKEND_INIT_FAILURE
    if isinstance(exc, ModelInvocationError):
        return AgentRunStatus.MODEL_FAILURE
    if isinstance(exc, OutputParseError):
        return AgentRunStatus.ACTION_PARSE_FAILURE
    if isinstance(exc, OutputContractValidationError):
        return AgentRunStatus.OUTPUT_CONTRACT_FAILURE
    if isinstance(exc, TimeoutError):
        return AgentRunStatus.TIMEOUT

    try:
        from smolagents import (
            AgentExecutionError,
            AgentGenerationError,
            AgentMaxStepsError,
            AgentParsingError,
            AgentToolCallError,
            AgentToolExecutionError,
        )
    except ImportError:
        return None

    if isinstance(exc, AgentMaxStepsError):
        return AgentRunStatus.MAX_STEPS_EXCEEDED
    if isinstance(exc, AgentParsingError):
        return AgentRunStatus.ACTION_PARSE_FAILURE
    if isinstance(exc, (AgentToolCallError, AgentToolExecutionError)):
        return AgentRunStatus.TOOL_FAILURE
    if isinstance(exc, AgentExecutionError):
        return AgentRunStatus.TOOL_FAILURE
    if isinstance(exc, AgentGenerationError):
        return AgentRunStatus.MODEL_FAILURE
    return None


def map_exception_to_status(exc: BaseException) -> AgentRunStatus:
    """Classify backend/worker failures into typed run statuses.

    Walk the cause/context chain from the innermost exception so CodeAgent
    wrappers (often ``AgentGenerationError``) do not hide parse/tool failures.
    """
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__

    for item in reversed(chain):
        mapped = _direct_map(item)
        if mapped is not None:
            return mapped
    return AgentRunStatus.INFRA_ERROR
