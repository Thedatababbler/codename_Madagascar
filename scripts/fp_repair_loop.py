#!/usr/bin/env python3
"""First-pass design from repair experience: the iteration (user's spec of 2026-10-08, §1, §4-§9).

Evolution tasks cookiecutter + imapclient (checkpoints and repair records), acceptance tasks nl2_python-jose +
nl2_tablib (confirmation checkpoints only), online task cookiecutter (full runs). The first-pass table starts
empty. Each round: repair experiences (memory replay) -> top-k clusters -> the evolver proposes one design per
cluster -> validation -> paired first runs on trigger-matched evolution checkpoints (sequential) -> confirmation
on acceptance checkpoints -> enabled designs published -> a full online run with the round's table (and one
empty-table control per code version) whose records feed the next round. No stop between rounds except §9.

    uv run python scripts/fp_repair_loop.py run [--rounds 3] [--round-cap 45]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "sealed"))

import fp_from_repair as P  # noqa: E402
import joint_experiment as J  # noqa: E402
from joint_score import score_first_run  # noqa: E402

EXP = "firstpass_from_repair_20261008"
OUT = ROOT / "outputs" / "fp_from_repair"
ONLINE_ROOT = ROOT / "outputs" / "fp_online"
NS = ROOT / "configs" / "playbook_v2" / "experiments" / EXP
TABLE = NS / "first_pass.yaml"
EMPTY_TABLE = NS / "first_pass.empty.yaml"
STATE = OUT / "state.json"
SEALED = OUT / "sealed_heldout.json"
CONFIG = "configs/experiments/codeprojecteval_milestones_evolution.yaml"
EVO_TASKS = ["cookiecutter", "imapclient"]
ACC_TASKS = ["nl2_python-jose", "nl2_tablib"]
ONLINE_TASKS = ["cookiecutter"]
K = 3
REPS = 2
MAX_CKPT = 4
COST_TOL = 0.3
MIN_ACCEPT_CKPT = 3
MIN_REL = 20
WINDOW_STOP = 97.0
SIZE_MEDIAN = 6     # experience size split (median of focus files over the experiences, preparation)


def log(msg: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


# --- tasks and checkpoints --------------------------------------------------------------------------


def task_spec(task: str) -> dict:
    return P.TASKS[task]


def is_nl2(task: str) -> bool:
    return task.startswith("nl2_")


def dataset_root(task: str) -> str:
    return str(task_spec(task)["data"])


def env_extra(task: str) -> dict:
    return {"CPE_ENV_ROOT": "/root/codex-benchmarks/nl2repo/envs"} if is_nl2(task) else {}


def checkpoints(tasks) -> list[dict]:
    out = []
    for t in tasks:
        feats = P.features(t)
        rel = json.loads((ROOT / "outputs/evolution/sealed/attribution" / f"{t}.json").read_text()).get("relevant") or {}
        for mid, f in feats.items():
            out.append({"task": t, "milestone": mid, "features": f, "relevant": len(rel.get(mid) or [])})
    return out


def predecessor(task: str, milestone: str) -> tuple[str, str]:
    run = ROOT / task_spec(task)["checkpoint_run"]
    canon = next(run.glob("tasks/*/canonical/repo"))
    plan = json.loads((ROOT / task_spec(task)["plan"]).read_text())
    order = [m["milestone_id"] for m in plan["milestones"]]
    log_ = subprocess.run(["git", "-C", str(canon), "log", "--format=%H %s"], capture_output=True, text=True, check=True).stdout
    commits, base = {}, ""
    for line in log_.splitlines():
        sha, _, subj = line.partition(" ")
        if subj.startswith("canonical commit "):
            commits[subj.removeprefix("canonical commit ").strip()] = sha
        elif "dataset inputs" in subj:
            base = sha
    i = order.index(milestone)
    return str(canon), (base if i == 0 else commits[order[i - 1]])


def first_run_job(design: str, task: str, mid: str, rep: int, tag: str) -> J.Job:
    jid = f"fr-{tag}-{design}-{task}-{mid}-{rep}"
    run = ROOT / task_spec(task)["checkpoint_run"]
    repo, rev = predecessor(task, mid)
    cmd = [str(ROOT / ".venv" / "bin" / "python"), "-m", "orchestra.cli.run_codeprojecteval_decomp",
           "--config", CONFIG, "--task-id", task, "--arm", "fp_repair", "--run-id", jid,
           "--output-root", str(OUT / "runs"), "--dataset-root", dataset_root(task),
           "--only-milestone", mid, "--base-snapshot", f"{repo}@{rev}", "--inherit-harness", str(run / "harness"),
           "--plan-file", str(ROOT / task_spec(task)["plan"])]
    env = {"ADAMAS_EVOLUTION_ROOT": str(OUT / "evolution_reruns"), "ADAMAS_FIRST_PASS_TABLE": str(TABLE),
           "ADAMAS_FP_INVENTORY_DIR": str(OUT / "inventory"), "ADAMAS_FIRST_RUN_ONLY": "1",
           "ADAMAS_FORCE_F": f"{mid}={design}", **env_extra(task)}
    return J.Job(jid, "first_run", task, mid, design, rep, env, cmd)


# --- state --------------------------------------------------------------------------------------------


def load_state() -> dict:
    if STATE.is_file():
        return json.loads(STATE.read_text())
    return {"designs": {}, "runs": {}, "rounds": [], "online": {}, "cluster_tries": {}, "stops": {"invalid": 0, "no_cluster": 0}}


def save_state(st: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, default=str))


def sealed() -> dict:
    return json.loads(SEALED.read_text()) if SEALED.is_file() else {}


def write_tables(st: dict) -> None:
    NS.mkdir(parents=True, exist_ok=True)
    f0 = {"entry_id": "F0", "triggers": [], "predicted_error_classes": [], "actions": [], "source_rows": [], "state": "active",
          "any_trigger": True, "combination_of": [], "origin": "", "intent": "the planner's own choice, unchanged"}
    EMPTY_TABLE.write_text(yaml.safe_dump({"version": "empty", "entries": [f0]}, sort_keys=False))
    entries = [f0]
    for did, d in st["designs"].items():
        state = {"enabled": "active", "retired": "retired"}.get(d.get("status"), "candidate")
        entries.append({"entry_id": did, "triggers": d.get("triggers") or [], "predicted_error_classes": d.get("predicted_error_classes") or [],
                        "actions": d.get("actions") or [], "source_rows": [], "state": state, "any_trigger": False,
                        "combination_of": [], "origin": f"cluster {d.get('cluster_key')}", "intent": d.get("intent", "")})
    TABLE.write_text(yaml.safe_dump({"version": f"round{len(st['rounds'])}", "entries": entries}, sort_keys=False, allow_unicode=True))


def fingerprint() -> str:
    paths = ["src/orchestra", "configs/roles", "configs/subgraph_templates", "configs/contracts", CONFIG]
    skip = "src/orchestra/control/evolution/"
    tree = subprocess.run(["git", "ls-tree", "-r", "HEAD", "--", *paths], cwd=ROOT, capture_output=True, text=True).stdout
    tree = "\n".join(ln for ln in tree.splitlines() if skip not in ln)
    dirty = subprocess.run(["git", "diff", "HEAD", "--", *paths, f":(exclude){skip}"], cwd=ROOT, capture_output=True, text=True).stdout
    if not tree.strip():
        raise SystemExit("fingerprint: empty tree listing")
    return hashlib.sha1((tree + dirty).encode()).hexdigest()[:16]


# --- verification suites ------------------------------------------------------------------------------------


def verifier_for(task: str, mid: str) -> Path | None:
    man = OUT / "verifiers.json"
    if man.is_file():
        p = json.loads(man.read_text()).get(f"{task}:{mid}")
        if p and Path(p).is_dir():
            return Path(p)
    for label in ("V", "A1"):
        m = ROOT / "outputs/author_eval" / label / "suites.s0.json"
        if m.is_file():
            for e in json.loads(m.read_text()):
                if e["task"] == task and e["milestone"] == mid and e.get("suite_dir"):
                    return Path(e["suite_dir"])
    return None


def ensure_verifiers(ckpts: list[dict], guard) -> None:
    need = [c for c in ckpts if verifier_for(c["task"], c["milestone"]) is None
            and (c["task"] in EVO_TASKS or c["relevant"] >= MIN_REL)]
    if not need:
        return
    log(f"generating {len(need)} verification suite(s) (v10.1 author, independent sample)")
    man_path = OUT / "verifiers.json"
    man = json.loads(man_path.read_text()) if man_path.is_file() else {}
    for group in (False, True):
        sel = [c for c in need if is_nl2(c["task"]) == group]
        if not sel:
            continue
        env = dict(os.environ)
        plans_dir = "configs/datasets/nl2repo_feature_plans" if group else "configs/datasets/cpe_feature_plans"
        if group:
            env.update(CPE_DATASET_ROOT=str(P.NL2_ROOT), CPE_ENV_ROOT="/root/codex-benchmarks/nl2repo/envs")
        cmd = [sys.executable, str(ROOT / "scripts" / "author_probe.py"), "run", "--tag", "fprV", "--plans-dir", plans_dir,
               "--config", "configs/experiments/author_probe.yaml", "--jobs", "2"]
        for c in sel:
            cmd += ["--select", f"{c['task']}:{c['milestone']}"]
        subprocess.run(cmd, cwd=ROOT, env=env, check=False)
        for c in sel:
            hits = sorted(ROOT.glob(f"outputs/author_probe/author-fprV-*-{c['task']}-{c['milestone']}*/{c['task']}/harness/{c['milestone']}.spec_tests"))
            if hits:
                man[f"{c['task']}:{c['milestone']}"] = str(hits[-1])
    man_path.write_text(json.dumps(man, indent=1))


# --- experiences --------------------------------------------------------------------------------------------


def runs_for_experience() -> dict[str, list[Path]]:
    return {t: P.runs_of(t) for t in EVO_TASKS}


def summarise(exp: dict, patch: str, strong, weak) -> str:
    from orchestra.control.evolution.evolver import default_call
    from orchestra.control.evolution.fp_evolver import leaked_identifiers, weak_leaks
    cache = OUT / "summaries.json"
    data = json.loads(cache.read_text()) if cache.is_file() else {}
    key = hashlib.sha1((patch or "").encode()).hexdigest()[:16]
    if key in data:
        return data[key]
    system = ("You describe what a code change did, in general terms, for a designer of software processes. Never name the "
              "project, package, module, file, class, function or test. Two sentences at most.")
    prompt = (f"A repair made {exp['fixed']} failing acceptance cases pass (error classes {exp['fixed_classes']}); it changed "
              f"{exp['diff']['files']} file(s), +{exp['diff']['added']} / -{exp['diff']['removed']} lines. The diff (truncated):\n\n"
              + (patch or "")[:12000] + "\n\nWhat kind of behaviour did it add or correct, and what kind of code did it touch?")
    text = default_call(system, prompt)
    if leaked_identifiers(text, strong) or weak_leaks(text, weak):
        text = re.sub(r"`[^`]+`", "<name>", text)
        for w in set(leaked_identifiers(text, strong)):
            text = re.sub(rf"\b{re.escape(w)}\b", "<name>", text, flags=re.I)
    data[key] = text
    cache.write_text(json.dumps(data, indent=1))
    return text


def verifier_fixes(task: str, mid: str, inc_ws: Path, cand_ws: Path) -> dict | None:
    """Stable fixes / regressions of a repair on the verification suite (valid cases), memory replay."""
    ver = verifier_for(task, mid)
    if ver is None or not inc_ws.is_dir() or not cand_ws.is_dir():
        return None
    from heldout_matrix import reference_failures
    from author_eval import run_suite
    from _guard import task_python
    invalid = set(reference_failures(task, ver))
    py = task_python(task)
    a = {k: v for k, v in run_suite(inc_ws, ver, py).items() if k not in invalid}
    b = {k: v for k, v in run_suite(cand_ws, ver, py).items() if k not in invalid}
    return {"fixed": sum(1 for k, v in a.items() if v == "fail" and b.get(k) == "pass"),
            "regressed": sum(1 for k, v in a.items() if v == "pass" and b.get(k) == "fail")}


def build_experiences(strong, weak) -> list[dict]:
    import glob as _glob
    cache_path = OUT / "experience_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
    out = []
    for task in EVO_TASKS:
        for e in P.experiences(task):
            key = f"{e['run']}|{task}|{e['milestone']}|{e['repair']}"
            if key not in cache:
                run = next(r for r in P.runs_of(task) if r.parent.name == e["run"])
                state = json.loads(Path(_glob.glob(str(run / "tasks/*/task_execution.json"))[0]).read_text())
                v = state["fast_loop_states"][e["milestone"]]
                cand = next(c for c in v["candidates"] if c.get("status") == "committed" and P.kind_of(c["candidate_id"], c.get("metadata") or {}))
                inc_ws = next(iter(sorted(run.glob(f"tasks/*/workspaces/{e['milestone']}/repo"))), Path("/nonexistent"))
                cand_ws = next(iter(sorted(run.glob(f"tasks/*/subtasks/{e['milestone']}/candidates/{cand['candidate_id']}/repo"))), Path("/nonexistent"))
                vf = verifier_fixes(task, e["milestone"], inc_ws, cand_ws)
                summary = summarise(e, cand.get("patch") or "", strong, weak)
                cache[key] = {"verifier": vf, "summary": summary}
                cache_path.write_text(json.dumps(cache, indent=1))
            e.update(cache[key])
            # §4.1: stable fixes on the gate and the verification suite together
            e["stable_fix"] = e["fixed"] + ((e.get("verifier") or {}).get("fixed") or 0)
            e["stable_reg"] = e["regressed"] + ((e.get("verifier") or {}).get("regressed") or 0)
            out.append(e)
    return out


def cluster_experiences(exps: list[dict], st: dict) -> list[dict]:
    from collections import defaultdict
    groups = defaultdict(list)
    for e in exps:
        size = "small" if e["features"]["n_focus_files"] <= SIZE_MEDIAN else "large"
        total_fixed = max(1, e["fixed"])
        for cls, n in e["fixed_classes"].items():
            share = n / total_fixed
            groups[(e["features"]["position_group"], size, cls)].append((e, e["stable_fix"] * share, e["stable_reg"] * share))
    enabled = {d.get("cluster_key") for d in st["designs"].values() if d.get("status") == "enabled"}
    out = []
    for key, items in groups.items():
        ms = {(e["task"], e["milestone"]) for e, _, _ in items}
        k = "/".join(key)
        mean_fix = statistics.fmean(f for _, f, _ in items)
        reg = sum(r for _, _, r in items)
        tries = st["cluster_tries"].get(k, 0)
        c = {"key": k, "position_group": key[0], "size_group": key[1], "error_class": key[2], "milestones": sorted(f"{t}:{m}" for t, m in ms),
             "n_milestones": len(ms), "experiences": len(items), "mean_stable_fixes": round(mean_fix, 2), "regressions": round(reg, 2),
             "usefulness": round(len(ms) * mean_fix - reg, 2), "repairs": {}, "summaries": [], "sentences": [],
             "cost_tokens": int(statistics.fmean((e["cost"].get("prompt_tokens") or 0) for e, _, _ in items))}
        for e, _, _ in items:
            c["repairs"][e["repair"]] = c["repairs"].get(e["repair"], 0) + 1
            if e.get("summary") and e["summary"] not in c["summaries"] and len(c["summaries"]) < 6:
                c["summaries"].append(e["summary"])
            for q in e["cited_sentences"]:
                if q not in c["sentences"] and len(c["sentences"]) < 10:
                    c["sentences"].append(q)
        c["excluded"] = ("one milestone" if len(ms) < 2 else "an enabled design covers it" if k in enabled
                         else "rejected twice" if tries >= 2 else "")
        out.append(c)
    out.sort(key=lambda c: -c["usefulness"])
    return out


# --- evolver ----------------------------------------------------------------------------------------------------

SYSTEM = """You design first-pass prevention for a multi-agent coding system, from repair experience. A milestone's first run is
the planner's subgraph (agents writing code from design documents) followed by one acceptance gate; failures are then
repaired. You are shown the kinds of problems repairs kept fixing; design changes to the first run that would avoid them.
Your designs must be task-agnostic: never name a project, package, module, file, class, function or test. Output JSON only."""

PROMPT = """## What a first-pass design can change
- {{"kind": "instruction", "text": "<appended to the writer's mandate, <= 2500 chars>"}}
- {{"kind": "inventory", "kinds": [<subset of main_path, boundary, state_transition, error_path, integration, protocol>]}}  -- the milestone's documented behaviours of these kinds, quoted from the documents, given to the writer
- {{"kind": "add_reviewer", "role": "<one of {readonly}>"}}  -- a read-only reviewer after the writer; its findings go to a repairer
- {{"kind": "template", "template_id": "<one of {templates}>"}}
- {{"kind": "budget", "steps": <extra agent steps>, "seconds": <extra seconds>}}
Structure: one writer at a time; the shape must end with a writer or accept an appended repairer.

## Triggers
Triggers are all required to hold, and they must hold on every milestone of the cluster you target (their feature values
are listed with each cluster). Features: dep_depth (0 = first milestone, 1 = second, ...), kind ("integration" for the last
milestone; any other milestone is labelled "foundation" or "middle" from keywords in its text, so do not use kind to mean the
middle position), n_focus_files, n_public_symbols, n_documented_exceptions, n_state_transitions, n_public_classes.
Express position with dep_depth and kind: first = dep_depth == 0; middle = dep_depth >= 1 and kind != "integration";
last = kind == "integration". Size: small = n_focus_files <= {median}; large = n_focus_files > {median}.

## The current first-pass table
{table}

## Repair experience: the {k} most useful clusters this round
{clusters}

## Designs tested so far
{history}

## Your task
For each cluster above propose at most one first-pass design that would prevent, in the first run, the kind of problem the
repairs kept fixing. Its triggers must match the cluster's position and size group. If you modify a tested design, say what you
changed and which result motivates it. Never resubmit a design rejected twice. Return a JSON array:
[{{"cluster": <cluster number>, "triggers": [{{"feature": "dep_depth", "op": ">=", "value": 1}}, ...],
   "predicted_error_classes": ["E3"], "actions": [...],
   "intent": "<one sentence>", "correspondence": "<which repeatedly repaired problem it avoids and how>",
   "modifies": "<tested design id or none>", "change_and_reason": "<what changed and why, or none>"}}]
"""


def cluster_text(cs: list[dict]) -> str:
    feats = {f"{c['task']}:{c['milestone']}": c["features"] for c in checkpoints(EVO_TASKS)}
    keys = ("dep_depth", "kind", "n_focus_files", "n_public_symbols", "n_documented_exceptions", "n_state_transitions", "n_public_classes")
    out = []
    for i, c in enumerate(cs, 1):
        rows = [json.dumps({k: feats[m][k] for k in keys}) for m in c["milestones"] if m in feats]
        out.append(f"### Cluster {i}: {c['position_group']} milestones, {c['size_group']} scope, error class {c['error_class']}\n"
                   f"seen on {c['n_milestones']} milestones ({c['experiences']} repairs); mean stable fixes per repair {c['mean_stable_fixes']}; "
                   f"regressions {c['regressions']}; mean repair cost {c['cost_tokens']} prompt tokens\n"
                   "feature values of its milestones (your triggers must hold on all of them):\n" + "\n".join(f"- {r}" for r in rows) + "\n"
                   f"repair actions: {json.dumps(c['repairs'])}\n"
                   "what the repairs did:\n" + "\n".join(f"- {s}" for s in c["summaries"]) + "\n"
                   "sentences the fixed cases cite:\n" + "\n".join(f'- "{q}"' for q in c["sentences"]))
    return "\n\n".join(out)


def history_text(st: dict) -> str:
    rows = []
    for did, d in st["designs"].items():
        v = d.get("verdict") or {}
        c = d.get("confirmation") or {}
        rows.append(f"### {did} (round {d.get('round')}, cluster {d.get('cluster_key')}) -- {d.get('status')}\n"
                    + json.dumps({k: d.get(k) for k in ("triggers", "actions", "intent")}) + "\n"
                    f"evolution checkpoints: gate+verification stable fixes {v.get('fix')}, regressions {v.get('reg')}, better/worse "
                    f"{v.get('better')}/{v.get('worse')}, targeted class first-run failures {v.get('target_base')} -> {v.get('target_new')}, "
                    f"tokens {v.get('tokens_new')} vs {v.get('tokens_base')}, held-out safety condition met: {v.get('heldout_safe')}; "
                    f"failed: {[k for k, ok in (v.get('conditions') or {}).items() if not ok]}"
                    + (f"\nacceptance checkpoints: stable fixes {c.get('fix')}, regressions {c.get('reg')}, held-out safety met: {c.get('heldout_safe')}, "
                       f"failed: {[k for k, ok in (c.get('conditions') or {}).items() if not ok]}" if c else ""))
    return "\n\n".join(rows) or "(none yet)"


def table_text(st: dict) -> str:
    on = [d for d in st["designs"].values() if d.get("status") == "enabled"]
    if not on:
        return "empty: every milestone runs the planner's own subgraph and the original prompts"
    return "\n".join(json.dumps({k: d.get(k) for k in ("entry_id", "triggers", "actions", "intent")}) for d in on)


def holds(triggers: list, features: dict) -> bool:
    from orchestra.control.first_pass.designer import Trigger
    for t in triggers or []:
        tr = Trigger(str(t.get("feature")), str(t.get("op")), t.get("value"))
        if not tr.holds(features, {}):
            return False
    return True


def propose(st: dict, cs: list[dict], rnd: int, strong, weak, shape) -> tuple[list[dict], list[dict]]:
    from orchestra.control.evolution.evolver import default_call
    from orchestra.control.evolution.fp_evolver import parse, validate
    from orchestra.control.evolution.validators import READ_ONLY_ROLES, available_roles, available_templates
    roles, templates = available_roles(), available_templates()
    prompt = PROMPT.format(readonly=[r for r in READ_ONLY_ROLES if r in roles], templates=sorted(templates), median=SIZE_MEDIAN,
                           table=table_text(st), k=len(cs), clusters=cluster_text(cs), history=history_text(st))
    d = OUT / "evolver"
    d.mkdir(parents=True, exist_ok=True)
    feats = {f"{c['task']}:{c['milestone']}": c["features"] for c in checkpoints(EVO_TASKS)}
    rejected2 = [x for x in st["designs"].values() if x.get("status") == "retired"]
    valid, discarded, feedback = [], [], ""
    for attempt in range(2):
        text = prompt + (f"\n\n## Your previous answer was rejected\n{feedback}\nFix every point." if feedback else "")
        (d / f"round{rnd}.a{attempt}.prompt.txt").write_text(SYSTEM + "\n\n" + text)
        reply = default_call(SYSTEM, text)
        (d / f"round{rnd}.a{attempt}.reply.txt").write_text(reply)
        designs = parse(reply)[: len(cs)]
        valid, bad = [], []
        for x in designs:
            reasons = validate(x, identifiers=strong, weak=weak, roles=roles, templates=templates, rejected=rejected2, shape_check=shape)
            try:
                ci = int(x.get("cluster")) - 1
                cl = cs[ci]
            except (TypeError, ValueError, IndexError):
                reasons.append("cluster number missing or unknown")
                cl = None
            if cl is not None:
                misses = [m for m in cl["milestones"] if not holds(x.get("triggers") or [], feats[m])]
                if misses:
                    reasons.append(f"triggers do not hold on {len(misses)} of the cluster's milestones: they must match the cluster's position and size group")
                x["cluster_key"] = cl["key"]
            (bad if reasons else valid).append({**x, "_reasons": reasons} if reasons else x)
        discarded += [{**b, "attempt": attempt} for b in bad]
        if valid:
            break
        feedback = "\n".join(f"- {', '.join(b['_reasons'])}" for b in bad) or "- no JSON array was found"
    seen, uniq = set(), []
    for v in valid:
        if v["cluster_key"] not in seen:
            seen.add(v["cluster_key"])
            uniq.append(v)
    return uniq, discarded


# --- runs and judgement -------------------------------------------------------------------------------------------


def current_design(st: dict, ck: dict) -> str:
    on = [d for d in st["designs"].values() if d.get("status") == "enabled" and holds(d.get("triggers"), ck["features"])]
    return on[0]["entry_id"] if on else "F0"


def ensure_runs(st: dict, design: str, cks: list[dict], guard, tag: str) -> str:
    fp = fingerprint()
    jobs = []
    for ck in cks:
        for r in range(REPS):
            key = f"{design}|{ck['task']}|{ck['milestone']}|{r}"
            run = st["runs"].get(key) or {}
            if run.get("status") == "done" and run.get("fingerprint") == fp:
                continue
            jobs.append(first_run_job(design, ck["task"], ck["milestone"], r, tag))
    if not jobs:
        return ""
    log(f"running {len(jobs)} first run(s) of {design} on {sorted({f'{j.task}:{j.milestone}' for j in jobs})}")
    reason = J.execute(jobs, guard=guard, concurrency=2, save=lambda: None)
    sh = sealed()
    from orchestra.control.evolution.bank import suite_version_of
    from joint_eval import trace_hits
    for j in jobs:
        key = f"{j.variant}|{j.task}|{j.milestone}|{j.rep}"
        run_dir = OUT / "runs" / j.job_id / j.task
        rec = {"job_id": j.job_id, "status": j.status, "fingerprint": fp}
        if j.status == "done":
            ws = sorted(run_dir.glob(f"tasks/*/workspaces/{j.milestone}/repo"))
            suite = run_dir / "harness" / f"{j.milestone}.spec_tests"
            if ws and suite.is_dir():
                sc = score_first_run(j.task, j.milestone, ws[0], suite, verifier_for(j.task, j.milestone))
                sh[key] = sc.pop("heldout")
                rec.update(sc)
                orig = ROOT / task_spec(j.task)["checkpoint_run"] / "harness" / f"{j.milestone}.spec_tests"
                rec["suite_same"] = suite_version_of(suite) == suite_version_of(orig)
                rec["tokens"] = J_tokens(run_dir, j.milestone)
                rec["trace_hits"] = trace_hits(run_dir)
            else:
                rec["status"] = "no_workspace"
        st["runs"][key] = rec
    SEALED.write_text(json.dumps(sh))
    save_state(st)
    unfair = [j.job_id for j in jobs if (st["runs"][f"{j.variant}|{j.task}|{j.milestone}|{j.rep}"].get("suite_same") is False
                                         or (st["runs"][f"{j.variant}|{j.task}|{j.milestone}|{j.rep}"].get("trace_hits") or 0) > 0)]
    return reason or (f"fairness: {unfair}" if unfair else "")


def J_tokens(run_dir: Path, mid: str) -> int:
    s = run_dir / "summary.json"
    if s.is_file():
        for o in json.loads(s.read_text()).get("milestone_objectives") or []:
            if o.get("milestone_id") == mid:
                return int(o.get("prompt_tokens") or 0)
    return 0


def _cases(st, design, ck, r):
    rec = st["runs"].get(f"{design}|{ck['task']}|{ck['milestone']}|{r}") or {}
    out = {f"g:{k}": v for k, v in (rec.get("gate") or {}).items()}
    out.update({f"v:{k}": v for k, v in (rec.get("verifier") or {}).items()})
    return out


def _target_failures(st, design, ck, cls) -> int:
    run = ROOT / task_spec(ck["task"])["checkpoint_run"]
    src = P.suite_sources(run / "harness" / f"{ck['milestone']}.spec_tests")
    ver = verifier_for(ck["task"], ck["milestone"])
    vsrc = P.suite_sources(ver) if ver else {}
    n = 0
    for r in range(REPS):
        rec = st["runs"].get(f"{design}|{ck['task']}|{ck['milestone']}|{r}") or {}
        for case, v in (rec.get("gate") or {}).items():
            if v == "fail" and P.classify(case, src) == cls:
                n += 1
        for case, v in (rec.get("verifier") or {}).items():
            if v == "fail" and P.classify(case, vsrc) == cls:
                n += 1
    return n


def pair_stats(st: dict, base_of, new: str, cks: list[dict], cls: str) -> dict:
    sh = sealed()
    per = []
    for ck in cks:
        base = base_of(ck)
        b = [_cases(st, base, ck, r) for r in range(REPS)]
        n = [_cases(st, new, ck, r) for r in range(REPS)]
        keys = set().union(*[set(x) for x in b + n])
        fix = sum(1 for c in keys if all(x.get(c) == "fail" for x in b) and all(x.get(c) == "pass" for x in n))
        reg = sum(1 for c in keys if all(x.get(c) == "pass" for x in b) and all(x.get(c) == "fail" for x in n))
        hb = [sh.get(f"{base}|{ck['task']}|{ck['milestone']}|{r}") or {} for r in range(REPS)]
        hn = [sh.get(f"{new}|{ck['task']}|{ck['milestone']}|{r}") or {} for r in range(REPS)]
        hk = set().union(*[set(x) for x in hb + hn])
        hfix = sum(1 for c in hk if all(x.get(c) == "fail" for x in hb) and all(x.get(c) == "pass" for x in hn))
        hreg = sum(1 for c in hk if all(x.get(c) == "pass" for x in hb) and all(x.get(c) == "fail" for x in hn))
        tok = lambda d: sum((st["runs"].get(f"{d}|{ck['task']}|{ck['milestone']}|{r}") or {}).get("tokens") or 0 for r in range(REPS))  # noqa: E731
        per.append({"checkpoint": f"{ck['task']}:{ck['milestone']}", "position": ck["features"]["position"], "baseline": base,
                    "fix": fix, "reg": reg, "heldout_fix": hfix, "heldout_reg": hreg, "relevant": ck["relevant"],
                    "target_base": _target_failures(st, base, ck, cls), "target_new": _target_failures(st, new, ck, cls),
                    "tokens_base": tok(base), "tokens_new": tok(new)})
    fix, reg = sum(p["fix"] for p in per), sum(p["reg"] for p in per)
    better = sum(1 for p in per if p["fix"] > p["reg"])
    worse = sum(1 for p in per if p["fix"] < p["reg"])
    hf, hr = sum(p["heldout_fix"] for p in per), sum(p["heldout_reg"] for p in per)
    tb, tn = sum(p["target_base"] for p in per), sum(p["target_new"] for p in per)
    cb, cn = sum(p["tokens_base"] for p in per), sum(p["tokens_new"] for p in per)
    conds = {"gate_verifier_fix_gt_reg": fix > reg, "better_gt_worse": better > worse, "targeted_class_down": tn < tb,
             "heldout_safe": hr <= hf, "cost_within_1.3x": (cn <= (1 + COST_TOL) * cb) if cb else True}
    return {"per_checkpoint": per, "fix": fix, "reg": reg, "better": better, "worse": worse, "heldout_fix": hf, "heldout_reg": hr,
            "heldout_safe": hr <= hf, "target_base": tb, "target_new": tn, "tokens_base": cb, "tokens_new": cn,
            "conditions": conds, "accepted": all(conds.values())}


def sample_checkpoints(design: dict, cluster: dict, evo: list[dict], seed: str) -> list[dict]:
    match = [c for c in evo if holds(design.get("triggers"), c["features"]) and verifier_for(c["task"], c["milestone"])]
    occurred = [c for c in match if f"{c['task']}:{c['milestone']}" in cluster["milestones"]]
    other = [c for c in match if c not in occurred]
    rng = random.Random(seed)
    rng.shuffle(occurred)
    rng.shuffle(other)
    n_occ = min(len(occurred), max(math.ceil(MAX_CKPT * 2 / 3), MAX_CKPT - len(other)))
    pick = occurred[:n_occ] + other[: MAX_CKPT - n_occ]
    return pick[:MAX_CKPT]


# --- online runs ------------------------------------------------------------------------------------------------------


def online_run(st: dict, label: str, table: Path, guard) -> dict:
    out = {}
    fp = fingerprint()
    for task in ONLINE_TASKS:
        key = f"{label}|{task}"
        if label == "control" and (st["online"].get(key) or {}).get("fingerprint") == fp:
            out[task] = st["online"][key]
            continue
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"cpe-{stamp}-{task}"
        cmd = [str(ROOT / ".venv" / "bin" / "python"), "-m", "orchestra.cli.run_codeprojecteval_decomp", "--config", CONFIG,
               "--task-id", task, "--arm", f"fp_online_{label}", "--run-id", run_id, "--output-root", str(ONLINE_ROOT / label),
               "--dataset-root", dataset_root(task), "--plan-file", str(ROOT / task_spec(task)["plan"])]
        env = {"ADAMAS_EVOLUTION_ROOT": str(OUT / "evolution_online"), "ADAMAS_FIRST_PASS_TABLE": str(table),
               "ADAMAS_FP_INVENTORY_DIR": str(OUT / "inventory"), **env_extra(task)}
        job = J.Job(f"online-{label}-{task}-{stamp}", "online", task, "", label, 0, env, cmd)
        log(f"online run {label} on {task}")
        reason = J.execute([job], guard=guard, concurrency=1, save=lambda: None, stagger=1.0)
        run_dir = ONLINE_ROOT / label / run_id / task
        rec = {"run_dir": str(run_dir), "status": job.status, "fingerprint": fp, "stopped": reason}
        if job.status == "done":
            ev = ONLINE_ROOT / label / run_id / "hidden_eval.json"
            subprocess.run([sys.executable, str(ROOT / "scripts" / "eval_codeprojecteval.py"), str(ONLINE_ROOT / label / run_id),
                            "--dataset-root", dataset_root(task), "--out", str(ev)], cwd=ROOT, check=False)
            if ev.is_file():
                r = (json.loads(ev.read_text()).get("results") or [{}])[0]
                rec["heldout_pass_rate"] = r.get("pass_rate")
            rec.update(online_metrics(run_dir))
        st["online"][key] = rec
        save_state(st)
        out[task] = rec
        if reason:
            break
    return out


def online_metrics(run_dir: Path) -> dict:
    import glob as _glob
    files = _glob.glob(str(run_dir / "tasks/*/task_execution.json"))
    if not files:
        return {}
    d = json.loads(Path(files[0]).read_text())
    first_fail, repair_tokens = {}, 0
    for sid, sub in (d.get("subtasks") or {}).items():
        fl = (d.get("fast_loop_states") or {}).get(sid) or {}
        inc = next((c for c in fl.get("candidates") or [] if c["candidate_id"] == "incumbent_first_pass"), None)
        first_fail[sid] = len(inc.get("behaviour_failures") or []) if inc else 0
        for c in fl.get("candidates") or []:
            if c["candidate_id"] != "incumbent_first_pass":
                repair_tokens += int((c.get("cost") or {}).get("prompt_tokens") or 0)
    return {"first_run_gate_failures": first_fail, "repair_prompt_tokens": repair_tokens}


# --- main ---------------------------------------------------------------------------------------------------------------


def run(args) -> None:
    from orchestra.control.evolution.fp_evolver import shape_checker
    import joint_iterate as JI
    OUT.mkdir(parents=True, exist_ok=True)
    st = load_state()
    write_tables(st)
    strong, weak = JI.identifiers()
    evo = checkpoints(EVO_TASKS)
    acc = checkpoints(ACC_TASKS)
    start = J.read_window()
    if start is None:
        raise SystemExit("cannot read the account window")
    rnd = len(st["rounds"])
    while rnd < args.rounds:
        rnd += 1
        now = J.read_window() or start
        if now >= WINDOW_STOP:
            st["stopped"] = f"account window at {now}%"
            break
        guard = J.BudgetGuard(J.read_window, round_start=now, total_start=now, round_cap=args.round_cap, total_cap=1000.0)
        guard_window = lambda: (J.read_window() or 0) >= WINDOW_STOP  # noqa: E731
        rec = {"round": rnd, "window_start": now, "clusters": [], "designs": [], "discarded": []}
        log(f"=== round {rnd} (window {now}%)")
        ensure_verifiers(evo + acc, guard)
        exps = build_experiences(strong, weak)
        cl = cluster_experiences(exps, st)
        chosen = [c for c in cl if not c["excluded"]][:K]
        rec["experiences"] = len(exps)
        rec["clusters"] = chosen
        (NS / f"experience_round{rnd}.json").write_text(json.dumps({"experiences": exps, "clusters": cl}, indent=1, default=str))
        if not chosen:
            st["stops"]["no_cluster"] += 1
            st["rounds"].append(rec)
            save_state(st)
            if st["stops"]["no_cluster"] >= 2:
                st["stopped"] = "two consecutive rounds without an eligible experience cluster"
                break
            continue
        st["stops"]["no_cluster"] = 0
        shape = shape_checker([_draft(c) for c in evo])
        designs, discarded = propose(st, chosen, rnd, strong, weak, shape)
        rec["discarded"] = [{"intent": d.get("intent"), "reasons": d.get("_reasons")} for d in discarded]
        if not designs:
            st["stops"]["invalid"] += 1
            st["rounds"].append(rec)
            save_state(st)
            if st["stops"]["invalid"] >= 2:
                st["stopped"] = "two consecutive rounds without a valid proposal"
                break
            continue
        st["stops"]["invalid"] = 0
        enabled_now = []
        for i, d in enumerate(designs, 1):
            did = f"R{rnd}{i}"
            cluster = next(c for c in chosen if c["key"] == d["cluster_key"])
            d = {**d, "entry_id": did, "round": rnd, "status": "trial", "cluster": cluster}
            st["designs"][did] = d
            write_tables(st)
            save_state(st)
            cks = sample_checkpoints(d, cluster, evo, f"{did}")
            d["checkpoints"] = [f"{c['task']}:{c['milestone']}" for c in cks]
            if not cks:
                d.update(status="candidate", note="no evolution checkpoint matches its triggers with a verification suite")
                continue
            half = cks[: math.ceil(len(cks) / 2)]
            base_of = lambda ck: current_design(st, ck)  # noqa: E731
            reason = ""
            for ck in half:
                reason = reason or ensure_runs(st, base_of(ck), [ck], guard, f"r{rnd}")
            reason = reason or ensure_runs(st, did, half, guard, f"r{rnd}")
            if reason:
                st["stopped"] = reason
                break
            first = pair_stats(st, base_of, did, half, cluster["error_class"])
            if first["reg"] > first["fix"]:
                d.update(verdict=first, status=_reject(st, cluster), stopped_early=True)
                log(f"{did}: stopped after half (gate+verification regressions {first['reg']} > fixes {first['fix']})")
                st["designs"][did] = d
                save_state(st)
                continue
            rest = cks[len(half):]
            for ck in rest:
                reason = reason or ensure_runs(st, base_of(ck), [ck], guard, f"r{rnd}")
            reason = reason or ensure_runs(st, did, rest, guard, f"r{rnd}")
            if reason:
                st["stopped"] = reason
                break
            v = pair_stats(st, base_of, did, cks, cluster["error_class"])
            d["verdict"] = v
            log(f"{did}: evolution verdict {'PASS' if v['accepted'] else 'reject'} {v['conditions']}")
            if not v["accepted"]:
                d["status"] = _reject(st, cluster)
                st["designs"][did] = d
                save_state(st)
                continue
            acc_ck = [c for c in acc if holds(d.get("triggers"), c["features"]) and c["relevant"] >= MIN_REL
                      and verifier_for(c["task"], c["milestone"])]
            if len(acc_ck) < MIN_ACCEPT_CKPT:
                d.update(status="pending", note=f"only {len(acc_ck)} acceptance checkpoint(s) match")
                st["designs"][did] = d
                save_state(st)
                continue
            for ck in acc_ck:
                reason = reason or ensure_runs(st, base_of(ck), [ck], guard, f"r{rnd}acc")
            reason = reason or ensure_runs(st, did, acc_ck, guard, f"r{rnd}acc")
            if reason:
                st["stopped"] = reason
                break
            cv = pair_stats(st, base_of, did, acc_ck, cluster["error_class"])
            d["confirmation"] = cv
            log(f"{did}: acceptance {'CONFIRMED' if cv['accepted'] else 'not confirmed'} {cv['conditions']}")
            d["status"] = "enabled" if cv["accepted"] else _reject(st, cluster)
            if cv["accepted"]:
                enabled_now.append(did)
            st["designs"][did] = d
            save_state(st)
        rec["designs"] = [x for x in st["designs"] if st["designs"][x].get("round") == rnd]
        write_tables(st)
        (NS / f"first_pass.round{rnd}.yaml").write_text(TABLE.read_text())
        if st.get("stopped"):
            rec["window_end"] = J.read_window()
            st["rounds"].append(rec)
            save_state(st)
            break
        if enabled_now:
            rec["online_control"] = online_run(st, "control", EMPTY_TABLE, guard)
            rec["online_design"] = online_run(st, f"round{rnd}", TABLE, guard)
        rec["window_end"] = J.read_window()
        st["rounds"].append(rec)
        save_state(st)
        log(f"round {rnd} done: enabled {enabled_now}; window {rec['window_end']}%")
    save_state(st)
    log(f"finished: {st.get('stopped') or 'all rounds done'}")


def _reject(st: dict, cluster: dict) -> str:
    st["cluster_tries"][cluster["key"]] = st["cluster_tries"].get(cluster["key"], 0) + 1
    return "retired" if st["cluster_tries"][cluster["key"]] >= 2 else "rejected"


def _draft(ck: dict):
    from orchestra.realbench.milestone_planner import parse_plan_payload
    d = parse_plan_payload(json.loads((ROOT / task_spec(ck["task"])["plan"]).read_text()), max_agents=6, max_milestones=12)
    return next(m for m in d.milestones if m.milestone_id == ck["milestone"])


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--rounds", type=int, default=3)
    r.add_argument("--round-cap", type=float, default=45.0)
    r.set_defaults(fn=run)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
