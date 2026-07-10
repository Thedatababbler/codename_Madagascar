"""Helpers for constructing the default Milestone-1 backend registry."""

from orchestra.backends.registry import AgentBackendRegistry
from orchestra.backends.structured_llm import StructuredLLMBackend
from orchestra.llm.base_async import AsyncLLMClient


def build_structured_llm_registry(client: AsyncLLMClient) -> AgentBackendRegistry:
    registry = AgentBackendRegistry()
    registry.register(StructuredLLMBackend(client))
    return registry
