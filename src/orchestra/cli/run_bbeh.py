"""CLI for BBEH + CodeAgent vertical slice runs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from orchestra.backends.factory import build_default_backend_registry
from orchestra.backends.health import healthcheck_used_backends
from orchestra.cli.validate_graph import build_compiler
from orchestra.config import _expand
from orchestra.executors.agent import AgentNodeExecutor
from orchestra.executors.harness import HarnessNodeExecutor
from orchestra.executors.registry import NodeExecutorRegistry
from orchestra.ir.artifacts import ArtifactBundle
from orchestra.ir.contracts import load_contracts
from orchestra.ir.graph import load_graph
from orchestra.llm.mock_async import MockAsyncLLMClient
from orchestra.llm.openai_compatible_async import OpenAICompatibleAsyncClient
from orchestra.runtime.backend import RunContext
from orchestra.runtime.checkpoint import CheckpointStore
from orchestra.runtime.limits import RuntimeLimits, RuntimeSemaphores
from orchestra.runtime.native_async import NativeAsyncRuntime
from orchestra.sandbox.mock import MockSandbox
from orchestra.settings import load_env_file
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.tasks.bbeh import BBEHTaskAdapter
from orchestra.telemetry.events import TelemetryEvent


class BBEHExperimentSection(BaseModel):
    name: str
    seed: int = 42
    graph_config: str
    contracts_dir: str = "configs/contracts"
    output_root: str


class BBEHBenchmarkSection(BaseModel):
    name: str = "bbeh"
    data_dir: str
    task_ids: list[str] = Field(default_factory=list)
    difficulties: list[str] = Field(default_factory=list)


class BBEHExperimentConfig(BaseModel):
    experiment: BBEHExperimentSection
    benchmark: BBEHBenchmarkSection
    runtime: RuntimeLimits = Field(default_factory=RuntimeLimits)
    logging: dict[str, Any] = Field(default_factory=dict)


def load_bbeh_experiment_config(path: str | Path) -> BBEHExperimentConfig:
    return BBEHExperimentConfig.model_validate(
        _expand(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    )


def _redact_text(value: str | None, *, limit: int = 80) -> str | None:
    if value is None:
        return None
    text = str(value)
    for secret_key in ("OPENAI_API_KEY", "HF_TOKEN", "API_KEY", "TOKEN", "SECRET"):
        secret = __import__("os").environ.get(secret_key)
        if secret:
            text = text.replace(secret, "[REDACTED]")
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def build_desensitized_summary(
    *,
    run_id: str,
    evaluations: list[dict[str, Any]],
    events_path: Path | None = None,
) -> dict[str, Any]:
    """Public-safe summary: no secrets, no full prompts, truncated answers."""
    per_task = []
    for item in evaluations:
        per_task.append(
            {
                "task_id": item.get("task_id"),
                "execution_success": item.get("execution_success"),
                "answer_correct": item.get("answer_correct"),
                "extracted_answer": _redact_text(item.get("extracted_answer"), limit=64),
                # Reference answers are omitted from the desensitized summary.
                "subset": (item.get("details") or {}).get("subset"),
                "extraction_status": (item.get("details") or {}).get(
                    "extraction_status"
                ),
            }
        )
    token_prompt = 0
    token_completion = 0
    latency_ms = 0
    if events_path is not None and events_path.exists():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("event_type") == "NODE_COMPLETED":
                token_prompt += int(event.get("prompt_tokens") or 0)
                token_completion += int(event.get("completion_tokens") or 0)
                latency_ms += int(event.get("latency_ms") or 0)
    return {
        "run_id": run_id,
        "tasks": len(evaluations),
        "execution_success": sum(
            1 for item in evaluations if item.get("execution_success")
        ),
        "answer_correct": sum(1 for item in evaluations if item.get("answer_correct")),
        "usage": {
            "prompt_tokens": token_prompt,
            "completion_tokens": token_completion,
            "latency_ms": latency_ms,
        },
        "tasks_summary": per_task,
        "notes": [
            "Desensitized summary omits reference answers and full prompts.",
            "Secrets from the environment are redacted if present in text fields.",
        ],
    }


async def _run(args: argparse.Namespace) -> int:
    load_env_file()
    config = load_bbeh_experiment_config(args.config)
    if args.output_root:
        config.experiment.output_root = args.output_root
    contracts = load_contracts(config.experiment.contracts_dir)
    graph = load_graph(config.experiment.graph_config)
    compiled = build_compiler(config.experiment.contracts_dir).compile(graph)
    adapter = BBEHTaskAdapter(config.benchmark.data_dir)
    task_ids = list(args.task_id or config.benchmark.task_ids)
    if args.limit is not None:
        task_ids = task_ids[: args.limit]

    contract_hash = hashlib.sha256(
        "".join(
            contracts[key].model_dump_json() for key in sorted(contracts)
        ).encode()
    ).hexdigest()
    run_id = args.run_id or (
        f"{config.experiment.name}-{graph.content_hash[:8]}-"
        f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    )
    run_dir = Path(config.experiment.output_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "experiment": config.model_dump(mode="json"),
                "graph_hash": graph.content_hash,
                "contract_hash": contract_hash,
                "task_ids": task_ids,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    llm = (
        MockAsyncLLMClient({})
        if args.mock_llm
        else OpenAICompatibleAsyncClient()
    )
    backend_registry = build_default_backend_registry(llm, include_smolagents=True)
    if not backend_registry.has("smolagents_code"):
        raise RuntimeError(
            "smolagents_code backend unavailable; install with uv sync --extra smolagents"
        )
    semaphores = RuntimeSemaphores(config.runtime)
    artifact_store = FileArtifactStore(run_dir)
    checkpoint_store = CheckpointStore(run_dir)
    event_writer = AppendOnlyEventWriter(run_dir)
    executors = NodeExecutorRegistry(
        agent_executor=AgentNodeExecutor(contracts, backend_registry),
        harness_executor=HarnessNodeExecutor(MockSandbox()),
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

    evaluations = []
    for index, task_id in enumerate(task_ids, 1):
        instance = adapter.load_instance(task_id)
        # Ensure reference never enters agent request path.
        agent_instance = {
            key: value for key, value in instance.items() if key not in {"gt", "reference"}
        }
        problem = adapter.build_problem_artifact(agent_instance)
        context = RunContext(
            run_id=run_id,
            task_id=task_id.replace(":", "_"),
            run_dir=run_dir,
            limits=config.runtime,
            semaphores=semaphores,
            contract_hash=contract_hash,
        )
        await event_writer.append(
            TelemetryEvent(
                run_id=run_id,
                task_id=context.task_id,
                graph_id=graph.graph_id,
                event_type="TASK_STARTED",
            )
        )
        try:
            result = await runtime.execute(
                graph=compiled,
                initial_artifacts=ArtifactBundle(slots={"problem": problem}),
                context=context,
            )
            final = None
            if result.state.final_output_artifact_id:
                final = await artifact_store.get(result.state.final_output_artifact_id)
            evaluation = adapter.evaluate(
                instance=instance,
                final_artifact=final,
                execution_success=bool(result.state.frozen and result.failed_node_count == 0),
            )
        except Exception as exc:  # noqa: BLE001
            evaluation = adapter.evaluate(
                instance=instance,
                final_artifact=None,
                execution_success=False,
            )
            evaluation.details["error"] = f"{type(exc).__name__}: {exc}"
            result = None
        evaluations.append(evaluation.model_dump(mode="json"))
        task_dir = run_dir / "tasks" / context.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "evaluation.json").write_text(
            json.dumps(evaluation.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        if result is not None:
            (task_dir / "graph_result.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
        print(
            f"[{index}/{len(task_ids)}] {task_id}: "
            f"exec={evaluation.execution_success} correct={evaluation.answer_correct} "
            f"answer={evaluation.extracted_answer!r}"
        )

    summary = {
        "run_id": run_id,
        "tasks": len(evaluations),
        "execution_success": sum(1 for item in evaluations if item["execution_success"]),
        "answer_correct": sum(1 for item in evaluations if item["answer_correct"]),
        "evaluations": evaluations,
    }
    (run_dir / "bbeh_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    redacted = build_desensitized_summary(
        run_id=run_id,
        evaluations=evaluations,
        events_path=run_dir / "events.jsonl",
    )
    (run_dir / "bbeh_summary_redacted.json").write_text(
        json.dumps(redacted, indent=2), encoding="utf-8"
    )
    (run_dir / "bbeh_summary_redacted.md").write_text(
        "\n".join(
            [
                f"# BBEH CodeAgent smoke (desensitized) — {run_id}",
                "",
                f"- tasks: {redacted['tasks']}",
                f"- execution_success: {redacted['execution_success']}",
                f"- answer_correct: {redacted['answer_correct']}",
                (
                    f"- tokens: prompt={redacted['usage']['prompt_tokens']} "
                    f"completion={redacted['usage']['completion_tokens']}"
                ),
                f"- latency_ms_sum: {redacted['usage']['latency_ms']}",
                "",
                "| task_id | exec | correct | answer |",
                "|---|---|---|---|",
                *[
                    (
                        f"| {row['task_id']} | {row['execution_success']} | "
                        f"{row['answer_correct']} | {row['extracted_answer']!r} |"
                    )
                    for row in redacted["tasks_summary"]
                ],
                "",
            ]
        ),
        encoding="utf-8",
    )
    await event_writer.append(
        TelemetryEvent(
            run_id=run_id,
            task_id=None,
            graph_id=graph.graph_id,
            event_type="RUN_COMPLETED",
            metadata={
                "tasks": summary["tasks"],
                "execution_success": summary["execution_success"],
                "answer_correct": summary["answer_correct"],
            },
        )
    )
    print(f"run_dir={run_dir}")
    print(f"redacted_summary={run_dir / 'bbeh_summary_redacted.md'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run BBEH CodeAgent vertical slice")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-root")
    parser.add_argument("--task-id", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--mock-llm", action="store_true")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
