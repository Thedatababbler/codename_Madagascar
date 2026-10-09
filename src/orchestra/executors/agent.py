import json
import os
import time
from pathlib import Path
from uuid import uuid4

from orchestra.backends.base import (
    AgentRequest,
    AgentRunStatus,
    AgentSessionPolicy,
    ArtifactRef,
    BackendExecutionContext,
    ModelSpec,
    OutputContract,
)
from orchestra.backends.errors import BackendCapabilityError
from orchestra.backends.registry import AgentBackendRegistry
from orchestra.control.author.node_review import author_review_dir, run_author_review
from orchestra.executors.seeded import seed_for, sync_tree
from orchestra.harness.progress import redact_hidden_suite
from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.ir.contracts import AgentContract
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.memory.assemble import strip_first_pass
from orchestra.memory.runtime import RUN_ENV, MemoryRun, after_result, before_send
from orchestra.prompts.agent_request import render_agent_request_messages
from orchestra.prompts.render import render_contract
from orchestra.runtime.backend import RunContext
from orchestra.runtime.state import NodeExecutionResult


def _agent_visible(
    inputs: dict[str, ArtifactEnvelope],
) -> dict[str, ArtifactEnvelope]:
    """The inputs as an agent may read them.

    A contract renders every input artifact into the prompt as JSON, whole. That
    is right for a repository change and wrong for a harness report, which
    carries the authored suite the agent is being ranked against — its path, its
    filenames and the identity of every test of it that failed. The stored
    artifact keeps all of it; the copy that reaches the prompt does not.
    """
    return {
        slot: (
            artifact.model_copy(
                update={"payload": redact_hidden_suite(artifact.payload)}
            )
            if artifact.artifact_type == "RepositoryHarnessResultArtifact"
            else artifact
        )
        for slot, artifact in inputs.items()
    }


class AgentNodeExecutor:
    """Control-plane agent node executor that delegates to AgentBackendRegistry."""

    def __init__(
        self,
        contracts: dict[str, AgentContract],
        backends: AgentBackendRegistry,
    ) -> None:
        self.contracts = contracts
        self.backends = backends

    def _build_request(
        self,
        node: AgentNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> AgentRequest:
        contract = self.contracts[node.contract_id]
        if len(node.output_slots) != 1:
            raise ValueError("Agent nodes must declare exactly one output slot")
        messages = list(render_contract(contract, _agent_visible(inputs)))
        if "__candidate__" in context.task_id and os.environ.get(RUN_ENV):
            # repair candidates re-running a first-run contract get no first-pass memory (memory spec §2.2)
            messages = strip_first_pass(messages)
        if node.prompt_prelude:
            prelude = str(node.prompt_prelude).strip()
            if prelude:
                messages.insert(1, {"role": "user", "content": prelude})
        if node.prompt_feedback:
            feedback = str(node.prompt_feedback).strip()
            if feedback:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Previous attempt failed. Apply this feedback carefully "
                            f"and fix the repository:\n{feedback}"
                        ),
                    }
                )
        backend = node.resolved_backend()
        max_steps = getattr(backend, "max_steps", 1)
        tools = list(node.tools) if node.tools else list(contract.allowed_tools)
        if node.model is not None:
            model = node.model
        else:
            model = ModelSpec(
                name=contract.model,
                temperature=contract.temperature,
                max_tokens=contract.max_tokens,
            )
        if node.output_contract is not None:
            output_contract = node.output_contract
        else:
            output_contract = OutputContract(
                parser_id=contract.parser_id,
                output_schema=contract.output_schema,
            )
        if node.session_policy:
            session_policy = AgentSessionPolicy(node.session_policy)
        else:
            session_policy = AgentSessionPolicy.FRESH
        return AgentRequest(
            request_id=str(uuid4()),
            task_id=context.task_id,
            subtask_id=context.subtask_id,
            node_id=node.node_id,
            role=contract.role,
            instruction=contract.system_prompt_template,
            input_artifacts=[
                ArtifactRef(
                    slot=slot,
                    artifact_id=artifact.artifact_id,
                    artifact_type=artifact.artifact_type,
                )
                for slot, artifact in sorted(inputs.items())
            ],
            rendered_context=messages[-1]["content"] if messages else "",
            model=model,
            tools=tools,
            max_steps=max_steps,
            timeout_seconds=node.timeout_seconds or contract.timeout_seconds,
            output_contract=output_contract,
            backend_config=backend.model_dump(mode="json"),
            messages=messages,
            contract_id=contract.contract_id,
            session_policy=session_policy,
            session_ref=None,
        )

    def _trace_dir(self, context: RunContext, node_id: str) -> str:
        path = Path(context.run_dir) / "tasks" / context.task_id / "backend_traces" / node_id
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _persist_raw_trace(
        self, trace_dir: str, request_id: str, result
    ) -> str | None:
        if not result.trace_events:
            return None
        path = Path(trace_dir) / f"{request_id}.json"
        path.write_text(
            json.dumps(
                [event.model_dump(mode="json") for event in result.trace_events],
                indent=2,
            ),
            encoding="utf-8",
        )
        return str(path)

    async def execute(
        self,
        node: AgentNodeSpec,
        inputs: dict[str, ArtifactEnvelope],
        context: RunContext,
    ) -> NodeExecutionResult:
        started = time.perf_counter()
        request = self._build_request(node, inputs, context)
        self.backends.validate_request(request)
        backend = self.backends.get(str(request.backend_config["type"]))
        trace_dir = self._trace_dir(context, node.node_id)
        backend_context = BackendExecutionContext(
            run_id=context.run_id,
            task_id=context.task_id,
            subtask_id=context.subtask_id,
            node_id=node.node_id,
            artifact_refs=request.input_artifacts,
            trace_dir=trace_dir,
            workspace_ref=context.workspace_ref,
        )
        seed = seed_for(context.workspace_ref)
        # memory banks (memory spec §4): checks on the exact prompt, off unless a run activated them
        memory = MemoryRun.current()
        memory_pre = None
        if memory is not None:
            prompt_text = render_agent_request_messages(request)
            memory_pre = before_send(
                memory, node_id=node.node_id, contract_id=request.contract_id or "", task_id=context.task_id,
                role=request.role, prompt=prompt_text,
            )
        if seed is not None:
            # joint experiment: the first stage replays a stored first run (no model call)
            result = await seeded_result(request, backend_context, seed)
        else:
            # Semaphores stay in the control plane; backends never see them.
            async with context.semaphores.llm:
                result = await backend.run(request, backend_context)
        raw_trace_path = self._persist_raw_trace(trace_dir, request.request_id, result)
        if memory is not None and memory_pre is not None:
            captured = Path(trace_dir) / f"{request.request_id}.prompt.txt"
            if seed is None and not captured.is_file() and str(request.backend_config.get("type")) != "codex_sdk":
                # a backend without its own capture was handed exactly these messages
                captured.write_text(prompt_text, encoding="utf-8")
            after_result(
                memory, memory_pre, node_id=node.node_id, contract_id=request.contract_id or "",
                task_id=context.task_id, role=request.role, prompt=prompt_text,
                final_output=str(result.final_output or ""), status=str(result.status.value if hasattr(result.status, "value") else result.status),
                trace_dir=trace_dir, request_id=request.request_id, seeded=seed is not None,
            )
        metadata = dict(result.backend_metadata)
        if result.session_ref is not None:
            metadata["session_ref"] = result.session_ref.model_dump(mode="json")
        if raw_trace_path:
            metadata["raw_trace_path"] = raw_trace_path
            metadata["trace_summary"] = [
                {
                    "event_type": event.event_type,
                    "index": event.index,
                    "summary": event.summary or event.message,
                }
                for event in result.trace_events
            ]
        latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
        if result.status is not AgentRunStatus.SUCCESS or not result.output_artifacts:
            message = (
                result.error.message
                if result.error is not None
                else f"Backend returned status {result.status}"
            )
            return NodeExecutionResult(
                node_id=node.node_id,
                succeeded=False,
                error=message,
                latency_ms=latency_ms,
                usage=result.usage,
                backend_id=result.backend_id,
                backend_status=result.status,
                trace_events=result.trace_events,
                backend_metadata=metadata,
            )
        review_dir = author_review_dir(request.contract_id)
        if review_dir is not None:
            # stage B3 of the author-evolution loop: audit, coverage, fix rounds, soft split --
            # inside the author node, before the custody node freezes the suite
            result, review = await run_author_review(
                review_dir=review_dir, request=request, backend=backend, backend_context=backend_context,
                first=result, semaphore=context.semaphores.llm,
            )
            metadata["author_review"] = review
        output_slot, expected_schema = next(iter(node.output_slots.items()))
        artifact = result.output_artifacts[0]
        if artifact.artifact_type != expected_schema:
            raise ValueError(
                f"Backend produced {artifact.artifact_type}, expected {expected_schema}"
            )
        return NodeExecutionResult(
            node_id=node.node_id,
            succeeded=True,
            outputs={output_slot: artifact},
            latency_ms=latency_ms,
            usage=result.usage,
            backend_id=result.backend_id,
            backend_status=result.status,
            trace_events=result.trace_events,
            backend_metadata=metadata,
        )

    async def execute_safely(self, *args, **kwargs) -> NodeExecutionResult:
        node = args[0] if args else kwargs["node"]
        started = time.perf_counter()
        try:
            return await self.execute(*args, **kwargs)
        except (ValueError, KeyError, TimeoutError, BackendCapabilityError) as exc:
            return NodeExecutionResult.failed(
                node.node_id, exc, int((time.perf_counter() - started) * 1000)
            )


async def seeded_result(request, backend_context, seed: Path):
    """An agent result whose change is the stored first run (joint experiment)."""
    from orchestra.backends.base import AgentResult
    from orchestra.ir.artifacts import create_artifact
    from orchestra.schemas.artifacts import RepositoryChangeArtifact
    from orchestra.workspaces.base import WorkspaceRef
    from orchestra.workspaces.git_workspace import SharedSubtaskGitWorkspaceManager

    ws = WorkspaceRef(workspace_id=backend_context.subtask_id or request.task_id, path=backend_context.workspace_ref,
                      task_id=backend_context.task_id, subtask_id=backend_context.subtask_id or "main", base_revision=None)
    manager = SharedSubtaskGitWorkspaceManager()
    before = await manager.snapshot(ws)
    ws = ws.model_copy(update={"base_revision": before.head_revision or before.base_revision})
    changed = sync_tree(seed, Path(backend_context.workspace_ref))
    snap = await manager.snapshot(ws)
    payload = RepositoryChangeArtifact(
        workspace_ref=ws.path, thread_id="seeded", base_revision=ws.base_revision, changed_files=list(snap.changed_files),
        patch=snap.patch, final_response=f"seeded from {seed} ({len(changed)} file(s) synced)", source_node=request.node_id,
    )
    artifact = create_artifact(payload, producer_node_id=request.node_id, task_id=request.task_id)
    return AgentResult(
        request_id=request.request_id, backend_id="seeded_replay", status=AgentRunStatus.SUCCESS,
        final_output=payload.final_response, output_artifacts=[artifact], latency_ms=0, step_count=0,
        backend_metadata={"seeded_from": str(seed), "synced_files": len(changed), "workspace_ref": ws.path},
    )
