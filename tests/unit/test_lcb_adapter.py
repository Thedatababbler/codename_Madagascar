import json

from orchestra.adapters.livecodebench.loader import LiveCodeBenchLoader
from orchestra.adapters.livecodebench.mapper import sample_manifest


def _write_shards(root):
    rows = []
    for index, difficulty in enumerate(("easy", "medium", "hard")):
        rows.append(
            {
                "question_id": f"q{index}",
                "question_title": f"Question {index}",
                "question_content": "Echo the input.",
                "platform": "atcoder",
                "contest_id": "c",
                "contest_date": "2025-01-01T00:00:00",
                "starter_code": "",
                "difficulty": difficulty,
                "public_test_cases": json.dumps(
                    [{"input": "1", "output": "1", "testtype": "stdin"}]
                ),
                "private_test_cases": json.dumps(
                    [{"input": "2", "output": "2", "testtype": "stdin"}]
                ),
                "metadata": "{}",
            }
        )
    (root / "test.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def test_mapping_and_private_isolation(tmp_path):
    _write_shards(tmp_path)
    loader = LiveCodeBenchLoader(tmp_path, "release_v1")
    tasks = loader.load()
    assert len(tasks) == 3
    assert tasks[0].problem.public_examples[0].input == "1"
    assert "private" not in tasks[0].model_dump_json().lower()
    hidden = loader.private_repository.get_for_final_evaluation("q0")
    assert hidden.private_tests[0].input == "2"


def test_manifest_sampling_is_deterministic(tmp_path):
    _write_shards(tmp_path)
    tasks = LiveCodeBenchLoader(tmp_path, "release_v1").load()
    kwargs = dict(
        tasks=tasks,
        release_version="release_v1",
        seed=42,
        counts={"easy": 1, "medium": 1, "hard": 1},
        excluded_ids=set(),
        livecodebench_commit="abc",
    )
    first = sample_manifest(**kwargs)
    second = sample_manifest(**kwargs)
    assert first == second
    assert len({entry.question_id for entry in first.entries}) == 3
