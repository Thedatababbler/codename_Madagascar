"""Helpers for constructing agent backend registries."""

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
) -> AgentBackendRegistry:
    registry = build_structured_llm_registry(client)
    if include_smolagents:
        try:
            import smolagents  # noqa: F401

            registry.register(SmolagentsCodeBackend())
        except ImportError:
            pass
    return registry
