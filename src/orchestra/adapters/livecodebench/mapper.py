import hashlib
import json
import random
from pathlib import Path

from pydantic import BaseModel, Field

from orchestra.schemas.task import LCBTask


class ManifestEntry(BaseModel):
    question_id: str
    title: str
    difficulty: str
    platform: str
    contest_date: str


class LCBManifest(BaseModel):
    schema_version: str = "1.0"
    release_version: str
    seed: int
    entries: list[ManifestEntry] = Field(default_factory=list)
    livecodebench_commit: str
    manifest_sha256: str | None = None

    def finalized(self) -> "LCBManifest":
        payload = self.model_dump(exclude={"manifest_sha256"})
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self.model_copy(update={"manifest_sha256": digest})


def sample_manifest(
    tasks: list[LCBTask],
    *,
    release_version: str,
    seed: int,
    counts: dict[str, int],
    excluded_ids: set[str],
    livecodebench_commit: str,
) -> LCBManifest:
    rng = random.Random(seed)
    selected: list[LCBTask] = []
    for difficulty, count in counts.items():
        candidates = [
            item
            for item in tasks
            if item.problem.difficulty == difficulty and item.question_id not in excluded_ids
        ]
        if len(candidates) < count:
            raise ValueError(
                f"Need {count} {difficulty} tasks, only {len(candidates)} available"
            )
        rng.shuffle(candidates)
        selected.extend(candidates[:count])
    entries = [
        ManifestEntry(
            question_id=item.question_id,
            title=item.problem.title,
            difficulty=item.problem.difficulty,
            platform=item.problem.platform,
            contest_date=item.problem.contest_date,
        )
        for item in selected
    ]
    if len({entry.question_id for entry in entries}) != len(entries):
        raise ValueError("Manifest contains duplicate question IDs")
    return LCBManifest(
        release_version=release_version,
        seed=seed,
        entries=entries,
        livecodebench_commit=livecodebench_commit,
    ).finalized()


def save_manifest(manifest: LCBManifest, path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def load_manifest(path: str | Path) -> LCBManifest:
    return LCBManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))
