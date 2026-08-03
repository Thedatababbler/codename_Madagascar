"""Deterministic local backends that never initialize external clients."""

from __future__ import annotations

import time
from typing import Any

from orchestra.backends.base import (
    AgentError,
    AgentRequest,
    AgentResult,
    AgentRunStatus,
    AgentTraceEvent,
    BackendExecutionContext,
    BackendHealth,
)
from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.backends.catalog import capabilities_for
from orchestra.ir.artifacts import create_artifact
from orchestra.llm.usage import LLMUsage
from orchestra.schemas.artifacts import (
    AlgorithmPlanArtifact,
    CodeArtifact,
    EdgeCaseArtifact,
    FinalAnswerArtifact,
    RepositoryChangeArtifact,
)


class DeterministicMockBackend:
    """API-free backend that preserves a real backend_id for accounting."""

    def __init__(
        self,
        backend_id: str,
        *,
        fixture_outputs: dict[str, str] | None = None,
        default_latency_ms: int = 5,
        fail_subtasks: set[str] | None = None,
    ) -> None:
        self._backend_id = backend_id
        self.fixture_outputs = dict(fixture_outputs or {})
        self.default_latency_ms = int(default_latency_ms)
        self.fail_subtasks = set(fail_subtasks or ())

    @property
    def backend_id(self) -> str:
        return self._backend_id

    @property
    def capabilities(self) -> BackendCapabilities:
        known = capabilities_for(self._backend_id)
        if known is not None:
            return known
        return BackendCapabilities(
            multi_step=False,
            code_actions=self._backend_id in {"smolagents_code", "codex_sdk"},
            structured_tools=False,
            repository_editing=self._backend_id in {"smolagents_code", "codex_sdk"},
            supports_remote_executor=False,
            supports_step_trace=True,
            supports_resume=False,
            supports_session_state=False,
            supported_session_policies=frozenset({SessionPolicy.FRESH}),
            supports_tool_policy_edit=False,
            supports_model_override=True,
            supports_workspace_rebinding=True,
            supports_parallel_instances=True,
        )

    async def healthcheck(self) -> BackendHealth:
        return BackendHealth(
            healthy=True,
            backend_id=self.backend_id,
            detail="deterministic_mock",
        )

    def _code_text(self, request: AgentRequest) -> str:
        key = request.contract_id or request.role or request.node_id
        if key in self.fixture_outputs:
            return self.fixture_outputs[key]
        sid = request.subtask_id or "task"
        return (
            "# DETERMINISTIC_MOCK\n"
            f"# backend={self.backend_id}\n"
            f"# subtask={sid}\n"
            "def solve():\n"
            f"    return {sid!r}\n"
        )

    def _payload(self, request: AgentRequest):
        schema = (
            request.output_contract.output_schema
            if request.output_contract is not None
            else "FinalAnswerArtifact"
        )
        text = self._code_text(request)
        if schema == "CodeArtifact":
            return CodeArtifact(code=text, source_node=request.node_id)
        if schema == "AlgorithmPlanArtifact":
            return AlgorithmPlanArtifact(
                problem_summary="deterministic mock plan",
                algorithm="mock",
                correctness_argument="fixture",
                time_complexity="O(1)",
                space_complexity="O(1)",
                edge_cases=["empty"],
                implementation_notes=["deterministic_mock"],
            )
        if schema == "EdgeCaseArtifact":
            return EdgeCaseArtifact(
                input_output_interpretation="mock",
                edge_cases=["empty"],
            )
        if schema == "RepositoryChangeArtifact":
            workspace = str(
                request.backend_config.get("workspace_ref") or "mock"
            )
            return RepositoryChangeArtifact(
                workspace_ref=workspace,
                thread_id=f"mock-{request.request_id}",
                changed_files=["solution.py"],
                patch=(
                    "diff --git a/solution.py b/solution.py\n"
                    "--- a/solution.py\n+++ b/solution.py\n"
                    "+# DETERMINISTIC_MOCK_SOLUTION\n"
                ),
                final_response=text,
                source_node=request.node_id,
            )
        return FinalAnswerArtifact(answer=text, source_node=request.node_id)

    def _materialize_repo_edit(self, context: BackendExecutionContext) -> str | None:
        """Write a deterministic correct solution into the candidate workspace."""
        if not context.workspace_ref:
            return None
        from pathlib import Path

        root = Path(context.workspace_ref)
        root.mkdir(parents=True, exist_ok=True)
        solution = root / "solution.py"
        # Prefer fixture-specific known solutions when present.
        if (root / "calculator.py").exists() or (root / "tests").exists():
            calc = root / "calculator.py"
            calc.write_text(
                "def add(a, b):\n    return a + b\n",
                encoding="utf-8",
            )
        # abc309_a public fixture: seats A,B adjacent in 1..9 row-major 3x3.
        code = (
            "import sys\n"
            "\n"
            "def main() -> None:\n"
            "    data = sys.stdin.read().strip().split()\n"
            "    a, b = map(int, data[:2])\n"
            "    if a > b:\n"
            "        a, b = b, a\n"
            "    same_row = (a - 1) // 3 == (b - 1) // 3\n"
            "    print('Yes' if b - a == 1 and same_row else 'No')\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        solution.write_text(code, encoding="utf-8")
        notes = root / "NOTES.md"
        if not notes.exists():
            notes.write_text("# deterministic mock notes\n", encoding="utf-8")
        return str(root)

    async def run(
        self,
        request: AgentRequest,
        context: BackendExecutionContext,
    ) -> AgentResult:
        started = time.perf_counter()
        if request.subtask_id and request.subtask_id in self.fail_subtasks:
            return AgentResult(
                request_id=request.request_id,
                backend_id=self.backend_id,
                status=AgentRunStatus.MODEL_FAILURE,
                error=AgentError(
                    status=AgentRunStatus.MODEL_FAILURE,
                    message="deterministic_mock_injected_failure",
                ),
                latency_ms=self.default_latency_ms,
                usage=LLMUsage(prompt_tokens=1, completion_tokens=0),
                backend_metadata={"mock": True, "injected_failure": True},
            )

        schema = (
            request.output_contract.output_schema
            if request.output_contract is not None
            else ""
        )
        if schema == "RepositoryChangeArtifact":
            workspace = self._materialize_repo_edit(context)
            if workspace is not None:
                request = request.model_copy(
                    update={
                        "backend_config": {
                            **dict(request.backend_config),
                            "workspace_ref": workspace,
                        }
                    }
                )

        payload = self._payload(request)
        if isinstance(payload, RepositoryChangeArtifact) and context.workspace_ref:
            payload = payload.model_copy(
                update={"workspace_ref": str(context.workspace_ref)}
            )
        artifact = create_artifact(
            payload,
            producer_node_id=request.node_id,
            task_id=request.task_id,
        )
        text = self._code_text(request)
        latency_ms = max(
            self.default_latency_ms,
            int((time.perf_counter() - started) * 1000),
        )
        return AgentResult(
            request_id=request.request_id,
            backend_id=self.backend_id,
            status=AgentRunStatus.SUCCESS,
            final_output=text,
            output_artifacts=[artifact],
            trace_events=[
                AgentTraceEvent(
                    event_type="deterministic_mock_step",
                    index=0,
                    summary=f"mock:{self.backend_id}",
                    message=text[:200],
                )
            ],
            usage=LLMUsage(prompt_tokens=8, completion_tokens=4, total_tokens=12),
            latency_ms=latency_ms,
            step_count=1,
            backend_metadata={
                "mock": True,
                "override": "deterministic_local",
                "run_id": context.run_id,
            },
        )


REQUIRED_MOCK_BACKEND_IDS = (
    "structured_llm",
    "smolagents_code",
    "codex_sdk",
)


def build_deterministic_mock_registry(
    *,
    backend_ids: tuple[str, ...] = REQUIRED_MOCK_BACKEND_IDS,
    fixture_outputs: dict[str, str] | None = None,
    fail_subtasks: set[str] | None = None,
):
    from orchestra.backends.registry import AgentBackendRegistry

    registry = AgentBackendRegistry()
    for backend_id in backend_ids:
        registry.register(
            DeterministicMockBackend(
                backend_id,
                fixture_outputs=fixture_outputs,
                fail_subtasks=fail_subtasks,
            )
        )
    return registry


def mock_backend_manifest_fields(
    *, enabled: bool, backend_ids: list[str]
) -> dict[str, Any]:
    return {
        "backend_override": "deterministic_mock" if enabled else None,
        "mock_backends": bool(enabled),
        "mock_backend_ids": list(backend_ids) if enabled else [],
    }
