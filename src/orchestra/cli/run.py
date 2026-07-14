import argparse
import asyncio
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from orchestra.adapters.livecodebench.loader import LiveCodeBenchLoader
from orchestra.adapters.livecodebench.mapper import load_manifest
from orchestra.backends.base import ModelSpec
from orchestra.backends.factory import build_default_backend_registry
from orchestra.backends.health import healthcheck_used_backends
from orchestra.cli.validate_graph import build_compiler
from orchestra.config import load_experiment_config
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.experiments.metadata import build_run_metadata, finalize_run_metadata
from orchestra.ir.artifacts import ArtifactBundle, create_artifact
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.ir.nodes import AgentNodeSpec
from orchestra.llm.mock_async import MockAsyncLLMClient
from orchestra.llm.openai_compatible_async import OpenAICompatibleAsyncClient
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.sandbox.docker import DockerSandbox, DockerUnavailableError
from orchestra.sandbox.lcb_official import (
    OfficialLCBSandbox,
    OfficialLCBSandboxUnavailable,
)
from orchestra.sandbox.mock import MockSandbox
from orchestra.settings import load_env_file
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.telemetry.events import TelemetryEvent

_MOCK_ALGORITHM = {
    "problem_summary": "Solve the visible task.",
    "algorithm": "Process inputs according to the statement.",
    "data_structures": ["list"],
    "correctness_argument": "The algorithm directly implements the specification.",
    "time_complexity": "O(n)",
    "space_complexity": "O(n)",
    "edge_cases": ["minimum input"],
    "implementation_notes": [],
}
_MOCK_EDGE_CASES = {
    "input_output_interpretation": "Use the declared interface.",
    "edge_cases": ["minimum input"],
    "overflow_risks": [],
    "indexing_risks": [],
    "interface_concerns": [],
    "likely_failure_modes": [],
}
_MOCK_REPAIR = {
    "diagnosis": "Visible tests failed.",
    "changes": ["Return the mock-correct solution."],
    "revised_code": "# CORRECT_SOLUTION\nprint(input())",
}
_MOCK_CODE = "```python\n# CORRECT_SOLUTION\nprint(input())\n```"


def _mock_client() -> MockAsyncLLMClient:
    algorithm = json.dumps(_MOCK_ALGORITHM)
    edge_cases = json.dumps(_MOCK_EDGE_CASES)
    repair = json.dumps(_MOCK_REPAIR)
    return MockAsyncLLMClient(
        {
            "direct_coder": lambda _: _MOCK_CODE,
            "solution_coder": lambda _: _MOCK_CODE,
            "algorithm_analyst": lambda _: algorithm,
            "edge_case_analyst": lambda _: edge_cases,
            "single_agent_repair": lambda _: repair,
            "repair_agent": lambda _: repair,
        }
    )


def _codeagent_fixture(final_payload: str | dict | list) -> str:
    """One structured CodeAgent step that calls final_answer(payload)."""
    return json.dumps(
        {
            "thought": "mock pipeline fixture",
            "code": f"final_answer({json.dumps(final_payload, ensure_ascii=False)})",
        }
    )


_CODEAGENT_MOCK_FIXTURES: dict[str, list[str]] = {
    "algorithm_analyst": [_codeagent_fixture(_MOCK_ALGORITHM)],
    "edge_case_analyst": [_codeagent_fixture(_MOCK_EDGE_CASES)],
    "solution_coder": [_codeagent_fixture(_MOCK_CODE)],
    "repair_agent": [_codeagent_fixture(_MOCK_REPAIR)],
}


class _MockAwareAgentNodeExecutor(AgentNodeExecutor):
    """Inject fixture CodeAgent responses when --mock-llm is set."""

    def _build_request(self, node: AgentNodeSpec, inputs, context):
        request = super()._build_request(node, inputs, context)
        if request.backend_config.get("type") != "smolagents_code":
            return request
        fixtures = _CODEAGENT_MOCK_FIXTURES.get(request.contract_id or "")
        if not fixtures:
            raise RuntimeError(
                f"--mock-llm has no CodeAgent fixture for contract "
                f"{request.contract_id!r}"
            )
        backend_config = dict(request.backend_config)
        backend_config["fixture_responses"] = list(fixtures)
        return request.model_copy(
            update={
                "backend_config": backend_config,
                "model": ModelSpec(
                    provider="fixture",
                    name=f"mock-{request.contract_id}",
                    temperature=request.model.temperature,
                    max_tokens=request.model.max_tokens,
                ),
            }
        )


async def _run(args) -> int:
    load_env_file()
    started_at = datetime.now(UTC)
    config = load_experiment_config(args.config)
    if args.manifest:
        config.benchmark.manifest = args.manifest
    if args.output_root:
        config.experiment.output_root = args.output_root
    manifest = load_manifest(config.benchmark.manifest)
    contracts = load_contracts(config.experiment.contracts_dir)
    graph = load_graph(config.experiment.graph_config)
    compiled = build_compiler(config.experiment.contracts_dir).compile(graph)

    ids = [entry.question_id for entry in manifest.entries]
    if args.task_id:
        ids = [qid for qid in ids if qid == args.task_id]
    if args.limit is not None:
        ids = ids[: args.limit]
    if args.dry_run:
        print(
            json.dumps(
                {
                    "baseline": graph.metadata.get("baseline"),
                    "task_ids": ids,
                    "waves": compiled.waves,
                    "graph_hash": graph.content_hash,
                },
                indent=2,
            )
        )
        return 0

    loader = LiveCodeBenchLoader(
        config.benchmark.data_dir, config.benchmark.release_version
    )
    tasks = loader.load(task_ids=set(ids))
    task_map = {task.question_id: task for task in tasks}
    contract_hash = hashlib.sha256(
        "".join(
            contracts[key].model_dump_json() for key in sorted(contracts)
        ).encode()
    ).hexdigest()
    run_id = args.run_id or (
        f"{config.experiment.name}-{manifest.manifest_sha256[:8]}-"
        f"{graph.content_hash[:8]}"
    )
    run_dir = Path(config.experiment.output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.force_rerun:
        for qid in ids:
            shutil.rmtree(run_dir / "tasks" / qid, ignore_errors=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "experiment": config.model_dump(),
                "manifest": manifest.model_dump(),
                "graph": graph.model_dump(mode="json"),
                "graph_hash": graph.content_hash,
                "contract_hash": contract_hash,
                "metadata": build_run_metadata(
                    config=config,
                    manifest=manifest,
                    graph=graph,
                    contracts_dir=config.experiment.contracts_dir,
                    started_at=started_at,
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    llm = _mock_client() if args.mock_llm else OpenAICompatibleAsyncClient()
    if args.mock_llm:
        sandbox = MockSandbox()
    elif config.sandbox.backend == "lcb_official":
        try:
            sandbox = OfficialLCBSandbox(
                repository_path=config.benchmark.repository_path,
                limits=config.sandbox.limits,
                num_process_evaluate=config.sandbox.num_process_evaluate,
                worker_grace_seconds=config.sandbox.worker_grace_seconds,
                max_worker_wall_seconds=config.sandbox.max_worker_wall_seconds,
            )
        except OfficialLCBSandboxUnavailable as exc:
            raise RuntimeError(
                "OfficialLCBSandbox is unavailable; refusing subprocess fallback."
            ) from exc
    elif config.sandbox.backend == "docker":
        try:
            sandbox = DockerSandbox(
                image=config.sandbox.image,
                timeout_seconds=config.sandbox.per_test_timeout_seconds,
                memory_mb=config.sandbox.limits.memory_mb,
            )
        except DockerUnavailableError as exc:
            raise RuntimeError(
                "Docker backend is unavailable; select lcb_official explicitly."
            ) from exc
    else:
        raise RuntimeError("sandbox.backend=mock is permitted only with --mock-llm")

    semaphores = RuntimeSemaphores(config.runtime)
    artifact_store = FileArtifactStore(run_dir)
    checkpoint_store = CheckpointStore(run_dir)
    event_writer = AppendOnlyEventWriter(run_dir)
    backend_registry = build_default_backend_registry(llm, include_smolagents=True)
    used_backends = {
        str(node.resolved_backend().type)
        for node in graph.nodes
        if getattr(node, "node_kind", None) and node.node_kind.value == "agent"
    }
    if "smolagents_code" in used_backends and not backend_registry.has(
        "smolagents_code"
    ):
        raise RuntimeError(
            "Graph requires smolagents_code; install with: uv sync --extra smolagents"
        )
    agent_executor_cls = (
        _MockAwareAgentNodeExecutor if args.mock_llm else AgentNodeExecutor
    )
    executors = NodeExecutorRegistry(
        agent_executor=agent_executor_cls(contracts, backend_registry),
        harness_executor=HarnessNodeExecutor(
            sandbox, timeout_seconds=config.sandbox.per_test_timeout_seconds
        ),
    )
    runtime = NativeAsyncRuntime(
        executors=executors,
        artifact_store=artifact_store,
        checkpoint_store=checkpoint_store,
        event_writer=event_writer,
    )
    await event_writer.append(
        TelemetryEvent(
            run_id=run_id,
            task_id=None,
            graph_id=graph.graph_id,
            event_type="RUN_STARTED",
        )
    )
    await healthcheck_used_backends(
        registry=backend_registry,
        graph=compiled,
        event_writer=event_writer,
        run_id=run_id,
        graph_id=graph.graph_id,
    )

    async def run_one(qid: str):
        async with semaphores.task:
            task = task_map[qid]
            initial = create_artifact(
                task.problem,
                producer_node_id="__input__",
                task_id=qid,
            )
            context = RunContext(
                run_id=run_id,
                task_id=qid,
                run_dir=run_dir,
                limits=config.runtime,
                semaphores=semaphores,
                contract_hash=contract_hash,
                allow_config_drift=args.allow_config_drift,
            )
            await event_writer.append(
                TelemetryEvent(
                    run_id=run_id,
                    task_id=qid,
                    graph_id=graph.graph_id,
                    event_type="TASK_STARTED",
                )
            )
            try:
                result = await runtime.execute(
                    graph=compiled,
                    initial_artifacts=ArtifactBundle(slots={"problem": initial}),
                    context=context,
                )
            except Exception as exc:
                failure_path = run_dir / "tasks" / qid / "task_failure.json"
                failure_path.parent.mkdir(parents=True, exist_ok=True)
                failure_path.write_text(
                    json.dumps({"error": str(exc)}, indent=2), encoding="utf-8"
                )
                await event_writer.append(
                    TelemetryEvent(
                        run_id=run_id,
                        task_id=qid,
                        graph_id=graph.graph_id,
                        event_type="TASK_FAILED",
                        status="failed",
                        metadata={"error": str(exc), "error_type": type(exc).__name__},
                    )
                )
                return qid, None
            result_path = run_dir / "tasks" / qid / "graph_result.json"
            result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
            await event_writer.append(
                TelemetryEvent(
                    run_id=run_id,
                    task_id=qid,
                    graph_id=graph.graph_id,
                    event_type="TASK_COMPLETED",
                    status="frozen" if result.state.frozen else "incomplete",
                )
            )
            return qid, result

    task_handles = {}
    async with asyncio.TaskGroup() as group:
        for qid in ids:
            task_handles[qid] = group.create_task(run_one(qid), name=qid)
    for index, qid in enumerate(ids, 1):
        result = task_handles[qid].result()[1]
        print(
            f"[{index}/{len(ids)}] {qid}: "
            f"{'failed' if result is None else f'frozen={result.state.frozen}'}"
        )
    await event_writer.append(
        TelemetryEvent(
            run_id=run_id,
            task_id=None,
            graph_id=graph.graph_id,
            event_type="RUN_COMPLETED",
        )
    )
    finalize_run_metadata(run_dir, completed_at=datetime.now(UTC))
    print(f"run_dir={run_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--output-root")
    parser.add_argument("--task-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--mock-llm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-rerun", action="store_true")
    parser.add_argument("--allow-config-drift", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
