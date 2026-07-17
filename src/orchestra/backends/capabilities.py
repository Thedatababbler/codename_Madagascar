"""Backend capability declarations for agent execution adapters."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SessionPolicy(StrEnum):
    """Session lifecycle policy negotiated against backend capabilities."""

    FRESH = "fresh"
    RESUME = "resume"
    FORK = "fork"


class BackendCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    multi_step: bool = False
    code_actions: bool = False
    structured_tools: bool = False
    repository_editing: bool = False
    supports_remote_executor: bool = False
    supports_step_trace: bool = False
    # Legacy flag kept for compile-time catalogs; prefer supported_session_policies.
    supports_resume: bool = False
    supports_session_state: bool = False
    supported_session_policies: frozenset[SessionPolicy] = Field(
        default_factory=lambda: frozenset({SessionPolicy.FRESH})
    )
    supports_tool_policy_edit: bool = False
    supports_model_override: bool = True
    supports_workspace_rebinding: bool = True
    supports_parallel_instances: bool = True

    def supports_policy(self, policy: SessionPolicy) -> bool:
        return policy in self.supported_session_policies
