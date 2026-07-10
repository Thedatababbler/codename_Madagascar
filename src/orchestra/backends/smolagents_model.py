"""Smolagents model factory wrapping OpenAI-compatible endpoints."""

from __future__ import annotations

import os
from typing import Any

from orchestra.backends.base import ModelSpec
from orchestra.backends.errors import BackendInitializationError


class SmolagentsModelFactory:
    """Create smolagents models from AdaMAS ModelSpec without bypassing pricing fields."""

    def create(self, spec: ModelSpec) -> Any:
        try:
            from smolagents import OpenAIServerModel
        except ImportError as exc:
            raise BackendInitializationError(
                "smolagents is not installed; install with: uv sync --extra smolagents"
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
