"""Map backend exceptions to AgentRunStatus (and Codex failure classes)."""

from __future__ import annotations

from enum import StrEnum

from orchestra.backends.base import AgentRunStatus
from orchestra.backends.errors import (
    BackendInitializationError,
    ModelInvocationError,
    OutputContractValidationError,
    OutputParseError,
)


class CodexFailureClass(StrEnum):
    """Coarse Codex failure taxonomy for smoke/telemetry (M3.5)."""

    INVALID = "invalid"
    AUTH = "auth"
    RUNTIME = "runtime"
    MODEL = "model"
    INFRA = "infra"


_CODEX_CLASS_TO_STATUS = {
    CodexFailureClass.INVALID: AgentRunStatus.INVALID_REQUEST,
    CodexFailureClass.AUTH: AgentRunStatus.BACKEND_INIT_FAILURE,
    CodexFailureClass.RUNTIME: AgentRunStatus.INFRA_ERROR,
    CodexFailureClass.MODEL: AgentRunStatus.MODEL_FAILURE,
    CodexFailureClass.INFRA: AgentRunStatus.INFRA_ERROR,
}


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


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def map_exception_to_status(exc: BaseException) -> AgentRunStatus:
    """Classify backend/worker failures into typed run statuses.

    Walk the cause/context chain from the innermost exception so CodeAgent
    wrappers (often ``AgentGenerationError``) do not hide parse/tool failures.
    """
    for item in reversed(_exception_chain(exc)):
        mapped = _direct_map(item)
        if mapped is not None:
            return mapped
    return AgentRunStatus.INFRA_ERROR


def _auth_message(message: str) -> bool:
    lowered = message.lower()
    # Do not treat bare 403 / quota errors as auth (proxies often use 403 for billing).
    needles = (
        "401",
        "unauthorized",
        "invalid_api_key",
        "incorrect api key",
        "missing bearer",
        "authentication",
        "auth error",
        "not logged in",
        "login required",
    )
    return any(n in lowered for n in needles)


def _quota_or_capacity_message(message: str) -> bool:
    lowered = message.lower()
    needles = (
        "quota",
        "rate limit",
        "insufficient",
        "额度",
        "配额",
        "余额",
        "overloaded",
        "server_overloaded",
        "status 429",
        "status 403",
    )
    return any(n in lowered for n in needles)


def classify_codex_exception(exc: BaseException) -> CodexFailureClass:
    """Classify a Codex SDK/runtime exception into invalid/auth/runtime/model/infra."""
    try:
        from openai_codex.errors import (
            InvalidParamsError,
            InvalidRequestError,
            MethodNotFoundError,
            ParseError,
            RetryLimitExceededError,
            ServerBusyError,
            TransportClosedError,
        )
    except ImportError:
        InvalidParamsError = InvalidRequestError = MethodNotFoundError = ParseError = ()  # type: ignore[misc, assignment]
        RetryLimitExceededError = ServerBusyError = TransportClosedError = ()  # type: ignore[misc, assignment]

    for item in reversed(_exception_chain(exc)):
        message = str(item)
        if isinstance(item, (InvalidRequestError, InvalidParamsError, ParseError)):
            return CodexFailureClass.INVALID
        if isinstance(item, MethodNotFoundError):
            return CodexFailureClass.RUNTIME
        if isinstance(item, TransportClosedError):
            return CodexFailureClass.RUNTIME
        if isinstance(item, (ServerBusyError, RetryLimitExceededError)):
            return CodexFailureClass.INFRA
        if isinstance(item, TimeoutError):
            return CodexFailureClass.INFRA
        if isinstance(item, ValueError) and "unsupported" in message.lower():
            return CodexFailureClass.INVALID
        if _auth_message(message):
            return CodexFailureClass.AUTH
        if _quota_or_capacity_message(message):
            return CodexFailureClass.INFRA
        # Model-facing HTTP / generation failures that are not clearly auth/infra.
        if isinstance(item, RuntimeError) and any(
            token in message.lower()
            for token in ("status 4", "status 5", "responses", "model")
        ):
            if _auth_message(message):
                return CodexFailureClass.AUTH
            if "status 5" in message.lower():
                return CodexFailureClass.INFRA
            return CodexFailureClass.MODEL
    return CodexFailureClass.INFRA


def map_codex_exception(exc: BaseException) -> tuple[AgentRunStatus, CodexFailureClass]:
    """Map Codex exceptions to (AgentRunStatus, CodexFailureClass)."""
    if isinstance(exc, TimeoutError):
        return AgentRunStatus.TIMEOUT, CodexFailureClass.INFRA
    failure_class = classify_codex_exception(exc)
    return _CODEX_CLASS_TO_STATUS[failure_class], failure_class
