from pydantic import BaseModel, Field


class VisibleTestResult(BaseModel):
    index: int
    passed: bool
    timed_out: bool = False
    runtime_error: str | None = None
    actual_output: str | None = None


class SandboxExecutionResult(BaseModel):
    compiled: bool
    passed_count: int
    total_count: int
    runtime_errors: int
    timeouts: int
    stderr_summary: str | None = None
    per_test_visible_results: list[VisibleTestResult] = Field(default_factory=list)
    duration_ms: int
