"""The shared collect cache under concurrent writers."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from orchestra.codeprojecteval.shared_cache import get_or_compute, read_json, write_json


def test_concurrent_writers_do_not_lose_each_others_entries(tmp_path: Path) -> None:
    """Read-modify-write is lossy without a lock, and silently so.

    Two evaluations finishing together each read the same snapshot; the second
    writes back a copy that has forgotten the first. The cache then looks fine
    and merely recomputes, which is exactly why nobody notices.
    """
    cache = tmp_path / "cache.json"
    ready = threading.Barrier(8)

    def writer(index: int) -> None:
        ready.wait()
        get_or_compute(cache, f"task-{index}", lambda: {"count": index})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stored = json.loads(cache.read_text(encoding="utf-8"))
    assert sorted(stored) == [f"task-{i}" for i in range(8)]


def test_a_cached_value_is_not_recomputed(tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    calls = 0

    def compute() -> int:
        nonlocal calls
        calls += 1
        return 42

    assert get_or_compute(cache, "k", compute) == 42
    assert get_or_compute(cache, "k", compute) == 42
    assert calls == 1


def test_a_corrupt_cache_is_treated_as_empty(tmp_path: Path) -> None:
    """A half-written file must not take the next run down with it."""
    cache = tmp_path / "cache.json"
    cache.write_text('{"task": {"a": 1', encoding="utf-8")

    assert read_json(cache) == {}
    assert get_or_compute(cache, "task", lambda: {"a": 2}) == {"a": 2}


def test_writes_are_atomic(tmp_path: Path) -> None:
    """A replaced cache is never observed partially written."""
    cache = tmp_path / "cache.json"
    write_json(cache, {"a": 1})
    write_json(cache, {"b": 2})

    assert json.loads(cache.read_text(encoding="utf-8")) == {"b": 2}
    assert not list(tmp_path.glob("*.tmp"))


def test_without_a_cache_path_the_value_is_simply_computed(tmp_path: Path) -> None:
    assert get_or_compute(None, "k", lambda: 7) == 7
    assert not list(tmp_path.iterdir())
