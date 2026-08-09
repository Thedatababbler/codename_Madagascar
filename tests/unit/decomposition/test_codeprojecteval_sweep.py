"""The sweep driver: trial matrix, concurrency, and the joined trial record."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "cpe_sweep",
    Path(__file__).resolve().parents[3] / "scripts" / "run_codeprojecteval_sweep.py",
)
assert _SPEC and _SPEC.loader
sweep = importlib.util.module_from_spec(_SPEC)
sys.modules["cpe_sweep"] = sweep
_SPEC.loader.exec_module(sweep)


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    plans = tmp_path / "plans"
    plans.mkdir(exist_ok=True)
    for name in ("pyjwt.solo.json", "pyjwt.multi.json"):
        (plans / name).write_text("{}", encoding="utf-8")
    base = {
        "tasks": ["pyjwt"],
        "arms": ["solo", "multi"],
        "repeats": 2,
        "plans": plans,
        "output_root": tmp_path / "out",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_matrix_covers_every_task_arm_and_repeat(tmp_path: Path) -> None:
    trials = sweep.build_trials(_args(tmp_path), "STAMP")

    assert len(trials) == 4
    assert {(t.arm, t.seed) for t in trials} == {
        ("solo", 1), ("solo", 2), ("multi", 1), ("multi", 2)
    }
    # Distinct run ids, because trials share an output root and a collision
    # would have one trial score another's repository.
    assert len({t.run_id for t in trials}) == 4


def test_a_missing_frozen_plan_fails_before_any_compute_is_spent(tmp_path: Path) -> None:
    args = _args(tmp_path, arms=["solo", "nonexistent"])
    with pytest.raises(SystemExit, match="missing frozen plan"):
        sweep.build_trials(args, "STAMP")


def test_trials_actually_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four trials at concurrency four must not take four times one trial.

    The whole point of the driver is that these are independent processes; a
    thread pool that serialised them would look identical from the outside
    except for taking a day.
    """
    live = 0
    peak = 0
    guard = threading.Lock()

    def fake_execute(trial: object, args: object) -> sweep.TrialResult:
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        time.sleep(0.2)
        with guard:
            live -= 1
        return sweep.TrialResult(
            task_id=trial.task_id, arm=trial.arm, seed=trial.seed,
            run_id=trial.run_id, status="ok", pass_rate=0.5,
        )

    monkeypatch.setattr(sweep, "execute", fake_execute)
    record = tmp_path / "trials.jsonl"
    monkeypatch.setattr(
        sys, "argv",
        [
            "sweep", "--tasks", "pyjwt", "--arms", "solo,multi", "--repeats", "2",
            "--concurrency", "4", "--plans", str(_args(tmp_path).plans),
            "--output-root", str(tmp_path / "out"), "--out", str(record),
        ],
    )
    started = time.monotonic()
    assert sweep.main() == 0
    elapsed = time.monotonic() - started

    assert peak > 1, "trials ran one at a time"
    assert elapsed < 0.6, "four 0.2s trials took as long as running them in series"
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4


def test_repeats_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """n above 5 is more compute than this experiment has agreed to spend."""
    monkeypatch.setattr(
        sys, "argv", ["sweep", "--tasks", "pyjwt", "--repeats", "6"]
    )
    with pytest.raises(SystemExit, match="repeats above 5"):
        sweep.main()


def test_record_joins_run_scoring_and_usage_into_one_row(tmp_path: Path) -> None:
    """The three artefacts a trial leaves behind must arrive as a single row."""
    batch = tmp_path / "batch"
    (batch / "pyjwt" / "logs" / "02_runtime").mkdir(parents=True)
    (batch / "batch_summary.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "task_id": "pyjwt", "milestone_count": 2, "agent_turns": 4,
                        "committed": ["m1", "m2"], "latency_ms": 660_000, "error": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (batch / "hidden_eval.json").write_text(
        json.dumps(
            {"results": [{"task_id": "pyjwt", "pass_rate": 0.74, "pass_rate_reachable": 0.9,
                          "status": "fail"}]}
        ),
        encoding="utf-8",
    )
    events = [
        {"event_type": "NODE_COMPLETED", "node_id": "agent_1_author",
         "prompt_tokens": 1000, "completion_tokens": 100},
        {"event_type": "CONDITIONAL_EDGE_ACTIVATED",
         "metadata": {"edge_id": "probe_pass_to_freeze"}},
        {"event_type": "NODE_COMPLETED", "node_id": "agent_2_author",
         "prompt_tokens": 500, "completion_tokens": 50},
        {"event_type": "CONDITIONAL_EDGE_ACTIVATED",
         "metadata": {"edge_id": "gate_fail_to_agent_2_repairer"}},
        {"event_type": "NODE_COMPLETED", "node_id": "repository_tests"},
    ]
    (batch / "pyjwt" / "logs" / "02_runtime" / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )

    trial = sweep.Trial("pyjwt", "multi", 1, "run-1", Path("plan.json"))
    row = sweep.collect_result(trial, batch, status="ok")

    assert row.pass_rate == 0.74
    assert row.milestones == 2
    assert row.committed == 2
    assert row.wall_clock_seconds == 660.0
    # Planned turns come from the plan, run turns from the event stream; an
    # early exit makes them differ, and reporting only the first would hide it.
    assert (row.agent_turns_planned, row.agent_turns_run) == (4, 2)
    assert row.prompt_tokens + row.completion_tokens == 1650
    assert (row.gates_passed, row.gates_failed) == (1, 1)


def test_an_unmeasured_run_is_not_recorded_as_a_scored_zero(tmp_path: Path) -> None:
    """A hidden suite that timed out says nothing about the configuration."""
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "hidden_eval.json").write_text(
        json.dumps({"results": [{"task_id": "pyjwt", "pass_rate": 0.0, "status": "timeout"}]}),
        encoding="utf-8",
    )

    row = sweep.collect_result(
        sweep.Trial("pyjwt", "solo", 1, "run-1", Path("plan.json")), batch, status="ok"
    )

    assert row.status == "timeout"
