import csv
import json
from pathlib import Path


def _mean(values):
    return sum(values) / len(values) if values else None


def summarize_run(run_dir: str | Path) -> dict:
    root = Path(run_dir)
    task_dirs = sorted((root / "tasks").glob("*")) if (root / "tasks").exists() else []
    hidden = []
    public_initial = []
    public_repaired = []
    compile_success = []
    wall = []
    total_node = []
    critical = []
    speedup = []
    failed_nodes = skipped_nodes = checkpoints = 0

    for task_dir in task_dirs:
        evaluation = task_dir / "final_evaluation.json"
        if evaluation.exists():
            hidden.append(bool(json.loads(evaluation.read_text())["passed"]))
        graph_result = task_dir / "graph_result.json"
        if graph_result.exists():
            result = json.loads(graph_result.read_text())
            wall.append(result["wall_latency_ms"])
            total_node.append(result["sum_node_latency_ms"])
            critical.append(result["critical_path_latency_ms"])
            speedup.append(result["concurrency_speedup"])
            failed_nodes += result["failed_node_count"]
            skipped_nodes += result["skipped_node_count"]
            checkpoints += result["state"]["checkpoint_count"]
        harness_artifacts = []
        for path in (task_dir / "artifacts").glob("*.json"):
            artifact = json.loads(path.read_text())
            if artifact.get("artifact_type") == "PublicHarnessResultArtifact":
                harness_artifacts.append(artifact)
        harness_artifacts.sort(key=lambda item: item["created_at"])
        if harness_artifacts:
            first = harness_artifacts[0]["payload"]
            public_initial.append(first["pass_ratio"])
            compile_success.append(bool(first["compile_success"]))
        if len(harness_artifacts) > 1:
            public_repaired.append(harness_artifacts[-1]["payload"]["pass_ratio"])

    prompt_tokens = completion_tokens = 0
    costs = []
    parse_failures = node_events = 0
    event_path = root / "events.jsonl"
    if event_path.exists():
        for line in event_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["event_type"] in {"NODE_COMPLETED", "NODE_FAILED"}:
                node_events += 1
                prompt_tokens += int(event.get("prompt_tokens") or 0)
                completion_tokens += int(event.get("completion_tokens") or 0)
                if event.get("estimated_cost_usd") is not None:
                    costs.append(float(event["estimated_cost_usd"]))
                if "parse" in str((event.get("metadata") or {}).get("error", "")).lower():
                    parse_failures += 1
    summary = {
        "number_of_tasks": len(task_dirs),
        "evaluated_tasks": len(hidden),
        "hidden_pass@1": _mean([float(item) for item in hidden]),
        "public_pass_before_repair": _mean(public_initial),
        "public_pass_after_repair": _mean(public_repaired),
        "compile_success_rate": _mean([float(item) for item in compile_success]),
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "total_model_cost_usd": sum(costs) if costs else None,
        "parse_failure_rate": parse_failures / node_events if node_events else 0.0,
        "average_graph_wall_latency_ms": _mean(wall),
        "average_sum_node_latency_ms": _mean(total_node),
        "average_critical_path_latency_ms": _mean(critical),
        "average_concurrency_speedup": _mean(speedup),
        "failed_node_count": failed_nodes,
        "skipped_node_count": skipped_nodes,
        "checkpoint_count": checkpoints,
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (root / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(summary.items())
    return summary
