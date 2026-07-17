"""Structured LLM backend wrapping the existing chat-completion agent path."""

from __future__ import annotations

import time

import httpx

from orchestra.backends.base import (
    AgentBackend,
    AgentError,
    AgentRequest,
    AgentResult,
    AgentRunStatus,
    AgentTraceEvent,
    BackendExecutionContext,
    BackendHealth,
)
from orchestra.backends.capabilities import BackendCapabilities, SessionPolicy
from orchestra.backends.errors import (
    ModelInvocationError,
    OutputContractValidationError,
    OutputParseError,
)
from orchestra.ir.artifacts import create_artifact
from orchestra.llm.base_async import AsyncLLMClient
from orchestra.prompts.parsers import parse_output


class StructuredLLMBackend:
    """Single-step structured LLM backend used by existing LiveCodeBench graphs."""

    def __init__(self, client: AsyncLLMClient) -> None:
        self.client = client

    @property
    def backend_id(self) -> str:
        return "structured_llm"

    @property
    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
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
        )

    async def healthcheck(self) -> BackendHealth:
        if self.client is None:
            return BackendHealth(
                healthy=False,
                backend_id=self.backend_id,
                detail="LLM client is not configured",
            )
        return BackendHealth(healthy=True, backend_id=self.backend_id)

    async def run(
        self,
        request: AgentRequest,
        context: BackendExecutionContext,
    ) -> AgentResult:
        started = time.perf_counter()
        messages = request.messages
        if not messages:
            messages = [
                {"role": "system", "content": request.instruction},
                {"role": "user", "content": request.rendered_context},
            ]
        try:
            try:
                response = await self.client.generate(
                    messages=messages,
                    model=request.model.name,
                    temperature=request.model.temperature,
                    max_tokens=request.model.max_tokens,
                    timeout_seconds=request.timeout_seconds,
                    metadata={
                        "run_id": context.run_id,
                        "task_id": request.task_id,
                        "node_id": request.node_id,
                        "contract_id": request.contract_id or "",
                        "backend_id": self.backend_id,
                    },
                )
            except (TimeoutError, httpx.TimeoutException) as exc:
                return self._failure(
                    request,
                    AgentRunStatus.TIMEOUT,
                    exc,
                    int((time.perf_counter() - started) * 1000),
                )
            except httpx.HTTPError as exc:
                raise ModelInvocationError(str(exc)) from exc

            try:
                parsed = parse_output(
                    request.output_contract.parser_id,
                    response.text,
                    request.output_contract.output_schema,
                    request.node_id,
                )
            except ValueError as exc:
                raise OutputParseError(str(exc)) from exc

            if type(parsed).__name__ != request.output_contract.output_schema:
                raise OutputContractValidationError(
                    f"Parser produced {type(parsed).__name__}, "
                    f"expected {request.output_contract.output_schema}"
                )
            artifact = create_artifact(
                parsed,
                producer_node_id=request.node_id,
                task_id=request.task_id,
                parent_artifact_ids=[ref.artifact_id for ref in context.artifact_refs],
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            return AgentResult(
                request_id=request.request_id,
                backend_id=self.backend_id,
                status=AgentRunStatus.SUCCESS,
                final_output=response.text,
                output_artifacts=[artifact],
                trace_events=[
                    AgentTraceEvent(
                        event_type="model_call",
                        index=0,
                        summary="structured_llm_completed",
                        metadata={"provider_request_id": response.provider_request_id},
                        token_usage={
                            "prompt_tokens": response.usage.prompt_tokens,
                            "completion_tokens": response.usage.completion_tokens,
                        },
                    )
                ],
                usage=response.usage,
                latency_ms=latency_ms,
                step_count=1,
                backend_metadata={
                    "contract_id": request.contract_id,
                    "trace_dir": context.trace_dir,
                },
            )
        except ModelInvocationError as exc:
            return self._failure(
                request,
                AgentRunStatus.MODEL_FAILURE,
                exc,
                int((time.perf_counter() - started) * 1000),
            )
        except OutputParseError as exc:
            return self._failure(
                request,
                AgentRunStatus.ACTION_PARSE_FAILURE,
                exc,
                int((time.perf_counter() - started) * 1000),
            )
        except OutputContractValidationError as exc:
            return self._failure(
                request,
                AgentRunStatus.OUTPUT_CONTRACT_FAILURE,
                exc,
                int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - map unknown provider failures
            return self._failure(
                request,
                AgentRunStatus.INFRA_ERROR,
                exc,
                int((time.perf_counter() - started) * 1000),
            )

    @staticmethod
    def _failure(
        request: AgentRequest,
        status: AgentRunStatus,
        exc: Exception,
        latency_ms: int,
    ) -> AgentResult:
        return AgentResult(
            request_id=request.request_id,
            backend_id="structured_llm",
            status=status,
            latency_ms=latency_ms,
            step_count=0,
            error=AgentError(
                status=status,
                message=f"{type(exc).__name__}: {exc}",
            ),
        )


def as_agent_backend(backend: StructuredLLMBackend) -> AgentBackend:
    return backend
