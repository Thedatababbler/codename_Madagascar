"""Backend capability declarations for agent execution adapters."""

from pydantic import BaseModel, ConfigDict


class BackendCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    multi_step: bool = False
    code_actions: bool = False
    structured_tools: bool = False
    repository_editing: bool = False
    supports_remote_executor: bool = False
    supports_step_trace: bool = False
    supports_resume: bool = False
