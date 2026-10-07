"""Joint experiment: seeded first runs (the incumbent is a stored first run, replayed without a model
call) and pair-only searches (R0 + the forced row, no probe, no resample)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

from orchestra.executors import seeded as S
from orchestra.control.fast_loop.controller import FastLoopController
from orchestra.control.fast_loop.evolution_config import EvolutionConfig
from orchestra.executors.agent import seeded_result


def test_only_the_seeded_milestones_first_attempt_workspace_is_seeded(monkeypatch) -> None:
    monkeypatch.setenv(S.SEED_DIR_ENV, "/seed/repo")
    monkeypatch.setenv(S.SEED_MILESTONE_ENV, "m1")
    assert S.seed_for("/r/tasks/rb_t/workspaces/m1/repo") == Path("/seed/repo")
    assert S.seed_for("/r/tasks/rb_t/subtasks/m1/candidates/cand_R0/repo") is None
    assert S.seed_for("/r/tasks/rb_t/workspaces/m2/repo") is None
    monkeypatch.delenv(S.SEED_DIR_ENV)
    assert S.seed_for("/r/tasks/rb_t/workspaces/m1/repo") is None


def _git(d: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base", "--allow-empty"], cwd=d, check=True)


def test_replay_makes_the_workspace_the_stored_first_run_and_returns_its_diff(tmp_path) -> None:
    ws = tmp_path / "workspaces" / "m1" / "repo"
    ws.mkdir(parents=True)
    (ws / "pkg").mkdir()
    (ws / "pkg" / "a.py").write_text("old = 1\n")
    (ws / "stale.py").write_text("x\n")
    (ws / "spec_tests").mkdir()
    (ws / "spec_tests" / "test_x.py").write_text("frozen\n")
    _git(ws)
    seed = tmp_path / "seed"
    (seed / "pkg").mkdir(parents=True)
    (seed / "pkg" / "a.py").write_text("new = 2\n")
    (seed / "pkg" / "b.py").write_text("b = 3\n")
    req = SimpleNamespace(request_id="r1", task_id="t", node_id="agent_1_builder_implementer")
    ctx = SimpleNamespace(subtask_id="m1", task_id="t", workspace_ref=str(ws))
    res = asyncio.run(seeded_result(req, ctx, seed))
    assert res.status.value == "success" and res.backend_id == "seeded_replay"
    assert (ws / "pkg" / "a.py").read_text() == "new = 2\n" and (ws / "pkg" / "b.py").is_file()
    assert not (ws / "stale.py").exists()
    assert (ws / "spec_tests" / "test_x.py").read_text() == "frozen\n"   # frozen suite untouched
    patch = res.output_artifacts[0].payload["patch"] if isinstance(res.output_artifacts[0].payload, dict) else res.output_artifacts[0].payload.patch
    assert "new = 2" in patch
    again = asyncio.run(seeded_result(req, ctx, seed))   # a later first-stage node: nothing left to sync
    assert again.backend_metadata["synced_files"] == 0


def test_pair_only_draws_no_probe(monkeypatch) -> None:
    evo = EvolutionConfig.from_config({"evolution": {"enabled": True}})
    inc = SimpleNamespace(behaviour_failures=["a"], behaviour_score=0.5)
    assert FastLoopController._more_probes_wanted(evo, inc, [])[0] is True
    monkeypatch.setenv(S.PAIR_ONLY_ENV, "1")
    more, why = FastLoopController._more_probes_wanted(evo, inc, [])
    assert more is False and "pair only" in why
