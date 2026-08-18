"""The sweep driver: trial matrix, concurrency, and the joined trial record."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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


def test_a_probe_samples_the_planner_instead_of_replaying_a_plan(tmp_path: Path) -> None:
    """Probes look for a repository whose gate fails; nothing is frozen yet."""
    trials = sweep.build_trials(_args(tmp_path, arms=["planner"], repeats=1), "STAMP")

    assert [t.plan_file for t in trials] == [None]
    # Named apart from A/B runs so a summariser cannot average the two together.
    assert trials[0].run_id.startswith("probe-")


def test_the_command_carries_the_config_and_omits_a_plan_it_does_not_have(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair loop is off in the default config, so a tuning run must
    be able to name a different one."""
    seen: list[list[str]] = []

    def fake_run(cmd, *, log, timeout, codex_home=None):  # noqa: ANN001
        seen.append(cmd)
        return 1  # non-zero: skip scoring, which needs a real batch directory

    monkeypatch.setattr(sweep, "_run", fake_run)
    monkeypatch.setattr(sweep, "isolated_codex_home", lambda *a, **k: None)
    args = _args(
        tmp_path,
        arms=["planner"],
        repeats=1,
        config=tmp_path / "tuning.yaml",
        run_timeout=1.0,
        eval_timeout=1.0,
        per_test_timeout=5.0,
    )
    trial = sweep.build_trials(args, "STAMP")[0]

    sweep.execute(trial, args)

    cmd = seen[0]
    assert "--config" in cmd
    assert str(tmp_path / "tuning.yaml") in cmd
    assert "--plan-file" not in cmd


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


def test_each_trial_gets_its_own_codex_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent Codex processes cannot share one CODEX_HOME.

    The CLI keeps its state in SQLite databases there, and the loser of the race
    dies at startup with "database is locked" -- which is how the first parallel
    sweep failed within six seconds.
    """
    source = tmp_path / "codex"
    source.mkdir()
    (source / "auth.json").write_text('{"token": "x"}', encoding="utf-8")
    (source / "config.toml").write_text("model = 'gpt-5.4'", encoding="utf-8")
    (source / "state_5.sqlite").write_text("shared db", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(source))

    first = sweep.isolated_codex_home("run-1", tmp_path / "out")
    second = sweep.isolated_codex_home("run-2", tmp_path / "out")

    assert first != second
    assert (first / "auth.json").read_text(encoding="utf-8") == '{"token": "x"}'
    assert (first / "config.toml").is_file()
    # The databases are what collide, so they must not be inherited.
    assert not (first / "state_5.sqlite").exists()
    assert sweep.trial_env(first)["CODEX_HOME"] == str(first)


def test_the_untrusted_harness_flag_stays_in_the_subprocess(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting it on this process would disable a safety check everywhere."""
    monkeypatch.delenv("ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS", raising=False)

    env = sweep.trial_env()

    assert env["ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS"] == "1"
    assert "ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS" not in os.environ


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
                        "subtask_status": {"m1": "committed", "m2": "failed"},
                        "milestone_objectives": [
                            {"milestone_id": "m1", "candidate_id": "cand_feedback"},
                            {"milestone_id": "m2", "candidate_id": "main"},
                        ],
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
    # Milestone acceptance gates, not the conditional probe edge inside
    # gate_then_repair, which is counted separately.
    assert (row.gates_passed, row.gates_failed) == (1, 1)
    assert (row.probe_gates_passed, row.probe_gates_failed) == (1, 1)
    # A milestone whose committed work came from a repair candidate is what a
    # tuning run buys, so it is named rather than merely counted.
    assert row.repaired == ["m1"]


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
    assert row.pass_rate is None
    assert row.pass_rate_reachable is None
