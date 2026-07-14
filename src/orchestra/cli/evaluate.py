import argparse
import asyncio
import json
from pathlib import Path

from tqdm import tqdm

from orchestra.adapters.livecodebench.exporter import (
    build_official_record,
    export_predictions,
)
from orchestra.adapters.livecodebench.final_evaluator import (
    FinalEvaluationStatus,
    FinalLCBEvaluator,
)
from orchestra.adapters.livecodebench.loader import LiveCodeBenchLoader
from orchestra.config import ExperimentConfig
from orchestra.runtime.state import RuntimeState
from orchestra.sandbox.lcb_official import FinalLCBWorker
from orchestra.schemas.artifacts import FinalCodeArtifact
from orchestra.settings import load_env_file
from orchestra.storage.artifacts import FileArtifactStore
from orchestra.storage.events import AppendOnlyEventWriter
from orchestra.telemetry.events import TelemetryEvent


async def _evaluate(run_dir: Path) -> int:
    run_manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    config = ExperimentConfig.model_validate(run_manifest["experiment"])
    manifest = run_manifest["manifest"]
    graph = run_manifest["graph"]
    ids = [entry["question_id"] for entry in manifest["entries"]]
    loader = LiveCodeBenchLoader(
        config.benchmark.data_dir, config.benchmark.release_version
    )
    loader.load(task_ids=set(ids))
    final_worker = FinalLCBWorker(
        repository_path=config.benchmark.repository_path,
        limits=config.sandbox.limits,
        num_process_evaluate=config.sandbox.num_process_evaluate,
        worker_grace_seconds=config.sandbox.worker_grace_seconds,
        max_worker_wall_seconds=config.sandbox.max_worker_wall_seconds,
    )
    evaluator = FinalLCBEvaluator(
        private_repository=loader.private_repository,
        evaluator_commit=manifest["livecodebench_commit"],
        worker=final_worker,
        sandbox_semaphore=asyncio.Semaphore(
            config.runtime.max_parallel_sandboxes
        ),
        per_test_timeout_seconds=config.sandbox.per_test_timeout_seconds,
    )
    store = FileArtifactStore(run_dir)
    events = AppendOnlyEventWriter(run_dir)
    predictions = []
    passed = 0
    infra_errors = 0
    incomplete = 0
    evaluated = 0
    cached = 0
    progress = tqdm(
        ids,
        desc="evaluate",
        unit="task",
        total=len(ids),
        dynamic_ncols=True,
    )
    for qid in progress:
        progress.set_postfix(
            task=qid,
            ok=passed,
            infra=infra_errors,
            skip=incomplete,
            cached=cached,
            refresh=False,
        )
        task_dir = run_dir / "tasks" / qid
        graph_result_path = task_dir / "graph_result.json"
        if not graph_result_path.exists():
            incomplete += 1
            progress.write(f"skip {qid}: missing graph_result.json")
            continue
        result_data = json.loads(graph_result_path.read_text(encoding="utf-8"))
        state = RuntimeState.model_validate(result_data["state"])
        if not state.frozen or not state.final_output_artifact_id:
            incomplete += 1
            progress.write(f"skip {qid}: not frozen")
            continue
        artifact = await store.get(state.final_output_artifact_id)
        final = FinalCodeArtifact.model_validate(artifact.payload)
        predictions.append(build_official_record(qid, final.code))
        output_path = task_dir / "final_evaluation.json"
        if output_path.exists():
            evaluation = json.loads(output_path.read_text(encoding="utf-8"))
            evaluated += 1
            cached += 1
            if evaluation.get("status") == FinalEvaluationStatus.PASSED:
                passed += 1
            elif evaluation.get("status") == FinalEvaluationStatus.INFRA_ERROR:
                infra_errors += 1
            continue
        progress.set_postfix(
            task=qid,
            status="running",
            ok=passed,
            infra=infra_errors,
            skip=incomplete,
            cached=cached,
            refresh=True,
        )
        await events.append(
            TelemetryEvent(
                run_id=state.run_id,
                task_id=qid,
                graph_id=graph["graph_id"],
                event_type="PRIVATE_EVALUATION_STARTED",
            )
        )
        evaluation = await evaluator.evaluate_frozen_run(
            runtime_state=state,
            final_code=final.code,
            lcb_problem_ref=qid,
        )
        output_path.write_text(evaluation.model_dump_json(indent=2), encoding="utf-8")
        evaluated += 1
        if evaluation.status is FinalEvaluationStatus.PASSED:
            passed += 1
        elif evaluation.status is FinalEvaluationStatus.INFRA_ERROR:
            infra_errors += 1
        progress.write(f"{qid}: {evaluation.status.value}")
        await events.append(
            TelemetryEvent(
                run_id=state.run_id,
                task_id=qid,
                graph_id=graph["graph_id"],
                event_type="PRIVATE_EVALUATION_COMPLETED",
                status=evaluation.status.value,
            )
        )
    progress.close()
    export_predictions(predictions, run_dir / "predictions.json")
    print(
        f"evaluated={evaluated}/{len(ids)} "
        f"pass@1={passed / evaluated if evaluated else 0:.4f} "
        f"infra_errors={infra_errors} incomplete={incomplete} cached={cached}"
    )
    return 0 if incomplete == 0 else 1


def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    return asyncio.run(_evaluate(Path(args.run_dir)))


if __name__ == "__main__":
    raise SystemExit(main())
