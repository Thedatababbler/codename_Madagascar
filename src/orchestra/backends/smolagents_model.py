"""Smolagents model factory wrapping OpenAI-compatible and offline fixtures."""

from __future__ import annotations

import os
from typing import Any

from orchestra.backends.base import ModelSpec
from orchestra.backends.errors import BackendInitializationError


def _build_scripted_fixture_model(responses: list[str], *, model_id: str) -> Any:
    from smolagents import (
        AgentGenerationError,
        AgentParsingError,
        AgentToolExecutionError,
        ChatMessage,
        Model,
        TokenUsage,
    )
    from smolagents.monitoring import AgentLogger, LogLevel

    if not responses:
        raise BackendInitializationError("fixture model requires at least one response")

    logger = AgentLogger(level=LogLevel.ERROR)

    class ScriptedFixtureModel(Model):
        def __init__(self) -> None:
            super().__init__(model_id=model_id)
            self._responses = list(responses)
            self._index = 0

        def generate(
            self,
            messages,  # noqa: ANN001
            stop_sequences=None,  # noqa: ANN001
            response_format=None,  # noqa: ANN001
            tools_to_call_from=None,  # noqa: ANN001
            **kwargs,  # noqa: ANN003
        ) -> ChatMessage:
            del messages, stop_sequences, response_format, tools_to_call_from, kwargs
            if self._index >= len(self._responses):
                raise AgentGenerationError(
                    "fixture model exhausted scripted responses", logger
                )
            text = self._responses[self._index]
            self._index += 1
            if text == "__RAISE_GENERATION__":
                raise AgentGenerationError("scripted model generation failure", logger)
            if text == "__RAISE_PARSING__":
                raise AgentParsingError("scripted model parsing failure", logger)
            if text == "__RAISE_TOOL__":
                raise AgentToolExecutionError(
                    "scripted tool execution failure", logger
                )
            return ChatMessage(
                role="assistant",
                content=text,
                token_usage=TokenUsage(input_tokens=3, output_tokens=5),
            )

    return ScriptedFixtureModel()


class SmolagentsModelFactory:
    """Create smolagents models from AdaMAS ModelSpec without bypassing pricing fields."""

    def create(
        self,
        spec: ModelSpec,
        *,
        fixture_responses: list[str] | None = None,
    ) -> Any:
        try:
            from smolagents import OpenAIServerModel
        except ImportError as exc:
            raise BackendInitializationError(
                "smolagents is not installed; install with: uv sync --extra smolagents"
            ) from exc

        if spec.provider == "fixture":
            try:
                return _build_scripted_fixture_model(
                    list(fixture_responses or []), model_id=spec.name
                )
            except BackendInitializationError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise BackendInitializationError(
                    f"Failed to initialize fixture model {spec.name!r}: {exc}"
                ) from exc

        api_base = os.getenv("OPENAI_BASE_URL")
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_base or not api_key:
            raise BackendInitializationError(
                "OPENAI_BASE_URL and OPENAI_API_KEY are required for smolagents_code"
            )
        try:
            return OpenAIServerModel(
                model_id=spec.name,
                api_base=api_base.rstrip("/"),
                api_key=api_key,
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise BackendInitializationError(
                f"Failed to initialize smolagents model {spec.name!r}: {exc}"
            ) from exc
