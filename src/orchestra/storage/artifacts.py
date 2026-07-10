import json
from abc import ABC, abstractmethod
from pathlib import Path

from orchestra.ir.artifacts import ArtifactEnvelope


class ArtifactStore(ABC):
    @abstractmethod
    async def put(self, artifact: ArtifactEnvelope) -> None: ...

    @abstractmethod
    async def get(self, artifact_id: str) -> ArtifactEnvelope: ...

    @abstractmethod
    async def list_for_task(self, task_id: str) -> list[ArtifactEnvelope]: ...


class FileArtifactStore(ArtifactStore):
    def __init__(self, run_dir: str | Path) -> None:
        self.root = Path(run_dir) / "tasks"

    def _path(self, task_id: str, artifact_id: str) -> Path:
        return self.root / task_id / "artifacts" / f"{artifact_id}.json"

    async def put(self, artifact: ArtifactEnvelope) -> None:
        artifact.validate_payload()
        path = self._path(artifact.task_id, artifact.artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = artifact.model_dump_json(indent=2)
        if path.exists():
            if path.read_text(encoding="utf-8") != encoded:
                raise RuntimeError(f"Immutable artifact already exists: {artifact.artifact_id}")
            return
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(encoded, encoding="utf-8")
        tmp.replace(path)

    async def get(self, artifact_id: str) -> ArtifactEnvelope:
        matches = list(self.root.glob(f"*/artifacts/{artifact_id}.json"))
        if len(matches) != 1:
            raise KeyError(artifact_id)
        artifact = ArtifactEnvelope.model_validate_json(matches[0].read_text(encoding="utf-8"))
        artifact.validate_payload()
        return artifact

    async def list_for_task(self, task_id: str) -> list[ArtifactEnvelope]:
        directory = self.root / task_id / "artifacts"
        if not directory.exists():
            return []
        return [
            ArtifactEnvelope.model_validate(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(directory.glob("*.json"))
        ]
