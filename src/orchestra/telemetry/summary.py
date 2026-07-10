import csv
import json
from collections import defaultdict
from pathlib import Path

from orchestra.sandbox.lcb_protocol import FinalEvaluationStatus


def _mean(values):
    return sum(values) / len(values) if values else None


def _task_difficulty(run_dir: Path, task_id: str) -> str | None:
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["manifest"]["entries"]:
        if entry["question_id"] == task_id:
            return entry["difficulty"]
    return None


def summarize_run(run_dir: str | Path) -> dict:
    root = Path(run_dir)
    task_dirs = sorted((root / "tasks").glob("*")) if (root / "tasks").exists() else []
    hidden = []
    hidden_strict = []
    infra_errors = 0
    code_timeouts = 0
    public_initial = []
    public_repaired = []
    compile_success = []
    runtime_errors = 0
    harness_timeouts = 0
    repair_triggered = 0
    repair_success = 0
    wall = []
    total_node = []
    critical = []
    speedup = []
    failed_nodes = skipped_nodes = checkpoints = 0
    by_difficulty: dict[str, dict[str, list]] = defaultdict(
        lambda: {
            "hidden_pass": [],
            "public_initial": [],
            "infra_errors": [],
        }
    )
    role_tokens: dict[str, dict[str, int]] = defaultdict(
        lambda: {"prompt_tokens": 0, "completion_tokens": 0}
    )

    for task_dir in task_dirs:
        task_id = task_dir.name
        difficulty = _task_difficulty(root, task_id)
        evaluation = task_dir / "final_evaluation.json"
        if evaluation.exists():
            payload = json.loads(evaluation.read_text(encoding="utf-8"))
            status = payload.get(
                "status",
                FinalEvaluationStatus.PASSED
                if payload["passed"]
                else FinalEvaluationStatus.WRONG_ANSWER,
            )
            passed = status == FinalEvaluationStatus.PASSED
            hidden.append(float(passed))
            hidden_strict.append(float(passed))
            if status == FinalEvaluationStatus.INFRA_ERROR:
                infra_errors += 1
            if status == FinalEvaluationStatus.CODE_TIMEOUT:
                code_timeouts += 1
            if difficulty:
                by_difficulty[difficulty]["hidden_pass"].append(float(passed))
                by_difficulty[difficulty]["infra_errors"].append(
                    float(status == FinalEvaluationStatus.INFRA_ERROR)
                )
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
            runtime_errors += int(first.get("runtime_errors", 0) > 0)
            harness_timeouts += int(first.get("timeouts", 0) > 0)
            if difficulty:
                by_difficulty[difficulty]["public_initial"].append(first["pass_ratio"])
            if first.get("repair_eligible"):
                repair_triggered += 1
        if len(harness_artifacts) > 1:
            last = harness_artifacts[-1]["payload"]
            public_repaired.append(last["pass_ratio"])
            if harness_artifacts[0]["payload"].get("repair_eligible") and last["passed"]:
                repair_success += 1

    prompt_tokens = completion_tokens = 0
    costs = []
    parse_failures = node_events = 0
    event_path = root / "events.jsonl"
    if event_path.exists():
        for line in event_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["event_type"] in {"NODE_COMPLETED", "NODE_FAILED"}:
                node_events += 1
                prompt = int(event.get("prompt_tokens") or 0)
                completion = int(event.get("completion_tokens") or 0)
                prompt_tokens += prompt
                completion_tokens += completion
                if event.get("estimated_cost_usd") is not None:
                    costs.append(float(event["estimated_cost_usd"]))
                if "parse" in str((event.get("metadata") or {}).get("error", "")).lower():
                    parse_failures += 1
                contract_id = (event.get("metadata") or {}).get("contract_id")
                if contract_id:
                    role_tokens[contract_id]["prompt_tokens"] += prompt
                    role_tokens[contract_id]["completion_tokens"] += completion

    evaluated = len([path for path in task_dirs if (path / "final_evaluation.json").exists()])
    diagnostic_denominator = evaluated - infra_errors
    summary = {
        "number_of_tasks": len(task_dirs),
        "evaluated_tasks": evaluated,
        "hidden_pass@1": _mean(hidden),
        "hidden_pass@1_strict": _mean(hidden_strict),
        "hidden_pass@1_diagnostic": (
            sum(hidden) / diagnostic_denominator if diagnostic_denominator else None
        ),
        "infrastructure_error_rate": infra_errors / evaluated if evaluated else 0.0,
        "infrastructure_error_count": infra_errors,
        "code_timeout_count": code_timeouts,
        "public_pass_before_repair": _mean(public_initial),
        "public_pass_after_repair": _mean(public_repaired),
        "compile_success_rate": _mean([float(item) for item in compile_success]),
        "runtime_error_count": runtime_errors,
        "harness_timeout_count": harness_timeouts,
        "repair_trigger_rate": repair_triggered / len(task_dirs) if task_dirs else 0.0,
        "repair_success_rate": repair_success / repair_triggered if repair_triggered else None,
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "avg_prompt_tokens_per_task": prompt_tokens / len(task_dirs) if task_dirs else None,
        "avg_completion_tokens_per_task": completion_tokens / len(task_dirs) if task_dirs else None,
        "total_model_cost_usd": sum(costs) if costs else None,
        "avg_cost_per_task_usd": _mean(costs) if costs else None,
        "cost_per_solved_task_usd": (
            sum(costs) / sum(hidden) if costs and sum(hidden) else None
        ),
        "parse_failure_rate": parse_failures / node_events if node_events else 0.0,
        "average_graph_wall_latency_ms": _mean(wall),
        "average_sum_node_latency_ms": _mean(total_node),
        "average_critical_path_latency_ms": _mean(critical),
        "average_concurrency_speedup": _mean(speedup),
        "failed_node_count": failed_nodes,
        "skipped_node_count": skipped_nodes,
        "checkpoint_count": checkpoints,
        "by_difficulty": {
            difficulty: {
                "hidden_pass@1": _mean(values["hidden_pass"]),
                "public_pass_before_repair": _mean(values["public_initial"]),
                "infrastructure_error_rate": _mean(values["infra_errors"]),
                "task_count": len(values["hidden_pass"]) or len(values["public_initial"]),
            }
            for difficulty, values in sorted(by_difficulty.items())
        },
        "role_tokens": dict(role_tokens),
    }
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (root / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        for key, value in summary.items():
            if isinstance(value, dict):
                writer.writerow([key, json.dumps(value)])
            else:
                writer.writerow([key, value])
    return summary
