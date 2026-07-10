"""Registry for pluggable agent backends."""

from __future__ import annotations

from orchestra.backends.base import AgentBackend, AgentRequest, BackendCapabilities
from orchestra.backends.errors import BackendCapabilityError


class AgentBackendRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, AgentBackend] = {}

    def register(self, backend: AgentBackend) -> None:
        backend_id = backend.backend_id
        if not backend_id:
            raise ValueError("Backend id must be non-empty")
        if backend_id in self._backends:
            raise ValueError(f"Duplicate backend id: {backend_id}")
        self._backends[backend_id] = backend

    def get(self, backend_id: str) -> AgentBackend:
        try:
            return self._backends[backend_id]
        except KeyError as exc:
            raise KeyError(f"Unknown agent backend: {backend_id}") from exc

    def has(self, backend_id: str) -> bool:
        return backend_id in self._backends

    def ids(self) -> list[str]:
        return sorted(self._backends)

    def validate_request(self, request: AgentRequest) -> None:
        backend_id = str(request.backend_config.get("type") or "structured_llm")
        if not self.has(backend_id):
            raise KeyError(f"Unknown agent backend: {backend_id}")
        backend = self.get(backend_id)
        self.validate_capabilities(request, backend.capabilities)

    @staticmethod
    def validate_capabilities(
        request: AgentRequest, capabilities: BackendCapabilities
    ) -> None:
        if request.max_steps > 1 and not capabilities.multi_step:
            raise BackendCapabilityError(
                f"Backend does not support multi-step execution (max_steps={request.max_steps})"
            )
        if request.tools and not (
            capabilities.structured_tools or capabilities.code_actions
        ):
            raise BackendCapabilityError("Backend does not support tools")
        if request.backend_config.get("executor_type") not in (None, "local"):
            if not capabilities.supports_remote_executor:
                raise BackendCapabilityError(
                    "Backend does not support remote executors"
                )
