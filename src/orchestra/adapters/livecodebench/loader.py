import base64
import json
import pickle
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from orchestra.schemas.artifacts import ProblemArtifact, PublicExample
from orchestra.schemas.task import (
    AgentVisibleLCBTask,
    HiddenLCBEvaluationRecord,
    LCBTask,
    PrivateTaskData,
    PrivateTestCase,
)

SHARDS = [
    "test.jsonl",
    "test2.jsonl",
    "test3.jsonl",
    "test4.jsonl",
    "test5.jsonl",
    "test6.jsonl",
]
RELEASE_FILES = {f"release_v{i}": SHARDS[:i] for i in range(1, 7)}


def _parse_json_field(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value) if isinstance(value, str) else value


def _parse_private_tests(value: Any) -> list[dict]:
    try:
        return _parse_json_field(value, [])
    except (json.JSONDecodeError, TypeError):
        decoded = pickle.loads(zlib.decompress(base64.b64decode(value.encode("utf-8"))))
        return json.loads(decoded) if isinstance(decoded, str) else decoded


class PrivateTestRepository:
    """Isolated hidden-test store. No method exposes data to normal artifacts."""

    def __init__(self) -> None:
        self._items: dict[str, PrivateTaskData] = {}

    def add(self, item: PrivateTaskData) -> None:
        self._items[item.question_id] = item

    def get_for_final_evaluation(self, question_id: str) -> PrivateTaskData:
        return self._items[question_id]


class LiveCodeBenchLoader:
    def __init__(self, data_dir: str | Path, release_version: str = "release_v6") -> None:
        if release_version not in RELEASE_FILES:
            raise ValueError(f"Unsupported pinned release: {release_version}")
        self.data_dir = Path(data_dir)
        self.release_version = release_version
        self.private_repository = PrivateTestRepository()

    def iter_raw(self) -> Iterable[dict]:
        for name in RELEASE_FILES[self.release_version]:
            path = self.data_dir / name
            if not path.exists():
                raise FileNotFoundError(path)
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        yield json.loads(line)

    def load(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        difficulties: set[str] | None = None,
        platforms: set[str] | None = None,
        task_ids: set[str] | None = None,
    ) -> list[LCBTask]:
        result = []
        for raw in self.iter_raw():
            qid = str(raw["question_id"])
            date = str(raw.get("contest_date", ""))[:10]
            difficulty = str(raw.get("difficulty", "")).lower()
            platform = str(raw.get("platform", "")).lower()
            if start_date and date < start_date:
                continue
            if end_date and date > end_date:
                continue
            if difficulties and difficulty not in difficulties:
                continue
            if platforms and platform not in platforms:
                continue
            if task_ids and qid not in task_ids:
                continue

            metadata = _parse_json_field(raw.get("metadata"), {})
            public_raw = _parse_json_field(raw.get("public_test_cases"), [])
            public = [
                PublicExample(
                    input=str(item.get("input", "")),
                    output=str(item.get("output", "")),
                    testtype=str(item.get("testtype", "stdin")),
                )
                for item in public_raw
            ]
            private = [
                PrivateTestCase(
                    input=str(item.get("input", "")),
                    output=str(item.get("output", "")),
                    testtype=str(item.get("testtype", "stdin")),
                )
                for item in _parse_private_tests(raw.get("private_test_cases"))
            ]
            function_name = metadata.get("func_name")
            problem = ProblemArtifact(
                question_id=qid,
                title=str(raw.get("question_title", "")),
                statement=str(raw.get("question_content", "")),
                starter_code=str(raw.get("starter_code", "") or ""),
                difficulty=difficulty,
                platform=platform,
                contest_date=str(raw.get("contest_date", "")),
                public_examples=public,
                function_name=function_name,
            )
            self.private_repository.add(
                PrivateTaskData(
                    question_id=qid,
                    private_tests=private,
                    public_tests=public,
                    function_name=function_name,
                )
            )
            result.append(
                LCBTask(
                    question_id=qid,
                    release_version=self.release_version,
                    problem=problem,
                    metadata={"contest_id": raw.get("contest_id", "")},
                )
            )
        return sorted(result, key=lambda item: item.question_id)

    def load_visible(self, **filters):
        tasks = self.load(**filters)
        visible = [
            AgentVisibleLCBTask(
                question_id=task.question_id,
                question_title=task.problem.title,
                question_content=task.problem.statement,
                platform=task.problem.platform,
                contest_date=task.problem.contest_date,
                starter_code=task.problem.starter_code,
                difficulty=task.problem.difficulty,
                public_test_cases=task.problem.public_examples,
                function_name=task.problem.function_name,
                metadata_public=task.metadata,
            )
            for task in tasks
        ]
        hidden_refs = [
            HiddenLCBEvaluationRecord(
                question_id=task.question_id,
                private_test_cases_ref=task.question_id,
            )
            for task in tasks
        ]
        return visible, hidden_refs
