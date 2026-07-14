"""Unit tests for smolagents exception → status mapping."""

from smolagents import (
    AgentExecutionError,
    AgentGenerationError,
    AgentMaxStepsError,
    AgentParsingError,
    AgentToolCallError,
    AgentToolExecutionError,
)
from smolagents.monitoring import AgentLogger, LogLevel

from orchestra.backends.base import AgentRunStatus
from orchestra.backends.errors import (
    BackendInitializationError,
    ModelInvocationError,
    OutputContractValidationError,
    OutputParseError,
)
from orchestra.backends.exception_mapping import map_exception_to_status

_LOGGER = AgentLogger(level=LogLevel.ERROR)


def test_map_typed_and_smolagents_exceptions():
    cases = [
        (ModelInvocationError("x"), AgentRunStatus.MODEL_FAILURE),
        (OutputParseError("x"), AgentRunStatus.ACTION_PARSE_FAILURE),
        (OutputContractValidationError("x"), AgentRunStatus.OUTPUT_CONTRACT_FAILURE),
        (BackendInitializationError("x"), AgentRunStatus.BACKEND_INIT_FAILURE),
        (TimeoutError("x"), AgentRunStatus.TIMEOUT),
        (AgentGenerationError("x", _LOGGER), AgentRunStatus.MODEL_FAILURE),
        (AgentParsingError("x", _LOGGER), AgentRunStatus.ACTION_PARSE_FAILURE),
        (AgentToolCallError("x", _LOGGER), AgentRunStatus.TOOL_FAILURE),
        (AgentToolExecutionError("x", _LOGGER), AgentRunStatus.TOOL_FAILURE),
        (AgentExecutionError("x", _LOGGER), AgentRunStatus.TOOL_FAILURE),
        (AgentMaxStepsError("x", _LOGGER), AgentRunStatus.MAX_STEPS_EXCEEDED),
        (RuntimeError("x"), AgentRunStatus.INFRA_ERROR),
    ]
    for exc, expected in cases:
        assert map_exception_to_status(exc) is expected


def test_map_unwraps_generation_wrapper_to_nested_parse_error():
    nested = AgentParsingError("parse", _LOGGER)
    outer = AgentGenerationError("wrapped", _LOGGER)
    outer.__cause__ = nested
    assert map_exception_to_status(outer) is AgentRunStatus.ACTION_PARSE_FAILURE


def test_map_unwraps_generation_wrapper_to_nested_tool_error():
    nested = AgentToolExecutionError("tool", _LOGGER)
    outer = AgentGenerationError("wrapped", _LOGGER)
    outer.__cause__ = nested
    assert map_exception_to_status(outer) is AgentRunStatus.TOOL_FAILURE
