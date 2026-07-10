"""Static capability catalog for compile-time validation.

Capabilities are keyed by backend type string. Validation uses
BackendCapabilities fields only — never backend-id special cases.
"""

from orchestra.backends.capabilities import BackendCapabilities

KNOWN_BACKEND_CAPABILITIES: dict[str, BackendCapabilities] = {
    "structured_llm": BackendCapabilities(
        multi_step=False,
        code_actions=False,
        structured_tools=False,
        repository_editing=False,
        supports_remote_executor=False,
        supports_step_trace=True,
        supports_resume=False,
    ),
    "smolagents_code": BackendCapabilities(
        multi_step=True,
        code_actions=True,
        structured_tools=True,
        repository_editing=False,
        supports_remote_executor=True,
        supports_step_trace=True,
        supports_resume=False,
    ),
}


def capabilities_for(backend_id: str) -> BackendCapabilities | None:
    return KNOWN_BACKEND_CAPABILITIES.get(backend_id)
