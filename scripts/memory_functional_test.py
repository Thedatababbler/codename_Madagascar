#!/usr/bin/env python3
"""Memory spec §9 functional tests T1 (leaks) and T2 (transfer, cross-task recall, version pinning).

T1 and T2 start no implementer, repairer or author. T2 item 2 makes one judge
call per milestone. Results go to outputs/memory/functional/<test>.json.

    uv run python scripts/memory_functional_test.py t1
    uv run python scripts/memory_functional_test.py t2
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import orchestra.control  # noqa: E402,F401
import orchestra.control.fast_loop.plan_candidates  # noqa: E402,F401
from orchestra.memory import assemble as A  # noqa: E402
from orchestra.memory import runtime as R  # noqa: E402
from orchestra.memory import store as S  # noqa: E402
from orchestra.memory import validate as V  # noqa: E402

OUT = ROOT / "outputs" / "memory" / "functional"
DATASET = Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")


def cfg():
    return S.load_config()


def train_test():
    tr = cfg().get("transfer") or {}
    return list(tr.get("train_tasks") or []), list(tr.get("test_tasks") or [])


def copy_store(dest: Path) -> Path:
    src = S.memory_root(cfg())
    shutil.copytree(src, dest, symlinks=True)
    for p in dest.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)
    return dest


# --------------------------------------------------------------------------- T1


def t1() -> dict:
    from orchestra.codeprojecteval.dataset import build_agent_workspace, load_task
    from orchestra.memory.firstpass import WorkspaceIsolationError, assert_workspace_clean

    res: dict = {}
    root = S.memory_root(cfg())
    # 1. workspace isolation on three training tasks
    iso = {}
    with tempfile.TemporaryDirectory() as d:
        for t in ["cookiecutter", "imapclient", "bplustree"]:
            ws = build_agent_workspace(load_task(t, dataset_root=DATASET), Path(d) / t)
            files = [str(p.relative_to(ws)) for p in ws.rglob("*") if p.is_file() and ".git" not in p.parts]
            mem_files = [f for f in files if f.startswith("memory/") and f.split("/")[1] in S.BANKS]
            try:
                assert_workspace_clean(ws, root)
                clean = True
            except WorkspaceIsolationError as exc:
                clean = str(exc)
            iso[t] = {"files": len(files), "memory_files": mem_files, "assert_workspace_clean": clean}
        # a planted copy must be caught
        planted = Path(d) / "planted"
        planted.mkdir()
        shutil.copy2(root / "first_pass" / "pitfalls.yaml", planted / "notes.yaml")
        try:
            assert_workspace_clean(planted, root)
            iso["planted_copy_caught"] = False
        except WorkspaceIsolationError:
            iso["planted_copy_caught"] = True
    res["1_workspace_isolation"] = {"tasks": iso, "pass": all(v is True or (isinstance(v, dict) and v["assert_workspace_clean"] is True and not v["memory_files"]) for v in iso.values())}

    # 2. tool refusal: repository tools, and command execution (detected from the captured turn items)
    from orchestra.backends.base import BackendExecutionContext
    from orchestra.tools import repository_tools as RT

    tool = {}
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d) / "ws"
        ws.mkdir()
        (ws / "a.py").write_text("x = 1\n")
        for rel in ["memory/first_pass/pitfalls.yaml", "./memory/repair/patterns.yaml", str(root / "first_pass" / "pitfalls.yaml"),
                    "../../" + str(root.relative_to("/")) + "/first_pass/pitfalls.yaml"]:
            try:
                RT.resolve_authorized_path(ws, rel, allow_missing=True)
                tool[rel] = "ALLOWED"
            except RT.WorkspacePathError as exc:
                tool[rel] = f"refused: {exc}"
        ctx = BackendExecutionContext(run_id="r", task_id="t", subtask_id="s", node_id="n", artifact_refs=[], trace_dir=d, workspace_ref=str(ws))
        read_tool = next((f for f in vars(RT).values() if callable(f) and getattr(f, "__name__", "") == "read_workspace_file"), None)
        if read_tool is not None:
            try:
                out = read_tool(ctx, "memory/first_pass/pitfalls.yaml")
            except Exception as exc:  # noqa: BLE001
                out = f"raised {type(exc).__name__}: {exc}"
            tool["read_workspace_file(memory/first_pass/pitfalls.yaml)"] = str(out)[:200]
        # command execution: Codex runs with full_access, so a read cannot be refused up front;
        # the audit of the captured turn items catches it and stops the run
        mroot = Path(d) / "mem"
        S.write_yaml(mroot / "first_pass" / "categories.yaml", [])
        (mroot / "VERSION").write_text("0\n")
        run = R.MemoryRun.activate(Path(d) / "run", {"memory": {}}, view=S.pin(mroot))
        trace = Path(d) / "trace"
        trace.mkdir()
        (trace / "q.prompt.txt").write_text("hello")
        (trace / "q.items.json").write_text(json.dumps([{"type": "commandExecution", "command": f"cat {root}/first_pass/pitfalls.yaml"}]))
        try:
            R.after_result(run, {"tags": [], "bank": "first_pass", "expected": None}, node_id="n", contract_id="c", task_id="t",
                           role="x", prompt="hello", final_output="", status="success", trace_dir=str(trace), request_id="q", seeded=False)
            tool["command_execution"] = "NOT DETECTED"
        except R.MemoryDeliveryError as exc:
            tool["command_execution"] = f"detected after the call, run stopped: {exc}"
        R.MemoryRun.deactivate()
    res["2_tool_refusal"] = {"results": tool, "pass": all(not str(v).startswith(("ALLOWED", "NOT")) for v in tool.values()),
                             "note": "repository tools refuse up front; Codex command execution (full_access sandbox) is detected from the captured turn items and stops the run"}

    # 3. memory content: all transfer outputs (written and rejected) through the four checks
    train, test = train_test()
    ids = V.build_identifiers(train)
    view = S.pin(root)
    cats = [c["category_id"] for c in view.entries("first_pass", "categories")]
    sources = V.load_sources(root)
    written = [p for p in view.entries("first_pass", "pitfalls") if p.get("state") != "retired"]
    rejected = S.read_yaml_list(root / "transfer" / "rejected.yaml")
    pending = S.read_yaml_list(root / "transfer" / "pending_pitfalls.yaml")
    cand = [("written", p) for p in written]
    migrated = [r["entry"] for r in rejected if r.get("stage") == "migration" and r.get("bank") == "repair"]
    mig_verdicts = V.validate_entries("repair", "patterns", migrated, category_ids=[c["category_id"] for c in view.entries("repair", "categories")],
                                      domain_tags=view.domain_tags(), identifiers=ids, sources=sources, test_tasks=test)
    rejected = [r for r in rejected if r.get("stage") != "migration"]
    for i, r in enumerate(rejected):
        g = r.get("generated") or r.get("entry") or {}
        if isinstance(g, dict) and (g.get("lesson") or g.get("instruction") or (g.get("action") or {}).get("instruction")):
            e = {**g, "pitfall_id": g.get("pitfall_id") or f"rej-{i}", "category_id": g.get("category_id") or (cats[0] if cats else "x"),
                 "state": g.get("state") or "trial", "evidence": g.get("evidence") or {"source_records": [r.get("record_id")] if r.get("record_id") else []}}
            if "symptom" not in e:
                e.update({"symptom": "-", "cause": "-", "lesson": e.get("lesson") or str((g.get("action") or {}).get("instruction") or "-")})
            cand.append((f"rejected:{r.get('stage')}", e))
    for i, p in enumerate(pending):
        cand.append(("pending", {**p, "pitfall_id": f"pend-{i}", "category_id": cats[0] if cats else "x"}))
    verdicts = V.validate_entries("first_pass", "pitfalls", [e for _, e in cand], category_ids=cats, domain_tags=view.domain_tags(),
                                  identifiers=ids, sources=sources, test_tasks=test)
    dist = collections.Counter()
    by_origin = collections.defaultdict(lambda: [0, 0])
    for (origin, _), v in [*zip(cand, verdicts, strict=True), *((("rejected:migration", None), v) for v in mig_verdicts)]:
        by_origin[origin][0 if v.ok else 1] += 1
        for r in v.reasons:
            dist[r.split(":")[0]] += 1
    res["3_memory_content"] = {"checked": len(cand) + len(mig_verdicts), "passed_checks": sum(v.ok for v in [*verdicts, *mig_verdicts]),
                               "failed_checks": sum(not v.ok for v in [*verdicts, *mig_verdicts]),
                               "by_origin [pass, reject]": dict(by_origin), "reason_distribution": dict(dist),
                               "written_all_pass": all(v.ok for (o, _), v in zip(cand, verdicts, strict=True) if o == "written"),
                               "pass": all(v.ok for (o, _), v in zip(cand, verdicts, strict=True) if o == "written")}

    # 4. isolation between banks, on a copy with one trial author rule so the author prompt is not empty
    with tempfile.TemporaryDirectory() as d:
        mroot = copy_store(Path(d) / "memory")
        rules = S.read_yaml_list(mroot / "author" / "rules.yaml")
        rules.append({"rule_id": "AU-R-900", "category_id": "AU-MAIN_PATH", "trigger": "a case exercises the main path",
                      "rule": "assert on the documented return value", "state": "trial", "evidence": {}})
        S.write_yaml(mroot / "author" / "rules.yaml", rules)
        (mroot / "VERSION").write_text("99\n")
        view = S.pin(mroot)
        pits = [p for p in view.entries("first_pass", "pitfalls") if p.get("state") == "trial"][:3]
        fp_prompt = A.first_pass_block(pits, [])
        from orchestra.control.fast_loop.playbook_v2 import RowFacts, to_playbook

        run = R.MemoryRun.activate(Path(d) / "run", {"memory": {}}, view=view)
        from orchestra.control.fast_loop.playbook_v2 import load_repair_table

        rows = [r for r in load_repair_table() if r.action in ("S", "B")]
        pb = to_playbook(rows[0], facts=RowFacts(template_id="test_first"))
        rp_prompt = pb.extra_prompt or ""
        from orchestra.memory.recall import recall

        au_prompt = A.rules_block(recall(view.entries("author", "rules"), categories=["AU-MAIN_PATH"], features={}, domain_tags=[], limit=5, id_field="rule_id"))
        R.MemoryRun.deactivate()
        banks = {}
        for name, text, bank in [("first_pass", fp_prompt, "first_pass"), ("repair", rp_prompt, "repair"), ("author", au_prompt, "author")]:
            tags = A.parse_tags(text)
            foreign = [t for t in tags if run.bank_of(t) != bank]
            other_ids = [eid for b, names in S.BANKS.items() if b != bank for n in names if n != "categories"
                         for e in view.entries(b, n) for eid in [str(e.get(S.ID_FIELD[n]))] if eid and eid in text]
            banks[name] = {"tags": tags, "foreign_tags": foreign, "other_bank_ids_in_text": other_ids, "ok": bool(tags) and not foreign and not other_ids}
    res["4_bank_isolation"] = {"prompts": banks, "pass": all(b["ok"] for b in banks.values())}
    res["pass"] = all(v.get("pass") for k, v in res.items() if isinstance(v, dict))
    return res


# --------------------------------------------------------------------------- T2


T2_MILESTONES = [
    *[("python-hl7", m) for m in ("shared_contracts_utilities", "hierarchical_containers", "parsing_batch_file_support", "mllp_transport", "public_api_integration")],
    *[("imapclient", m) for m in ("shared_contracts_and_utilities", "response_lexing_and_parsing", "transport_session_and_authentication",
                                  "mailbox_message_and_extension_operations", "public_api_config_and_full_integration")],
    ("cookiecutter", "template_source_resolution"), ("cookiecutter", "project_generation_engine"),
    ("bplustree", "node_hierarchy"), ("zxcvbn", None),
]


def t2() -> dict:
    from orchestra.codeprojecteval.dataset import load_task
    from orchestra.memory.firstpass import apply_first_pass_memory, full_docs
    from orchestra.realbench.milestone_planner import parse_plan_payload
    from orchestra.settings import load_env_file

    load_env_file(ROOT / ".env")
    res: dict = {}
    stats = sorted((ROOT / "outputs" / "memory").glob("transfer_*/stats.json"))
    res["1_transfer"] = json.loads(stats[-1].read_text()) if stats else None
    res["1_transfer"]["pass"] = bool(stats)
    res["1_transfer"]["pending_new_categories"] = [p.get("proposed_category") for p in S.read_yaml_list(S.memory_root(cfg()) / "transfer" / "pending_pitfalls.yaml")]

    # 2. cross-task recall: judge + recall only
    rows = []
    with tempfile.TemporaryDirectory() as d:
        per_task: dict[str, list] = {}
        for task, mid in T2_MILESTONES:
            per_task.setdefault(task, []).append(mid)
        for task, mids in per_task.items():
            plan = parse_plan_payload(json.loads((ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{task}.plan.json").read_text()),
                                      max_agents=6, max_milestones=12)
            if mids == [None]:
                ms = [m for i, m in enumerate(plan.milestones) if 0 < i < len(plan.milestones) - 1][:1]
            else:
                ms = [m for m in plan.milestones if m.milestone_id in mids]
            run = R.MemoryRun.activate(Path(d) / task, cfg(), view=S.pin(S.memory_root(cfg())), task_id=task)
            docs = full_docs(load_task(task, dataset_root=DATASET))
            out = apply_first_pass_memory(plan, docs=docs, task_id=task, run=run, only={m.milestone_id for m in ms})
            delivery = json.loads((Path(d) / task / R.DELIVERY_FILE).read_text())
            for m in ms:
                rec = delivery["first_pass"][m.milestone_id]
                rows.append({"task": task, "milestone": m.milestone_id, "position": rec["features"]["position_group"],
                             "domain_tags": rec["domain_tags"], "selected": rec["selected"], "recalled": rec["recalled"],
                             "cross_task_entries": rec["cross_task_entries"], "skill_version": rec["skill_version"]})
            R.MemoryRun.deactivate()
            del out
    n_cross = sum(1 for r in rows if r["cross_task_entries"])
    res["2_cross_task_recall"] = {"milestones": rows, "tasks": len({r["task"] for r in rows}), "n_milestones": len(rows),
                                  "milestones_with_cross_task_entries": n_cross, "pass": n_cross >= 2 and len(rows) >= 6 and len({r["task"] for r in rows}) >= 3}

    # 3. version pinning, on a copy of the store: task A pins, a write lands, task B pins
    with tempfile.TemporaryDirectory() as d:
        mroot = copy_store(Path(d) / "memory")
        a = R.MemoryRun.activate(Path(d) / "taskA", {"memory": {}}, view=S.pin(mroot), task_id="A")
        va = a.view().version
        before = [p["pitfall_id"] for p in a.view().entries("first_pass", "pitfalls")]
        pits = S.read_yaml_list(mroot / "first_pass" / "pitfalls.yaml")
        new = {**[p for p in pits if p.get("state") == "trial"][0], "pitfall_id": "P-9999", "lesson": "a new lesson written between tasks"}
        vnew = S.commit_write(mroot, "first_pass", "pitfalls", pits + [new], reason="T2.3", origin="test", changed_ids=["P-9999"])
        a_again = R.MemoryRun.current()                               # task A, still running, re-reads its pinned view
        after = [p["pitfall_id"] for p in a_again.view().entries("first_pass", "pitfalls")]
        R.MemoryRun.deactivate()
        b = R.MemoryRun.activate(Path(d) / "taskB", {"memory": {}}, view=S.pin(mroot), task_id="B")
        vb = b.view().version
        b_ids = [p["pitfall_id"] for p in b.view().entries("first_pass", "pitfalls")]
        R.MemoryRun.deactivate()
        res["3_version_pinning"] = {"task_A_version": va, "written_version": vnew, "task_B_version": vb,
                                    "task_A_sees_new_entry": "P-9999" in after, "task_B_sees_new_entry": "P-9999" in b_ids,
                                    "task_A_unchanged": before == after,
                                    "pass": va < vnew == vb and before == after and "P-9999" in b_ids and "P-9999" not in after}
    res["pass"] = all(v.get("pass") for k, v in res.items() if isinstance(v, dict) and "pass" in v)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("test", choices=["t1", "t2"])
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    res = t1() if a.test == "t1" else t2()
    (OUT / f"{a.test}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({k: (v.get("pass") if isinstance(v, dict) else v) for k, v in res.items()}, indent=1))
    return 0 if res.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
