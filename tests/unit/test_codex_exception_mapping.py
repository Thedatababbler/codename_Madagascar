"""Unit tests for Codex exception → failure class / status mapping."""

from openai_codex.errors import (
    InvalidParamsError,
    InvalidRequestError,
    ServerBusyError,
    TransportClosedError,
)

from orchestra.backends.base import AgentRunStatus
from orchestra.backends.exception_mapping import (
    CodexFailureClass,
    classify_codex_exception,
    map_codex_exception,
)


def test_classify_codex_invalid_auth_runtime_model_infra():
    cases = [
        (InvalidRequestError(-32600, "bad request"), CodexFailureClass.INVALID),
        (InvalidParamsError(-32602, "bad params"), CodexFailureClass.INVALID),
        (
            RuntimeError("unexpected status 401 Unauthorized: Incorrect API key"),
            CodexFailureClass.AUTH,
        ),
        (
            RuntimeError("unexpected status 403 Forbidden: 用户配额不足"),
            CodexFailureClass.INFRA,
        ),
        (TransportClosedError("closed"), CodexFailureClass.RUNTIME),
        (ServerBusyError(-32000, "busy", "server_overloaded"), CodexFailureClass.INFRA),
        (
            RuntimeError("unexpected status 400 on /v1/responses for model"),
            CodexFailureClass.MODEL,
        ),
        (RuntimeError("disk full"), CodexFailureClass.INFRA),
    ]
    for exc, expected in cases:
        assert classify_codex_exception(exc) is expected


def test_map_codex_exception_to_status():
    status, failure = map_codex_exception(
        RuntimeError("401 Unauthorized: invalid_api_key")
    )
    assert failure is CodexFailureClass.AUTH
    assert status is AgentRunStatus.BACKEND_INIT_FAILURE

    status, failure = map_codex_exception(TimeoutError("wall"))
    assert status is AgentRunStatus.TIMEOUT
    assert failure is CodexFailureClass.INFRA
