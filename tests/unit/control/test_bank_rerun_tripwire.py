"""Stage 5 of the self-evolution spec: the bank, re-runs and the sealed held-out tripwire."""

from __future__ import annotations

import json
import re
from pathlib import Path

from orchestra.control.evolution.bank import AdmissionPolicy, Bank, BankEntry, suite_version_of
from orchestra.control.evolution.rerun import (
    FORCE_F_ENV,
    FORCE_ROW_ENV,
    cli_command,
    f_entry_verdict,
    forced_f_for,
    forced_row_for,
    merge_rerun_records,
    pair_row_records,
    paired_case_stats,
    plan_f_reruns,
    plan_row_reruns,
)
from orchestra.control.evolution.tripwire import check_tripwire, record_tripwire

SPLIT = Path("configs/datasets/evolution_split.yaml")


def _entry(task, mid, outcome="gate_failed", classes=("E3",), stale=False, features=None):
    return BankEntry(
        entry_id=f"{task}:{mid}:abc12345", task_id=task, milestone_id=mid, outcome=outcome,
        frozen_suite_ref=f"/h/{mid}.spec_tests", predecessor_snapshot={"repo": "/repo", "revision": "abc12345"},
        persistent_failures={f"t.py::{c}_{i}": c for i, c in enumerate(classes)}, suite_stale=stale,
        features=dict(features or {}), run_dir="/runs/x",
    )


def test_test_tasks_never_enter_the_bank_and_successes_are_sampled(tmp_path) -> None:
    bank = Bank(tmp_path / "bank")
    policy = AdmissionPolicy(success_sample_rate=0.0, split_file=SPLIT)
    admitted = bank.admit([
        _entry("pyjwt", "m1"),                      # test task
        _entry("tinydb", "m1"),                     # train, failed -> in
        _entry("tinydb", "m2", outcome="low_score"),  # train, low -> in
        _entry("tinydb", "m3", outcome="success"),  # train, success at rate 0 -> out
    ], policy)
    assert [e.milestone_id for e in admitted] == ["m1", "m2"]
    assert all(e.task_id == "tinydb" for e in bank.entries())
    everything = AdmissionPolicy(success_sample_rate=1.0, split_file=SPLIT)
    assert [e.milestone_id for e in bank.admit([_entry("tinydb", "m3", outcome="success")], everything)] == ["m3"]
    # a newer run of the same milestone supersedes the older entry
    newer = _entry("tinydb", "m1")
    newer.entry_id = "tinydb:m1:def67890"
    bank.admit([newer], policy)
    ids = {e.entry_id for e in bank.entries()}
    assert "tinydb:m1:def67890" in ids and "tinydb:m1:abc12345" not in ids
    assert (bank.root / "archive").is_dir() and bank.class_counts()["E3"] == 3


def test_stale_entries_are_excluded_and_invalidation_archives(tmp_path) -> None:
    bank = Bank(tmp_path / "bank")
    policy = AdmissionPolicy(success_sample_rate=1.0, split_file=SPLIT)
    bank.admit([_entry("tinydb", "m1"), _entry("tinydb", "m2")], policy)
    assert bank.mark_suite_stale("tinydb", "m1") == 1
    assert [e.milestone_id for e in bank.entries()] == ["m2"]
    assert len(bank.entries(include_stale=True)) == 2
    plan = plan_row_reruns(bank.entries(include_stale=True), row_id="E3-S1", error_class="E3", reps=2, needed=5, max_runs=40)
    assert {j.milestone_id for j in plan.jobs} == {"m2"} and plan.skipped["tinydb:m1:abc12345"] == "suite_stale"
    assert bank.invalidate_task("tinydb") == 2 and bank.entries(include_stale=True) == []


def test_rerun_plans_respect_the_budget_and_force_the_variant(monkeypatch, tmp_path) -> None:
    entries = [_entry("tinydb", f"m{i}") for i in range(1, 6)]
    plan = plan_row_reruns(entries, row_id="E3-S1", error_class="E3", reps=2, needed=5, max_runs=6)
    assert plan.runs == 6 and all(j.env[FORCE_ROW_ENV] == f"{j.milestone_id}=E3-S1" for j in plan.jobs)
    assert all(j.control == "R0" and j.kind == "row" for j in plan.jobs)
    ok = [_entry("tinydb", f"s{i}", outcome="success", features={"kind": "foundation"}) for i in range(3)]
    bad = [_entry("tinydb", f"f{i}", features={"kind": "foundation"}) for i in range(4)]
    fplan = plan_f_reruns(bad + ok, entry_id="F1", matches=lambda f: f.get("kind") == "foundation", reps=1, needed=5, max_runs=40, success_share=0.3)
    kinds = {j.variant for j in fplan.jobs}
    assert kinds == {"F0", "F1"} and sum(1 for j in fplan.jobs if j.milestone_id.startswith("s")) == 4  # 2 success entries x 2 versions
    cmd = cli_command(plan.jobs[0], entries[0], config="c.yaml", output_root=tmp_path, dataset_root=None)
    assert "--only-milestone" in cmd and "--base-snapshot" in cmd and cmd[cmd.index("--base-snapshot") + 1] == "/repo@abc12345"
    monkeypatch.setenv(FORCE_ROW_ENV, "m2=E3-S1")
    assert forced_row_for("m2") == "E3-S1" and forced_row_for("m1") is None
    monkeypatch.setenv(FORCE_F_ENV, "F1")
    assert forced_f_for("anything") == "F1"


def test_paired_statistics_and_the_f_verdict() -> None:
    control = [{"a": "fail", "b": "pass", "c": "fail"}, {"a": "fail", "b": "pass", "c": "pass"}]
    variant = [{"a": "pass", "b": "fail", "c": "pass"}, {"a": "pass", "b": "fail", "c": "pass"}]
    stats = paired_case_stats(control, variant)
    # a: stable fix; b: stable regression; c: flaky in the control -> neither
    assert stats["stable_fixes"] == ["a"] and stats["stable_regressions"] == ["b"] and stats["net"] == 0
    recs = [
        {"candidate_kind": "R0", "row_id": "", "per_case_results": {"a": "fail"}},
        {"candidate_kind": "row", "row_id": "E3-S1~swap", "per_case_results": {"a": "pass"}},
    ]
    assert pair_row_records(recs, "E3-S1")["stable_fixes"] == ["a"]
    merged = merge_rerun_records([{"x": 1}], [{"x": 2}])
    assert [r["source"] for r in merged] == ["online", "rerun"]
    good = f_entry_verdict(net_effect=1, cost_variant=1.1, cost_control=1.0, target_before=4, target_after=2, checkpoints=5)
    assert good.promote
    bad = f_entry_verdict(net_effect=1, cost_variant=1.3, cost_control=1.0, target_before=4, target_after=4, checkpoints=3, tripwire_fired=True)
    assert not bad.promote and len(bad.reasons) == 4


def test_tripwire_fires_only_when_gate_and_heldout_disagree(tmp_path) -> None:
    sealed = tmp_path / "heldout_tripwire.jsonl"
    record_tripwire([{"record_id": "b1", "heldout_attributed_subset_pass_rate": 0.80},
                     {"record_id": "b2", "heldout_attributed_subset_pass_rate": 0.82},
                     {"record_id": "p1", "heldout_attributed_subset_pass_rate": 0.70}], sealed)
    fired, note = check_tripwire(promoted_record_ids=["p1"], baseline_record_ids=["b1", "b2"], gate_net_fix=2, tolerance=2.0, path=sealed)
    assert fired and "lower" in note
    ok, _ = check_tripwire(promoted_record_ids=["p1"], baseline_record_ids=["b1", "b2"], gate_net_fix=-1, tolerance=2.0, path=sealed)
    assert not ok
    missing, note = check_tripwire(promoted_record_ids=["nope"], baseline_record_ids=["b1"], gate_net_fix=2, path=sealed)
    assert not missing and "no sealed" in note


def test_only_the_tripwire_module_reads_the_sealed_file() -> None:
    """§5.4: the search, the selection, the commit logic and the evolver never open the sealed file."""
    src = Path("src/orchestra")
    readers = []
    for p in src.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "heldout_tripwire" in text and p.name != "tripwire.py":
            readers.append(str(p))
    assert readers == [], readers
    writer_only = Path("scripts/heldout_tripwire_record.py").read_text(encoding="utf-8")
    assert "record_tripwire" in writer_only and "check_tripwire" not in writer_only


def test_suite_version_hashes_the_frozen_suite_content(tmp_path) -> None:
    d = tmp_path / "m.spec_tests"
    d.mkdir()
    (d / "test_a.py").write_text("def test_a():\n    assert 1\n")
    v1 = suite_version_of(d)
    (d / "test_a.py").write_text("def test_a():\n    assert 2\n")
    assert v1 and v1 != suite_version_of(d) and suite_version_of(tmp_path / "missing") == ""


def test_cli_rerun_helpers_overlay_inherit_and_restrict(tmp_path) -> None:
    """--base-snapshot, --inherit-harness and --only-milestone as the re-run launcher uses them."""
    import subprocess

    from orchestra.cli.run_codeprojecteval_decomp import (
        inherit_harness_sidecars,
        overlay_snapshot,
        restrict_to_milestone,
    )
    from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
    from orchestra.decomposition.schemas import BudgetSpec, SubtaskSpec, TaskPlan

    canon = tmp_path / "canon"
    canon.mkdir()
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=canon, check=True, capture_output=True, text=True)
    git("init", "-q")
    (canon / "pkg.py").write_text("VERSION = 1\n")
    git("add", "-A"); git("commit", "-q", "-m", "one")
    rev = git("rev-parse", "HEAD").stdout.strip()
    (canon / "pkg.py").write_text("VERSION = 2\n")
    git("add", "-A"); git("commit", "-q", "-m", "two")

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "docs.md").write_text("dataset inputs\n")
    overlay_snapshot(ws, f"{canon}@{rev}")
    assert (ws / "pkg.py").read_text() == "VERSION = 1\n" and not (ws / "docs.md").exists()

    old = tmp_path / "old_harness"
    (old / "m1.spec_tests").mkdir(parents=True)
    (old / "m1.spec_tests" / "test_x.py").write_text("def test_x(): pass\n")
    (old / "m1.contracts.json").write_text("{}")
    (old / "m1.spec_tests.committed_cases.json").write_text("{}")
    (old / "unrelated.txt").write_text("no")
    new = tmp_path / "new_harness"
    new.mkdir()
    copied = inherit_harness_sidecars(old, new)
    assert sorted(copied) == ["m1.contracts.json", "m1.spec_tests", "m1.spec_tests.committed_cases.json"]
    assert not (new / "unrelated.txt").exists()

    def spec(sid, deps):
        return SubtaskSpec(subtask_id=sid, title=sid, objective=sid, dependencies=deps, keystone_harness_id="h",
                           local_graph_template="configs/graphs/codex_single_implementer.yaml", budget=BudgetSpec(), expected_outputs=[])

    plan = TaskPlan(task_id="t", plan_version=1, decomposition_rationale="x",
                    subtasks=[spec("m1", []), spec("m2", ["m1"]), spec("m3", ["m2"])])
    state = TaskExecutionState.from_plan(plan, artifact_store_ref=str(tmp_path / "a"))
    restrict_to_milestone(state, "m2")
    assert state.subtasks["m1"].status == SubtaskStatus.COMMITTED
    assert state.subtasks["m2"].status == SubtaskStatus.READY
    assert state.subtasks["m3"].status == SubtaskStatus.SKIPPED
