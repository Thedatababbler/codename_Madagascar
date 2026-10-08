#!/usr/bin/env python3
"""The joint experiment's real iteration of first-pass prevention (user's spec of 2026-10-08).

Each round: the evolver proposes 1-2 first-pass designs; each is paired against the current best
design on the evolution checkpoints (2 first runs each; cookiecutter's two first, stopped when stable
regressions outnumber stable fixes); held-out (relevant cases, read by the sealed scorer) and the gate
decide; an accepted design becomes the current best; all results go back to the evolver. At least 3
rounds or until the budget; the final best is then confirmed against the initial design on the
acceptance checkpoints. Nothing pauses between rounds except the stop conditions of the spec (§6).

    uv run python scripts/joint_iterate.py run [--max-rounds 6]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "sealed"))

import joint_experiment as J  # noqa: E402
from joint_score import score_first_run  # noqa: E402

J.RUNS.update({
    "cookiecutter": "outputs/cpe_evolution/cpe-20261001T021833Z-cookiecutter/cookiecutter",
    "imapclient": "outputs/cpe_evolution/cpe-20261001T055006Z-imapclient/imapclient",
})
EVO = [("cookiecutter", "foundation_config_environment_contracts"), ("cookiecutter", "template_source_resolution"),
       ("imapclient", "shared_contracts_and_utilities")]
SEQ_FIRST = 2   # cookiecutter's two checkpoints decide whether the third is run
ACC = [("voluptuous", "errors_markers_contracts"), ("voluptuous", "schema_compiler_core"),
       ("python-hl7", "hierarchical_containers"), ("flask", "core_contracts_and_context_primitives")]
REPS = 2
TOTAL_CAP = 20.0
COST_FACTOR = 1.3
IT = J.OUT / "iter"
TABLE = J.NS / "iter" / "first_pass.yaml"
STATE = IT / "state.json"
SEALED = IT / "sealed_heldout.json"    # per-run held-out results; the evolver never reads this file
TRAIN = ["bplustree", "cookiecutter", "csvs-to-sqlite", "deprecated", "djangorestframework-simplejwt", "flask",
         "imapclient", "python-hl7", "rsa", "tinydb", "voluptuous", "zxcvbn"]


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}", flush=True)


# --- state ------------------------------------------------------------------------------------------


def load_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text())
    return {"best": "F0", "designs": {"F0": {"entry_id": "F0", "origin": "initial: the planner's own choice, no prevention",
                                             "actions": [], "status": "best"}},
            "runs": {}, "rounds": [], "invalid_rounds": 0}


def save_state(st: dict) -> None:
    IT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def sealed() -> dict:
    return json.loads(SEALED.read_text()) if SEALED.is_file() else {}


def write_table(st: dict) -> None:
    base = yaml.safe_load((J.NS / "first_pass.yaml").read_text())
    entries = [e for e in base["entries"] if e["entry_id"] == "F0"]
    for did, d in st["designs"].items():
        if did == "F0":
            continue
        entries.append({"entry_id": did, "triggers": d.get("triggers") or [], "predicted_error_classes": d.get("predicted_error_classes") or [],
                        "actions": d.get("actions") or [], "source_rows": [], "state": "trial", "any_trigger": True,
                        "combination_of": [], "origin": json.dumps(d.get("source") or {}), "intent": d.get("intent", "")})
    TABLE.parent.mkdir(parents=True, exist_ok=True)
    TABLE.write_text(yaml.safe_dump({"version": f"iter-r{len(st['rounds'])}", "entries": entries}, sort_keys=False, allow_unicode=True))


def fingerprint() -> str:
    """Code and configuration on the first-run path (the designs themselves are keyed separately)."""
    tree = subprocess.run(["git", "ls-tree", "-r", "HEAD", "src/orchestra", "configs/roles", "configs/subgraph_templates",
                           "configs/contracts", J.CONFIG], cwd=ROOT, capture_output=True, text=True).stdout
    dirty = subprocess.run(["git", "diff", "HEAD", "--", "src/orchestra", "configs/roles", "configs/subgraph_templates"],
                           cwd=ROOT, capture_output=True, text=True).stdout
    return hashlib.sha1((tree + dirty).encode()).hexdigest()[:16]


# --- runs -----------------------------------------------------------------------------------------


def job_for(design: str, task: str, mid: str, rep: int, tag: str) -> J.Job:
    jid = f"it-{tag}-{design}-{task}-{mid}-{rep}"
    env = {"ADAMAS_EVOLUTION_ROOT": str(IT / "evolution"), "ADAMAS_FIRST_PASS_TABLE": str(TABLE),
           "ADAMAS_FP_INVENTORY_DIR": str(J.OUT / "inventory"), "ADAMAS_FIRST_RUN_ONLY": "1",
           "ADAMAS_FORCE_F": f"{mid}={design}"}
    cmd = J.base_cmd(task, mid, jid)
    cmd[cmd.index("--output-root") + 1] = str(IT / "runs")
    return J.Job(jid, "first_run", task, mid, design, rep, env, cmd)


def verifier_for(task: str, mid: str) -> Path | None:
    from joint_eval import verifier_suite
    return verifier_suite(task, mid)


def run_jobs(jobs: list[J.Job], st: dict, guard: J.BudgetGuard) -> str:
    def save():
        pass
    reason = J.execute(jobs, guard=guard, concurrency=2, save=save)
    fp = fingerprint()
    sh = sealed()
    for j in jobs:
        key = f"{j.variant}|{j.task}|{j.milestone}|{j.rep}"
        rec = {"job_id": j.job_id, "status": j.status, "fingerprint": fp}
        run_dir = IT / "runs" / j.job_id / j.task
        if j.status == "done":
            ws = sorted(run_dir.glob(f"tasks/rb_*/workspaces/{j.milestone}/repo"))
            suite = run_dir / "harness" / f"{j.milestone}.spec_tests"
            if ws and suite.is_dir():
                from orchestra.control.evolution.bank import suite_version_of
                sc = score_first_run(j.task, j.milestone, ws[0], suite, verifier_for(j.task, j.milestone))
                sh[key] = sc.pop("heldout")
                rec.update(sc)
                rec["suite_same"] = suite_version_of(suite) == suite_version_of(ROOT / J.RUNS[j.task] / "harness" / f"{j.milestone}.spec_tests")
                rec["tokens"] = J_tokens(run_dir, j.milestone)
                rec["trace_hits"] = J_trace(run_dir)
                rec["one_pass"] = bool(rec["gate"]) and all(v == "pass" for v in rec["gate"].values())
            else:
                rec["status"] = "no_workspace"
        st["runs"][key] = rec
    SEALED.write_text(json.dumps(sh))
    save_state(st)
    unfair = [j.job_id for j in jobs if st["runs"].get(f"{j.variant}|{j.task}|{j.milestone}|{j.rep}", {}).get("suite_same") is False
              or (st["runs"].get(f"{j.variant}|{j.task}|{j.milestone}|{j.rep}", {}).get("trace_hits") or 0) > 0]
    if unfair and not reason:
        reason = f"fairness: gate suite differs or a trace touched verification / held-out material in {unfair}"
    return reason


def J_tokens(run_dir: Path, mid: str) -> int:
    s = run_dir / "summary.json"
    if s.is_file():
        for o in json.loads(s.read_text()).get("milestone_objectives") or []:
            if o.get("milestone_id") == mid:
                return int(o.get("prompt_tokens") or 0)
    return 0


def J_trace(run_dir: Path) -> int:
    from joint_eval import trace_hits
    return trace_hits(run_dir)


def have_runs(st: dict, design: str, ckpts, fp: str) -> bool:
    return all(st["runs"].get(f"{design}|{t}|{m}|{r}", {}).get("status") == "done"
               and st["runs"][f"{design}|{t}|{m}|{r}"].get("fingerprint") == fp for t, m in ckpts for r in range(REPS))


def ensure_runs(st: dict, design: str, ckpts, guard, tag: str) -> str:
    fp = fingerprint()
    jobs = [job_for(design, t, m, r, tag) for t, m in ckpts for r in range(REPS)
            if not (st["runs"].get(f"{design}|{t}|{m}|{r}", {}).get("status") == "done"
                    and st["runs"][f"{design}|{t}|{m}|{r}"].get("fingerprint") == fp)]
    if not jobs:
        return ""
    log(f"running {len(jobs)} first run(s) of {design} on {sorted({(j.task, j.milestone) for j in jobs})}")
    return run_jobs(jobs, st, guard)


# --- judgement ------------------------------------------------------------------------------------


def stable(st: dict, base: str, new: str, task: str, mid: str) -> dict:
    sh = sealed()
    b = [sh.get(f"{base}|{task}|{mid}|{r}") or {} for r in range(REPS)]
    n = [sh.get(f"{new}|{task}|{mid}|{r}") or {} for r in range(REPS)]
    cases = set().union(*[set(x) for x in b + n])
    fix = sum(1 for c in cases if all(x.get(c) == "fail" for x in b) and all(x.get(c) == "pass" for x in n))
    reg = sum(1 for c in cases if all(x.get(c) == "pass" for x in b) and all(x.get(c) == "fail" for x in n))
    one = lambda d: sum(1 for r in range(REPS) if st["runs"].get(f"{d}|{task}|{mid}|{r}", {}).get("one_pass"))  # noqa: E731
    tok = lambda d: sum(st["runs"].get(f"{d}|{task}|{mid}|{r}", {}).get("tokens") or 0 for r in range(REPS))  # noqa: E731
    return {"task": task, "milestone": mid, "relevant_cases": len(cases), "stable_fix": fix, "stable_reg": reg,
            "one_pass_base": one(base), "one_pass_new": one(new), "tokens_base": tok(base), "tokens_new": tok(new)}


def verdict(per: list[dict]) -> dict:
    fix = sum(p["stable_fix"] for p in per)
    reg = sum(p["stable_reg"] for p in per)
    better = sum(1 for p in per if p["stable_fix"] - p["stable_reg"] > 0)
    worse = sum(1 for p in per if p["stable_fix"] - p["stable_reg"] < 0)
    ob, on = sum(p["one_pass_base"] for p in per), sum(p["one_pass_new"] for p in per)
    tb, tn = sum(p["tokens_base"] for p in per), sum(p["tokens_new"] for p in per)
    conds = {"stable_fix_gt_reg": fix > reg, "better_gt_worse": better > worse, "one_pass_not_lower": on >= ob,
             "cost_within_1.3x": (tn <= COST_FACTOR * tb) if tb else True}
    return {"stable_fix": fix, "stable_reg": reg, "better": better, "worse": worse, "one_pass_base": ob, "one_pass_new": on,
            "tokens_base": tb, "tokens_new": tn, "conditions": conds, "accepted": all(conds.values())}


# --- evolver context ---------------------------------------------------------------------------------


def _suite_sources(suite: Path) -> dict[str, tuple[str, list[str]]]:
    """case id (spec_tests/...) -> (function source, cited sentences)."""
    from orchestra.codeprojecteval.suite_audit import CITATION_RE
    out = {}
    for p in suite.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        src = p.read_text(errors="replace")
        lines = src.splitlines()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        owner = {id(m): c.name for c in tree.body if isinstance(c, ast.ClassDef) for m in c.body}
        rel = p.relative_to(suite).as_posix()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                start = min([fn.lineno] + [d.lineno for d in fn.decorator_list]) - 1
                j = start - 1
                while j >= 0 and lines[j].strip().startswith("#"):
                    j -= 1
                body = "\n".join(lines[j + 1: fn.end_lineno])
                cites = [m.group("quote") for m in CITATION_RE.finditer(body)]
                key = f"spec_tests/{rel}::{owner[id(fn)]}::{fn.name}" if id(fn) in owner else f"spec_tests/{rel}::{fn.name}"
                out[key] = (body, cites)
    return out


def classify(task: str, mid: str, case: str, message: str, sources: dict) -> str:
    from orchestra.control.fast_loop.error_classes import CaseFacts, classify_case
    body = sources.get(case, ("", []))[0]
    return classify_case(CaseFacts(case_id=case, key=case, name=case.split("::")[-1], source=body, output=message or ""))


def failures_by_class(st: dict, design: str, ckpts) -> dict:
    """{class: {"gate": n, "verifier": n, "sentences": [...]}} over the design's runs (both reps)."""
    out: dict = {}
    for t, m in ckpts:
        gate_src = _suite_sources(ROOT / J.RUNS[t] / "harness" / f"{m}.spec_tests")
        ver = verifier_for(t, m)
        ver_src = _suite_sources(ver) if ver else {}
        for r in range(REPS):
            rec = st["runs"].get(f"{design}|{t}|{m}|{r}") or {}
            for kind, res, msgs, src in (("gate", rec.get("gate") or {}, rec.get("gate_messages") or {}, gate_src),
                                         ("verifier", rec.get("verifier") or {}, rec.get("verifier_messages") or {}, ver_src)):
                for case, v in res.items():
                    if v != "fail":
                        continue
                    cls = classify(t, m, case, msgs.get(case, ""), src)
                    slot = out.setdefault(cls, {"gate": 0, "verifier": 0, "sentences": []})
                    slot[kind] += 1
                    for q in src.get(case, ("", []))[1][:2]:
                        if q not in slot["sentences"] and len(slot["sentences"]) < 8:
                            slot["sentences"].append(q)
    return out


def design_text(d: dict) -> str:
    keep = {k: d.get(k) for k in ("entry_id", "triggers", "predicted_error_classes", "actions", "source", "intent",
                                  "differs_from_previous", "why_now") if d.get(k) not in (None, "", [])}
    if d.get("entry_id") == "F0":
        return "F0: the planner's own subgraph, no prevention (the initial design)"
    return json.dumps(keep, indent=1)


def evolver_context(st: dict) -> dict:
    from orchestra.control.fast_loop import playbook_v2 as pb
    best = st["designs"][st["best"]]
    hist = []
    for did, d in st["designs"].items():
        if did == "F0":
            continue
        v = d.get("verdict") or {}
        hist.append(f"### {did} (round {d.get('round')}) -- {'ACCEPTED' if v.get('accepted') else 'REJECTED' if v else 'untested'}\n"
                    f"{design_text(d)}\n"
                    f"result vs {d.get('compared_with')}: held-out stable fixes {v.get('stable_fix')}, stable regressions {v.get('stable_reg')}; "
                    f"checkpoints better/worse {v.get('better')}/{v.get('worse')}; gate one-pass {v.get('one_pass_new')} vs {v.get('one_pass_base')}; "
                    f"prompt tokens {v.get('tokens_new')} vs {v.get('tokens_base')}; failed conditions: "
                    f"{[k for k, ok in (v.get('conditions') or {}).items() if not ok]}{' (stopped after the first two checkpoints)' if d.get('stopped_early') else ''}\n"
                    f"gate / verification failures of its runs by error class: "
                    f"{json.dumps({c: {'gate': x['gate'], 'verifier': x['verifier']} for c, x in failures_by_class(st, did, d.get('tested_on') or []).items()})}")
    resid = failures_by_class(st, st["best"], EVO)
    residual = "\n".join(f"- {c}: {x['gate']} gate and {x['verifier']} verification failures (both runs together); cited sentences: "
                         + "; ".join(f'"{q}"' for q in x["sentences"]) for c, x in sorted(resid.items())) or "- none"
    rows = []
    known = {"E3-T1": "tested 3 times: tied with the default repair", "pb_rtf_swap_angle": "tested 6 times (legacy)",
             "E3-S1": "joint round 1: tied with the default repair on gate, verification and held-out in 4/4 pairs, +64% tokens"}
    for r in pb.default_repair_rows():
        edits = ",".join(f"{e.slot}:{e.role}" for e in r.slot_edits)
        rows.append(f"- {r.row_id} [{','.join(r.error_classes)}] action {r.action}"
                    + (f", template {r.target_template}" if r.target_template else "") + (f", slots {edits}" if edits else "")
                    + f": {r.intent}" + (f" -- {known[r.row_id]}" if r.row_id in known else " -- untested"))
    return {"best": design_text(best), "history": "\n\n".join(hist) or "(none yet: only the initial design has been run)",
            "residual": residual, "rows": "\n".join(rows)}


def identifiers() -> frozenset[str]:
    from orchestra.codeprojecteval.public_symbols import derive_public_symbols, load_docs
    from orchestra.control.evolution.validators import training_identifiers
    mids, focus, cases = [], [], []
    words = set()
    for t in TRAIN:
        plan = json.loads((ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{t}.plan.json").read_text())
        mids += [m["milestone_id"] for m in plan["milestones"]]
        focus += [p for m in plan["milestones"] for p in m.get("focus_paths") or []]
        inv = derive_public_symbols(load_docs(Path(J.DATASET_ROOT) / t / "docs"))
        for n in inv.names():
            leaf = n.split(".")[-1]
            if ("_" in leaf and len(leaf) > 4) or (sum(ch.isupper() for ch in leaf) >= 2 and any(ch.islower() for ch in leaf)):
                words.add(leaf.lower())
        cfg = json.loads((Path(J.DATASET_ROOT) / t / "config.json").read_text())
        words.add(str(cfg.get("source_code") or t).split("/")[0].lower())
    for t, m in EVO:
        cases += list(_suite_sources(ROOT / J.RUNS[t] / "harness" / f"{m}.spec_tests"))
    return training_identifiers(task_ids=TRAIN, milestone_ids=mids, case_names=cases, focus_paths=focus) | frozenset(words)


# --- main loop ------------------------------------------------------------------------------------


def milestones_for_shape():
    from orchestra.realbench.milestone_planner import parse_plan_payload
    out = []
    for t, m in EVO:
        d = parse_plan_payload(json.loads((ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{t}.plan.json").read_text()), max_agents=6)
        out.append(next(x for x in d.milestones if x.milestone_id == m))
    return out


def prepare_inventories() -> None:
    from orchestra.codeprojecteval.behaviour_inventory import extract_inventory
    from orchestra.codeprojecteval.public_symbols import load_docs
    os.environ.setdefault("ADAMAS_AUTHOR_INVENTORY_CACHE", str(ROOT / "outputs" / "author_eval" / "inventory_cache"))
    for t, mid in EVO + ACC:
        if (J.OUT / "inventory" / f"{mid}.inventory.json").is_file():
            continue
        plan = json.loads((ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{t}.plan.json").read_text())
        m = next(x for x in plan["milestones"] if x["milestone_id"] == mid)
        extract_inventory(task=t, milestone_id=mid, objective=m.get("objective", ""), criteria=(m.get("acceptance") or {}).get("criteria") or [],
                          focus_paths=m.get("focus_paths") or [], docs=load_docs(Path(J.DATASET_ROOT) / t / "docs"), out_dir=J.OUT / "inventory")


def run(args) -> None:
    from orchestra.control.evolution.fp_evolver import propose, shape_checker
    IT.mkdir(parents=True, exist_ok=True)
    st = load_state()
    b = json.loads((IT / "budget.json").read_text()) if (IT / "budget.json").is_file() else {}
    now = J.read_window()
    if now is None:
        raise SystemExit("cannot read the account window; refusing to spend without the guard")
    b.setdefault("start", now)
    (IT / "budget.json").write_text(json.dumps(b))
    guard = J.BudgetGuard(J.read_window, round_start=b["start"], total_start=b["start"], round_cap=TOTAL_CAP, total_cap=TOTAL_CAP)
    used = lambda: (J.read_window() or b["start"]) - b["start"]  # noqa: E731
    prepare_inventories()
    write_table(st)
    shape = shape_checker(milestones_for_shape())
    ids = identifiers()
    # baseline: the initial design on the evolution checkpoints
    reason = ensure_runs(st, "F0", EVO, guard, "base")
    if reason:
        return stop(st, reason)
    st.setdefault("budget_log", []).append({"after": "baseline", "used": used()})
    save_state(st)
    per_run = max(0.1, used() / max(1, sum(1 for k, r in st["runs"].items() if r.get("status") == "done")))
    reserve = per_run * 2.4 * REPS * 2 * len(ACC)   # acceptance first runs are ~2.4x the evolution ones (round 1)
    log(f"baseline done; {used():.1f} points used, ~{per_run:.2f} per run, confirmation reserve ~{reserve:.1f}")
    rnd = len(st["rounds"])
    while rnd < args.max_rounds:
        rnd += 1
        remaining = TOTAL_CAP - used()
        design_cost = per_run * REPS * len(EVO)
        if remaining - reserve < design_cost * 0.67:
            log(f"round {rnd}: {remaining:.1f} points left, reserve {reserve:.1f}: stop iterating, go to confirmation")
            break
        log(f"round {rnd}: asking the evolver")
        out_dir = IT / "evolver"
        out_dir.mkdir(parents=True, exist_ok=True)
        rejected = [d for d in st["designs"].values() if (d.get("verdict") or {}).get("accepted") is False]
        valid, discarded = propose(context=evolver_context(st), out_dir=out_dir, tag=f"round{rnd}", identifiers=ids,
                                   rejected=rejected, shape_check=shape)
        rec = {"round": rnd, "proposed": len(valid) + len(discarded), "valid": [], "discarded": [
            {"intent": d.get("intent"), "reasons": d.get("_reasons")} for d in discarded], "used_before": used()}
        if not valid:
            st["invalid_rounds"] = st.get("invalid_rounds", 0) + 1
            st["rounds"].append(rec)
            save_state(st)
            if st["invalid_rounds"] >= 2:
                return stop(st, "two consecutive rounds without a valid proposal")
            continue
        st["invalid_rounds"] = 0
        affordable = 2 if remaining - reserve >= 2 * design_cost else 1
        for k, d in enumerate(valid[:affordable], 1):
            did = f"G{rnd}{k}"
            d = {**d, "entry_id": did, "round": rnd, "status": "trial", "compared_with": st["best"]}
            st["designs"][did] = d
            rec["valid"].append(did)
            write_table(st)
            save_state(st)
            best = st["best"]
            # sequential: cookiecutter's two checkpoints first
            reason = ensure_runs(st, best, EVO[:SEQ_FIRST], guard, f"r{rnd}") or ensure_runs(st, did, EVO[:SEQ_FIRST], guard, f"r{rnd}")
            if reason:
                return stop(st, reason)
            per = [stable(st, best, did, t, m) for t, m in EVO[:SEQ_FIRST]]
            if sum(p["stable_reg"] for p in per) > sum(p["stable_fix"] for p in per):
                v = verdict(per)
                v["accepted"] = False
                d.update(verdict=v, per_checkpoint=per, tested_on=EVO[:SEQ_FIRST], stopped_early=True, status="rejected")
                log(f"{did}: stopped after cookiecutter (stable regressions {v['stable_reg']} > fixes {v['stable_fix']})")
            else:
                reason = ensure_runs(st, best, EVO[SEQ_FIRST:], guard, f"r{rnd}") or ensure_runs(st, did, EVO[SEQ_FIRST:], guard, f"r{rnd}")
                if reason:
                    return stop(st, reason)
                per = [stable(st, best, did, t, m) for t, m in EVO]
                v = verdict(per)
                d.update(verdict=v, per_checkpoint=per, tested_on=EVO, status="accepted" if v["accepted"] else "rejected")
                log(f"{did}: {'ACCEPTED' if v['accepted'] else 'rejected'} {v}")
                if v["accepted"]:
                    st["designs"][best]["status"] = "superseded"
                    st["best"] = did
                    d["status"] = "best"
            st["designs"][did] = d
            save_state(st)
        rec["used_after"] = used()
        st["rounds"].append(rec)
        save_state(st)
        write_round_version(st, rnd)
    confirm(st, guard)


def write_round_version(st: dict, rnd: int) -> None:
    d = J.NS / "iter"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"first_pass.round{rnd}.yaml").write_text(TABLE.read_text())
    log_path = d / "CHANGELOG.yaml"
    entries = yaml.safe_load(log_path.read_text()) if log_path.is_file() else []
    r = st["rounds"][-1]
    entries.append({"round": rnd, "best_after": st["best"], "tested": [
        {"design": did, "intent": st["designs"][did].get("intent"), "source": st["designs"][did].get("source"),
         "differs_from_previous": st["designs"][did].get("differs_from_previous"), "why_now": st["designs"][did].get("why_now"),
         "verdict": {k: v for k, v in (st["designs"][did].get("verdict") or {}).items() if k != "conditions"},
         "failed_conditions": [k for k, ok in ((st["designs"][did].get("verdict") or {}).get("conditions") or {}).items() if not ok]}
        for did in r["valid"]], "discarded": r["discarded"], "points": round(r.get("used_after", 0) - r["used_before"], 1)})
    log_path.write_text(yaml.safe_dump(entries, sort_keys=False, allow_unicode=True))


def confirm(st: dict, guard) -> None:
    best = st["best"]
    if best == "F0":
        log("the best design is still the initial one: no confirmation to run")
        st["confirmation"] = {"skipped": "best is the initial design"}
        save_state(st)
        return
    log(f"confirmation: {best} vs F0 on the acceptance checkpoints")
    reason = ensure_runs(st, "F0", ACC, guard, "acc") or ensure_runs(st, best, ACC, guard, "acc")
    per = [stable(st, "F0", best, t, m) for t, m in ACC if all(st["runs"].get(f"{x}|{t}|{m}|{r}", {}).get("status") == "done"
                                                              for x in ("F0", best) for r in range(REPS))]
    st["confirmation"] = {"best": best, "per_checkpoint": per, "verdict": verdict(per) if per else None, "stopped": reason}
    save_state(st)
    log(f"confirmation: {st['confirmation']['verdict']}")


def stop(st: dict, reason: str) -> None:
    st["stopped"] = reason
    save_state(st)
    log(f"STOPPED: {reason}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--max-rounds", type=int, default=6)
    r.set_defaults(fn=run)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
