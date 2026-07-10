from pathlib import Path

from orchestra.runtime.state import RuntimeState


class CheckpointDriftError(RuntimeError):
    pass


class CheckpointStore:
    def __init__(self, run_dir: str | Path) -> None:
        self.root = Path(run_dir) / "tasks"

    def path(self, task_id: str) -> Path:
        return self.root / task_id / "checkpoint.json"

    async def save(self, state: RuntimeState) -> None:
        path = self.path(state.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)

    async def load(
        self,
        task_id: str,
        *,
        graph_hash: str,
        contract_hash: str,
        allow_config_drift: bool = False,
    ) -> RuntimeState | None:
        path = self.path(task_id)
        if not path.exists():
            return None
        state = RuntimeState.model_validate_json(path.read_text(encoding="utf-8"))
        drift = state.graph_hash != graph_hash or state.contract_hash != contract_hash
        if drift and not allow_config_drift:
            raise CheckpointDriftError(
                f"Checkpoint config drift for {task_id}; use --allow-config-drift explicitly"
            )
        return state
