"""TaskExecutionState checkpoint store (alongside graph checkpoint.json)."""

from __future__ import annotations

from pathlib import Path

from orchestra.control.task_state import TaskExecutionState


class TaskCheckpointDriftError(RuntimeError):
    pass


class TaskCheckpointStore:
    def __init__(self, run_dir: str | Path) -> None:
        self.root = Path(run_dir) / "tasks"

    def path(self, task_id: str) -> Path:
        return self.root / task_id / "task_execution.json"

    async def save(self, state: TaskExecutionState) -> None:
        path = self.path(state.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)

    async def load(
        self,
        task_id: str,
        *,
        plan_version: int | None = None,
        plan_content_hash: str | None = None,
        allow_config_drift: bool = False,
    ) -> TaskExecutionState | None:
        path = self.path(task_id)
        if not path.exists():
            return None
        state = TaskExecutionState.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        drift_reasons: list[str] = []
        if plan_version is not None and state.task_plan.plan_version != plan_version:
            drift_reasons.append(
                f"plan_version {state.task_plan.plan_version} != {plan_version}"
            )
        expected_hash = plan_content_hash or state.plan_content_hash
        if plan_content_hash is not None and state.plan_content_hash != expected_hash:
            drift_reasons.append("plan_content_hash mismatch")
        if (
            state.plan_content_hash
            and state.plan_content_hash != state.task_plan.content_hash()
        ):
            drift_reasons.append("stored plan_content_hash disagrees with task_plan")
        if drift_reasons and not allow_config_drift:
            raise TaskCheckpointDriftError(
                f"Task checkpoint drift for {task_id}: {'; '.join(drift_reasons)}; "
                "use allow_config_drift explicitly"
            )
        return state
