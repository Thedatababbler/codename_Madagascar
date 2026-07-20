"""Static capability catalog for compile-time validation.

Capabilities are keyed by backend type string. Validation uses
BackendCapabilities fields only — never backend-id special cases.
"""

from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy

KNOWN_BACKEND_CAPABILITIES: dict[str, BackendCapabilities] = {
    "structured_llm": BackendCapabilities(
        multi_step=False,
        code_actions=False,
        structured_tools=False,
        repository_editing=False,
        supports_remote_executor=False,
        supports_step_trace=True,
        supports_resume=False,
        supports_session_state=False,
        supported_session_policies=frozenset({SessionPolicy.FRESH}),
        supports_tool_policy_edit=False,
        supports_model_override=True,
        supports_workspace_rebinding=True,
        supports_parallel_instances=True,
    ),
    "smolagents_code": BackendCapabilities(
        multi_step=True,
        code_actions=True,
        structured_tools=True,
        repository_editing=False,
        supports_remote_executor=True,
        supports_step_trace=True,
        supports_resume=False,
        supports_session_state=False,
        supported_session_policies=frozenset({SessionPolicy.FRESH}),
        supports_tool_policy_edit=True,
        supports_model_override=True,
        supports_workspace_rebinding=True,
        supports_parallel_instances=True,
    ),
    "codex_sdk": BackendCapabilities(
        multi_step=True,
        code_actions=True,
        structured_tools=False,
        repository_editing=True,
        supports_remote_executor=False,
        supports_step_trace=False,
        supports_resume=True,
        supports_fork=True,
        supports_session_state=True,
        supported_session_policies=frozenset(
            {SessionPolicy.FRESH, SessionPolicy.RESUME, SessionPolicy.FORK}
        ),
        supports_tool_policy_edit=False,
        supports_model_override=True,
        supports_workspace_rebinding=True,
        supports_cross_workspace_resume=True,
        supports_cross_workspace_fork=True,
        supports_parallel_instances=True,
    ),
}


def capabilities_for(backend_id: str) -> BackendCapabilities | None:
    return KNOWN_BACKEND_CAPABILITIES.get(backend_id)
