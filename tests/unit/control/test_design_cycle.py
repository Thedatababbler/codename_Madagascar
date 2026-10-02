"""Stage 6 of the self-evolution spec: promotions, the evolver's validators, replay, versions and rollback."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from orchestra.control.evolution import design_cycle as dc
from orchestra.control.evolution.evolver import parse_reply, propose, unresolved_pool
from orchestra.control.evolution.memory_replay import (
    RowClassStats,
    class_distribution,
    pair_statistics,
    ranking_from_stats,
    shrinkage_score,
    simulate_ranking,
)
from orchestra.control.evolution.validators import (
    available_roles,
    available_templates,
    training_identifiers,
    validate_f_entry,
    validate_row,
)
from orchestra.control.fast_loop.playbook_v2 import (
    PlaybookRow,
    default_repair_rows,
    load_repair_table,
)
from orchestra.control.first_pass.designer import default_first_pass_entries, load_first_pass_table

CFG = dc.CycleConfig(m=5, n0=3, max_trial_concurrent=2)


def _stats(row, cls, deltas, regs=None, r0_regs=None):
    s = RowClassStats(row, cls)
    s.deltas = list(deltas)
    s.costs = [1.0] * len(deltas)
    s.regressions = list(regs or [0] * len(deltas))
    s.r0_regressions = list(r0_regs or [0] * len(deltas))
    s.record_ids = [f"{row}:{i}" for i in range(len(deltas))]
    return s


def _row(rid, cls="E3", state="candidate", origin="new"):
    return PlaybookRow(row_id=rid, table="repair", error_classes=(cls,), action="S", instruction="x", state=state, origin=origin)


def test_trial_promotion_boundaries() -> None:
    rows = [_row("E3-S9", state="trial"), _row("E3-T9", state="trial"), _row("E4-S9", cls="E4", state="trial")]
    stats = {
        ("E3-S9", "E3"): _stats("E3-S9", "E3", [1, 1, 1, 1]),            # m-1 pairs: not judged
        ("E3-T9", "E3"): _stats("E3-T9", "E3", [1, 1, 1, 1, 1]),         # m pairs, positive, no regressions: active
        ("E4-S9", "E4"): _stats("E4-S9", "E4", [1, 1, 1, 1, 1], regs=[2] * 5, r0_regs=[0] * 5),  # regressions above R0: fail
    }
    out, changes = dc.update_row_states(rows, stats, cfg=dc.CycleConfig(m=5, max_trial_concurrent=0))
    by = {r.row_id: r for r in out}
    assert by["E3-S9"].state == "trial" and by["E3-T9"].state == "active"
    assert by["E4-S9"].state == "candidate" and by["E4-S9"].origin.endswith("#trial2")
    # second failed trial retires; an active row with a negative recent score is demoted
    again = [by["E4-S9"].__class__(**{**by["E4-S9"].__dict__, "state": "trial"}), _row("E5-S9", cls="E5", state="active")]
    stats2 = {("E4-S9", "E4"): _stats("E4-S9", "E4", [0, 0, 0, 0, -1]), ("E5-S9", "E5"): _stats("E5-S9", "E5", [1] * 6 + [-2] * 10)}
    out2, _ = dc.update_row_states(again, stats2, cfg=dc.CycleConfig(m=5, max_trial_concurrent=0))
    by2 = {r.row_id: r for r in out2}
    assert by2["E4-S9"].state == "retired" and by2["E5-S9"].state == "candidate"


def test_candidates_fill_trial_slots_newest_origin_first() -> None:
    rows = [_row("E3-S8", origin="evolver:cycle-1"), _row("E3-S9", origin="evolver:cycle-2"), _row("E4-S9", cls="E4"), _row("E5-S9", cls="E5")]
    out, changes = dc.update_row_states(rows, {}, cfg=CFG)
    trials = [r.row_id for r in out if r.state == "trial"]
    assert trials == ["E3-S9", "E4-S9"]  # newest evolver origin wins in E3; two slots overall


def test_shrinkage_ranking_and_replay_evidence_rule() -> None:
    assert shrinkage_score(0, 5.0) == 0.0 and abs(shrinkage_score(3, 2.0, n0=3) - 1.0) < 1e-9
    recs = []
    for i in range(4):
        recs.append({"record_id": f"t:m{i}:R0", "task_id": "t", "milestone_id": f"m{i}", "split": "train", "candidate_kind": "R0", "row_id": "", "net_fix": 0,
                     "error_classes": ["E3"], "regressed": []})
        recs.append({"record_id": f"t:m{i}:a", "task_id": "t", "milestone_id": f"m{i}", "split": "train", "candidate_kind": "row", "row_id": "E3-S1~swap",
                     "error_classes": ["E3"], "delta_vs_R0": 1, "paired_R0_record_id": f"t:m{i}:R0", "regressed": [], "cost": {"usd": 0.1}})
    recs.append({"record_id": "x:m0:b", "task_id": "x", "milestone_id": "m0", "split": "test", "candidate_kind": "row", "row_id": "E3-T1",
                 "error_classes": ["E3"], "delta_vs_R0": 5, "regressed": [], "cost": {"usd": 0.1}})
    stats = pair_statistics(recs)
    assert ("E3-T1", "E3") not in stats  # test-split records never enter the statistics
    ranking = ranking_from_stats(stats, n0=3)
    assert ranking == {"E3": {"E3-S1": round(4 / 7, 4)}}
    sim = simulate_ranking(recs[:-1], old_ranking={}, new_ranking=ranking, table_rows={"E3": ["E3-S1", "E3-T1"]})
    assert sim["comparable"] == 4 and sim["sufficient"]
    sim2 = simulate_ranking(recs[:-1], old_ranking={"E3": {"E3-T1": 9}}, new_ranking=ranking, table_rows={"E3": ["E3-S1", "E3-T1"]})
    assert sim2["verdict"] == "insufficient evidence"
    dist = class_distribution([{"task_id": "t", "milestone_id": "m", "error_classes": ["E6", "E3"]}], min_class_samples=2)
    assert dist["merge_advice"] == {"E6": "E3"}


def test_evolver_rejects_training_identifiers_and_unknown_roles(tmp_path) -> None:
    roles, templates = available_roles(), available_templates()
    ids = training_identifiers(task_ids=["tinydb"], case_names=["test_table.py::test_insert_document"], focus_paths=["tinydb/queries.py"])
    assert "tinydb" in ids and "queries" in ids and "insert_document" in ids
    reply = yaml.safe_dump({
        "rows": [
            {"row_id": "E5-S2", "table": "repair", "error_classes": ["E5"], "action": "S", "instruction": "Mirror tinydb's queries module when raising.",
             "intent": "leak", "targets": "cluster:E5"},
            {"row_id": "E5-R2", "table": "repair", "error_classes": ["E5"], "action": "R", "slot_edits": [{"slot": "reviewer", "role": "wizard", "read_only": True}],
             "intent": "bad role", "targets": "cluster:E5"},
            {"row_id": "E5-T2", "table": "repair", "error_classes": ["E5"], "action": "T", "target_template": "no_such_template",
             "intent": "bad template", "targets": "cluster:E5"},
            {"row_id": "E5-S3", "table": "repair", "error_classes": ["E5"], "action": "S",
             "instruction": "Before writing, list every documented exception and the condition that raises it; add a test-shaped check for each.",
             "intent": "documented exceptions first", "targets": "cluster:E5"},
        ],
        "f_entries": [
            {"entry_id": "F9", "triggers": [{"feature": "n_documented_exceptions", "op": ">=", "value": "thr.f2"}], "predicted_error_classes": ["E5"],
             "actions": [{"kind": "instruction", "text": "List the documented exceptions before implementing."}], "intent": "prevent", "targets": "row:E5-S1",
             "source_rows": ["E5-S1"]},
            {"entry_id": "F10", "triggers": [{"feature": "kind", "op": "==", "value": "foundation"}], "predicted_error_classes": ["E5"],
             "actions": [{"kind": "add_reviewer", "role": "implementer"}], "intent": "writer as reviewer", "targets": "row:E5-S1", "source_rows": ["E5-S1"]},
        ],
    })
    result = propose(prompt="p", out_dir=tmp_path, cycle_id="cycle-7", roles=roles, templates=templates, identifiers=ids,
                     repair_row_ids=[r.row_id for r in default_repair_rows()], f_entry_ids=["F0", "F1"], max_proposals=3, call=lambda s, p: reply)
    assert [r["row_id"] for r in result.rows] == ["E5-S3"] and result.rows[0]["origin"] == "evolver:cycle-7" and result.rows[0]["state"] == "candidate"
    assert [e["entry_id"] for e in result.f_entries] == ["F9"]
    reasons = {r["item"]: " ".join(r["reasons"]) for r in result.rejected}
    assert "training-task identifiers" in reasons["E5-S2"] and "unknown role" in reasons["E5-R2"]
    assert "unknown template" in reasons["E5-T2"] and "read-only role" in reasons["F10"]
    assert (tmp_path / "evolver_prompt.md").is_file() and (tmp_path / "evolver_reply.yaml").is_file()
    no_source = validate_f_entry({"entry_id": "F11", "triggers": [{"feature": "kind", "op": "==", "value": "x"}], "predicted_error_classes": ["E3"],
                                  "actions": [{"kind": "chain"}]}, roles=roles, templates=templates, identifiers=ids, repair_row_ids=["E3-S1"])
    assert not no_source.ok and any("source_rows" in r for r in no_source.reasons)
    assert not validate_row({"row_id": "E3-S1", "error_classes": ["E3"], "action": "S", "instruction": "x", "intent": "i", "targets": "c"},
                            roles=roles, templates=templates, identifiers=ids, existing_ids=["E3-S1"]).ok
    rows, entries = parse_reply("```yaml\nrows: []\nf_entries: []\n```")
    assert rows == [] and entries == []


def test_unresolved_pool_groups_by_class() -> None:
    recs = [
        {"task_id": "t", "milestone_id": "m", "split": "train", "persistent_before": ["a::x", "a::y"], "fixed": ["a::x"], "case_classes": {"a::y": "E5"}},
        {"task_id": "u", "milestone_id": "m", "split": "test", "persistent_before": ["b::z"], "fixed": [], "case_classes": {"b::z": "E5"}},
    ]
    pool = unresolved_pool(recs)
    assert [(c.error_class, [x["case"] for x in c.cases]) for c in pool] == [("E5", ["a::y"])]


def _tables(tmp_path):
    paths = {"repair_path": tmp_path / "repair.yaml", "first_pass_path": tmp_path / "first_pass.yaml", "ranking_path": tmp_path / "ranking.json"}
    from orchestra.control.fast_loop.playbook_v2 import save_repair_table
    from orchestra.control.first_pass.designer import save_first_pass_table

    save_repair_table(default_repair_rows(), paths["repair_path"])
    save_first_pass_table(default_first_pass_entries(), paths["first_pass_path"])
    return paths


def test_publish_and_rollback_demote_newly_active_rows(tmp_path) -> None:
    paths = _tables(tmp_path)
    vdir = tmp_path / "versions"
    rows = list(default_repair_rows())
    dc.publish_version(rows, default_first_pass_entries(), {}, {"promoted_rows": []}, version="v1", versions_dir=vdir, **paths)
    promoted = [r.__class__(**{**r.__dict__, "state": "active"}) if r.row_id == "E5-S1" else r for r in rows]
    dc.publish_version(promoted, default_first_pass_entries(), {"E5": {"E5-S1": 0.5}}, {"promoted_rows": ["E5-S1"]}, version="v2", versions_dir=vdir, **paths)
    assert dc.current_version(vdir) == "v2" and {r.row_id: r.state for r in load_repair_table(paths["repair_path"])}["E5-S1"] == "active"
    version, changes = dc.rollback("v1", versions_dir=vdir, reason="batch fell", **paths)
    assert version == "v3" and changes["demoted_rows"] == ["E5-S1"] and dc.current_version(vdir) == "v3"
    live = {r.row_id: r.state for r in load_repair_table(paths["repair_path"])}
    assert live["E5-S1"] == "candidate" and (vdir / "v2" / "repair.yaml").is_file()  # history kept
    try:
        dc.publish_version(rows, (), {}, {}, version="v2", versions_dir=vdir, **paths)
        raise AssertionError("versions must be immutable")
    except FileExistsError:
        pass


def _ledger(tmp_path, *, with_tripwire_rows=False):
    root = tmp_path / "ledger" / "v1"
    root.mkdir(parents=True)
    cands, miles = [], []
    for i in range(6):
        t, m = "tinydb", f"m{i}"
        cands.append({"record_id": f"{t}:{m}:inc", "task_id": t, "milestone_id": m, "split": "train", "candidate_kind": "incumbent", "row_id": "",
                      "error_classes": ["E3"], "actual_error_classes": {"E3": 2}, "features": {"kind": "middle"}, "committed": False,
                      "persistent_before": [f"a::{m}_x"], "fixed": [], "per_case_results": {f"a::{m}_x": "fail"}, "f_entries_applied": []})
        cands.append({"record_id": f"{t}:{m}:cand_R0", "task_id": t, "milestone_id": m, "split": "train", "candidate_kind": "R0", "row_id": "",
                      "error_classes": ["E3"], "net_fix": 0, "regressed": [], "committed": False, "cost": {"usd": 0.2}, "f_entries_applied": []})
        cands.append({"record_id": f"{t}:{m}:c1", "task_id": t, "milestone_id": m, "split": "train", "candidate_kind": "row", "row_id": "E3-S1",
                      "row_state_at_run": "trial", "error_classes": ["E3"], "delta_vs_R0": 1, "paired_R0_record_id": f"{t}:{m}:cand_R0", "regressed": [],
                      "prior_regressions": [], "committed": True, "net_fix": 1, "cost": {"usd": 0.3}, "fixed": [f"a::{m}_x"], "features": {"kind": "middle"},
                      "workspace_ref": "/w", "f_entries_applied": []})
        miles.append({"task_id": t, "milestone_id": m, "split": "train", "features": {"kind": "middle", "n_documented_exceptions": 6}, "error_classes": ["E3"],
                      "final_status": "committed"})
    (root / "candidates.jsonl").write_text("\n".join(json.dumps(r) for r in cands) + "\n")
    (root / "milestones.jsonl").write_text("\n".join(json.dumps(r) for r in miles) + "\n")
    return tmp_path / "ledger"


def test_run_cycle_report_only_then_publish_and_tripwire(tmp_path) -> None:
    from orchestra.control.evolution.tripwire import record_tripwire
    from orchestra.control.fast_loop.playbook_v2 import save_repair_table

    paths = _tables(tmp_path)
    rows = [r.__class__(**{**r.__dict__, "state": "trial"}) if r.row_id == "E3-S1" else r for r in default_repair_rows()]
    save_repair_table(rows, paths["repair_path"])
    ledger = _ledger(tmp_path)
    vdir, cycles, bank, routing = tmp_path / "versions", tmp_path / "cycles", tmp_path / "bank", tmp_path / "routing.jsonl"
    sealed = tmp_path / "sealed.jsonl"
    common = dict(ledger_root=ledger, bank_root=bank, versions_dir=vdir, cycles_root=cycles, sealed_path=sealed, routing_path=routing, table_paths=paths)

    res = dc.run_cycle(CFG, cycle_id="cycle-1", publish=False, **common)
    assert not res.published and res.version is None and Path(res.report_path).is_file()
    assert res.ranking == {"E3": {"E3-S1": round(6 / 9, 4)}}
    assert [(c.row_id, c.after) for c in res.row_changes if c.row_id == "E3-S1"] == [("E3-S1", "active")]
    assert not (vdir / "v2").exists() and {r.row_id: r.state for r in load_repair_table(paths["repair_path"])}["E3-S1"] == "trial"
    assert (cycles / "cycle-1" / "proposed" / "repair.yaml").is_file()

    # sealed held-out says the promoted row's workspaces score lower than R0's: the promotion is undone and routed
    record_tripwire([{"record_id": f"tinydb:m{i}:c1", "heldout_attributed_subset_pass_rate": 0.5} for i in range(6)]
                    + [{"record_id": f"tinydb:m{i}:cand_R0", "heldout_attributed_subset_pass_rate": 0.9} for i in range(6)], sealed)
    res2 = dc.run_cycle(CFG, cycle_id="cycle-2", publish=True, **common)
    assert res2.published and res2.version == "v2" and dc.current_version(vdir) == "v2"
    assert res2.tripwire and res2.tripwire[0]["fired"]
    assert {r.row_id: r.state for r in load_repair_table(paths["repair_path"])}["E3-S1"] == "candidate"
    routed = [json.loads(line) for line in routing.read_text().splitlines()]
    assert routed and routed[0]["route"] == "suite_suspect" and routed[0]["entry_id"] == "E3-S1"
    assert load_first_pass_table(paths["first_pass_path"])  # still readable after publish


def test_f_entry_evaluation_pairs_f0_and_variant_runs() -> None:
    entry = next(e for e in default_first_pass_entries() if e.entry_id == "F2")
    recs = []
    for i in range(5):
        for variant, applied, rate in (("F0", [], "fail"), ("F2", ["F2"], "pass")):
            recs.append({"record_id": f"t:m{i}:{variant}:inc", "task_id": "t", "milestone_id": f"m{i}", "split": "train", "candidate_kind": "incumbent",
                         "f_entries_applied": applied, "actual_error_classes": {"E5": 0 if variant == "F2" else 2}, "cost": {"usd": 0.1}})
            recs.append({"record_id": f"t:m{i}:{variant}:c", "task_id": "t", "milestone_id": f"m{i}", "split": "train", "candidate_kind": "R0",
                         "f_entries_applied": applied, "committed": True, "per_case_results": {"a": rate, "b": "pass"}, "cost": {"usd": 0.1}})
    verdict, detail = dc.evaluate_f_entry(entry, recs, cfg=dc.CycleConfig(m_f=5))
    assert verdict.promote and detail["paired"] == 5 and detail["net"] == 1 and detail["target_before"] == 10 and detail["target_after"] == 0
    short, _ = dc.evaluate_f_entry(entry, recs[:8], cfg=dc.CycleConfig(m_f=5))
    assert not short.promote and any("checkpoints" in r for r in short.reasons)


def test_should_trigger_and_batch_regression() -> None:
    miles = [{"task_id": f"t{i}", "split": "train"} for i in range(6)]
    ok, _ = dc.should_trigger(miles, seen_tasks=["t0"], new_failures=0, cfg=dc.CycleConfig(batch_tasks=6))
    assert not ok
    ok, _ = dc.should_trigger(miles, seen_tasks=[], new_failures=0, cfg=dc.CycleConfig(batch_tasks=6))
    assert ok
    ok, _ = dc.should_trigger([], seen_tasks=[], new_failures=12, cfg=dc.CycleConfig(buffer_failures=12))
    assert ok
    prev = [{"committed": True, "net_fix": 2, "features": {"kind": "middle"}} for _ in range(5)]
    new = [{"committed": True, "net_fix": 0, "features": {"kind": "middle"}} for _ in range(5)]
    fired, detail = dc.batch_regressed(prev, new, m=5)
    assert fired and detail["middle"]["prev"] == 2.0


def test_pretrial_check_keeps_entries_with_no_target_history_out_of_trial() -> None:
    entry = next(e for e in default_first_pass_entries() if e.entry_id == "F1")  # foundation -> E2/E5
    miles = [{"features": {"kind": "foundation"}, "error_classes": ["E3"]} for _ in range(6)]
    hist = dc.f_entry_history(entry, miles, thresholds=dc.DEFAULT_THRESHOLDS)
    assert hist["matched"] == 6 and hist["with_target"] == 0
    ok, why = dc.trial_eligible(entry, hist, cfg=dc.CycleConfig(m_f=5))
    assert not ok and "calibration" in why
    short = dc.f_entry_history(entry, miles[:3], thresholds=dc.DEFAULT_THRESHOLDS)
    assert dc.trial_eligible(entry, short, cfg=dc.CycleConfig(m_f=5))[0]
    seen = [{"features": {"kind": "foundation"}, "error_classes": ["E5"]}] * 2 + miles[:4]
    assert dc.trial_eligible(entry, dc.f_entry_history(entry, seen, thresholds=dc.DEFAULT_THRESHOLDS), cfg=dc.CycleConfig(m_f=5))[0]
