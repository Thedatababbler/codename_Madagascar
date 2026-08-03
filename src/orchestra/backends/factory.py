"""Helpers for constructing agent backend registries."""

from __future__ import annotations

from typing import Any

from orchestra.backends.codex_sdk import CodexSDKBackend
from orchestra.backends.mock_deterministic import (
    REQUIRED_MOCK_BACKEND_IDS,
    build_deterministic_mock_registry,
    mock_backend_manifest_fields,
)
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.backends.smolagents_code import SmolagentsCodeBackend
from orchestra.backends.structured_llm import StructuredLLMBackend
from orchestra.llm.base_async import AsyncLLMClient


def build_structured_llm_registry(client: AsyncLLMClient) -> AgentBackendRegistry:
    registry = AgentBackendRegistry()
    registry.register(StructuredLLMBackend(client))
    return registry


def build_default_backend_registry(
    client: AsyncLLMClient,
    *,
    include_smolagents: bool = True,
    include_codex: bool = True,
) -> AgentBackendRegistry:
    registry = build_structured_llm_registry(client)
    if include_smolagents:
        try:
            import smolagents  # noqa: F401

            registry.register(SmolagentsCodeBackend())
        except ImportError:
            pass
    if include_codex:
        try:
            import openai_codex  # noqa: F401

            registry.register(CodexSDKBackend())
        except ImportError:
            pass
    return registry


def build_mock_backend_registry(
    *,
    required_ids: tuple[str, ...] = REQUIRED_MOCK_BACKEND_IDS,
    fixture_outputs: dict[str, str] | None = None,
    fail_subtasks: set[str] | None = None,
) -> AgentBackendRegistry:
    """Fully API-free registry: no external clients may be initialized."""
    registry = build_deterministic_mock_registry(
        backend_ids=required_ids,
        fixture_outputs=fixture_outputs,
        fail_subtasks=fail_subtasks,
    )
    missing = [bid for bid in required_ids if not registry.has(bid)]
    if missing:
        raise RuntimeError(
            "mock-backends fail-closed: could not override configured backends: "
            + ", ".join(missing)
        )
    return registry


def resolve_backend_registry(
    *,
    mock_backends: bool,
    client: AsyncLLMClient | None = None,
    include_smolagents: bool = True,
    include_codex: bool = True,
    required_mock_ids: tuple[str, ...] = REQUIRED_MOCK_BACKEND_IDS,
) -> tuple[AgentBackendRegistry, dict[str, Any]]:
    """Shared factory boundary for production and fixture runners."""
    if mock_backends:
        registry = build_mock_backend_registry(required_ids=required_mock_ids)
        manifest = mock_backend_manifest_fields(
            enabled=True, backend_ids=list(required_mock_ids)
        )
        return registry, manifest
    if client is None:
        raise RuntimeError("non-mock backend registry requires an AsyncLLMClient")
    registry = build_default_backend_registry(
        client,
        include_smolagents=include_smolagents,
        include_codex=include_codex,
    )
    return registry, mock_backend_manifest_fields(enabled=False, backend_ids=[])
