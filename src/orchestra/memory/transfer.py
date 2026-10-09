"""Transfer channel: pitfalls from stored repair records (memory spec §5). Memory replay only.

Source: committed default repairs (R0) and node resamples of **training** tasks.
Every gate case such a repair fixed is one raw fact (the incumbent failed it,
the committed candidate passes it). Identical (task, milestone, case) facts from
several runs are generated once and counted as several observations.

Per fact: the case source, the failure output before the repair (from
``repair_evidence/failures.md`` when stored), the repair diff (the hunks that
touch what the case calls) and the document sentences the case cites. One
temperature-0 call turns it into symptom / cause / lesson / category /
applies_when / is_test_artifact_suspect.

Filters, all of which must pass:
1. reference: the case fails on the reference implementation -> a suite error;
   rejected, and a note goes to the author's pending list;
2. test artifact: suspected by the model, or the diff adapts to fake commands,
   fake signatures or environment variables;
3. generality: checked against same-category facts of other milestones (one
   call, yes / no / unsure). With no such facts the pitfall may still enter,
   but only as ``trial`` and marked ``single_source``;
4. leak: the four checks of §2.3.

Pitfalls that pass are merged within their category when one call judges two
lessons the same. ``NEW:`` categories are not created; those pitfalls go to
``transfer/pending_pitfalls.yaml`` for people to review. Everything that passes
is written as ``trial``, the version is bumped and CHANGELOG is updated.
"""

from __future__ import annotations

import ast
import glob
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestra.memory.store import ROOT, commit_write, read_yaml_list, sha256_text, write_yaml
from orchestra.memory.validate import build_identifiers, validate_entries

Call = Callable[[str, str], str]
REFERENCE_SCRIPT = ROOT / "scripts" / "sealed" / "reference_check.py"
CACHE_DIR = ROOT / "outputs" / "memory" / "transfer_cache"
DIFF_CHARS = 7000
ERROR_CHARS = 3000

_ERR_SIGNALS = ("No such file or directory", "unexpected keyword argument", "exit status 127", "command not found",
                "got an unexpected", "takes", "positional argument")
_DIFF_SIGNALS = (r"os\.environ", r"\bPATH\b", r"shutil\.which", r"hasattr\(", r"inspect\.signature", r"except TypeError",
                 r"\*\*kwargs", r"executable", r"/usr/bin/env", r"bash")


@dataclass
class Fact:
    record_id: str
    task: str
    run: str
    milestone: str
    candidate: str
    kind: str                 # R0 | resample
    case: str                 # file::name as the ledger spells it
    case_name: str
    suite_dir: str
    position_group: str
    case_source: str = ""
    citations: list[str] = field(default_factory=list)
    error_output: str = ""
    diff_excerpt: str = ""
    duplicates: list[str] = field(default_factory=list)   # record ids of the same fix in other runs

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def record_id(run: str, mid: str, cand: str, case: str) -> str:
    return "R-" + hashlib.sha1(f"{run}|{mid}|{cand}|{case}".encode()).hexdigest()[:10]


def _norm_case(case: str) -> str:
    parts = case.split("::")
    return parts[0].rsplit("/", 1)[-1] + "::" + "::".join(parts[1:])


def runs_of(task: str) -> list[Path]:
    pats = [f"outputs/*/cpe-*-{task}/{task}", f"outputs/*/cpe-*/{task}", f"outputs/evolution/reruns/*{task}*/{task}"]
    out = set()
    for p in pats:
        for d in glob.glob(str(ROOT / p)):
            if glob.glob(d + "/tasks/*/task_execution.json"):
                out.add(Path(d))
    return sorted(out)


def kind_of(cid: str, meta: dict) -> str | None:
    if "feedback" in cid or cid == "incumbent_first_pass" or meta.get("probe"):
        return None
    k = meta.get("candidate_kind")
    if cid == "cand_R0" or k == "R0" or "continue_improve" in cid:
        return "R0"
    if "node_resample" in cid or k == "resample":
        return "resample"
    return None   # playbook rows are not a transfer source (§5.1)


def case_sources(suite: Path) -> dict[str, tuple[str, list[str]]]:
    from orchestra.codeprojecteval.suite_audit import CITATION_RE

    out: dict[str, tuple[str, list[str]]] = {}
    for p in Path(suite).rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        src = p.read_text(errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                start = min([fn.lineno] + [d.lineno for d in fn.decorator_list]) - 1
                j = start - 1
                while j >= 0 and lines[j].strip().startswith("#"):
                    j -= 1
                body = "\n".join(lines[j + 1: fn.end_lineno])
                out[fn.name] = (body, [m.group("quote") for m in CITATION_RE.finditer(body)])
    return out


def failure_section(failures_md: Path, name: str) -> str:
    if not failures_md.is_file():
        return ""
    lines = failures_md.read_text(errors="replace").splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("_") and re.search(rf"[ .]{re.escape(name)}(?:\[[^\]]*\])? ", ln):
            j = i + 1
            while j < len(lines) and not lines[j].startswith(("____", "====")):
                j += 1
            return "\n".join(lines[i:j])[:ERROR_CHARS]
    return ""


def diff_excerpt(patch: str, case_source: str) -> str:
    """The patch's per-file diffs, those mentioning what the case calls first, cut to DIFF_CHARS."""
    files = re.split(r"(?=^diff --git )", patch or "", flags=re.M)
    names = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\s*\(", case_source))
    scored = sorted(files, key=lambda f: -sum(1 for n in names if n in f))
    out, total = [], 0
    for f in scored:
        if not f.strip():
            continue
        if total + len(f) > DIFF_CHARS:
            out.append(f[: max(0, DIFF_CHARS - total)])
            break
        out.append(f)
        total += len(f)
    return "".join(out)


def _patch_of(run: Path, mid: str, cand: str) -> str:
    for a in glob.glob(str(run / "tasks" / f"*{mid}__candidate__{cand}" / "artifacts" / "*freeze_change*.json")):
        try:
            return str(json.loads(Path(a).read_text())["payload"].get("patch") or "")
        except (OSError, ValueError, KeyError):
            continue
    return ""


def _positions(task: str) -> dict[str, str]:
    p = ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{task}.plan.json"
    if not p.is_file():
        return {}
    ms = [m["milestone_id"] for m in json.loads(p.read_text()).get("milestones") or []]
    return {m: ("first" if i == 0 else "last" if i == len(ms) - 1 else "middle") for i, m in enumerate(ms)}


def collect_facts(train_tasks: Iterable[str]) -> tuple[list[Fact], int]:
    """(unique facts, number of raw facts)."""
    facts: dict[tuple[str, str, str], Fact] = {}
    raw = 0
    for task in train_tasks:
        pos = _positions(task)
        for run in runs_of(task):
            for te in glob.glob(str(run / "tasks" / "*" / "task_execution.json")):
                d = json.loads(Path(te).read_text())
                for mid, v in (d.get("fast_loop_states") or {}).items():
                    inc = next((c for c in v["candidates"] if c["candidate_id"] == "incumbent_first_pass"), None)
                    if not inc:
                        continue
                    for c in v["candidates"]:
                        if c.get("status") != "committed":
                            continue
                        meta = c.get("metadata") or {}
                        k = kind_of(c["candidate_id"], meta)
                        if not k:
                            continue
                        fixed = {_norm_case(x) for x in inc.get("behaviour_failures") or []} - {_norm_case(x) for x in c.get("behaviour_failures") or []}
                        suite = run / "harness" / f"{mid}.spec_tests"
                        srcs = case_sources(suite) if fixed else {}
                        ev = meta.get("repair_evidence")
                        patch = _patch_of(run, mid, c["candidate_id"]) if fixed else ""
                        for case in sorted(fixed):
                            raw += 1
                            name = case.split("::")[-1].split("[")[0]
                            rid = record_id(str(run.relative_to(ROOT)), mid, c["candidate_id"], case)
                            key = (task, mid, name)
                            body, cites = srcs.get(name, ("", []))
                            err = failure_section(Path(ev) / "failures.md", name) if ev else ""
                            if key in facts:
                                f = facts[key]
                                f.duplicates.append(rid)
                                if not f.error_output and err:
                                    f.error_output = err
                                continue
                            facts[key] = Fact(
                                record_id=rid, task=task, run=str(run.relative_to(ROOT)), milestone=mid, candidate=c["candidate_id"],
                                kind=k, case=case, case_name=name, suite_dir=str(suite), position_group=pos.get(mid, "middle"),
                                case_source=body, citations=cites, error_output=err, diff_excerpt=diff_excerpt(patch, body),
                            )
    return list(facts.values()), raw


# --------------------------------------------------------------------------- model calls


GEN_SYSTEM = (
    "You turn one repaired test failure into a reusable lesson for implementers of other projects. "
    "Answer with one JSON object only."
)


def gen_prompt(f: Fact, categories: list[dict], tags: list[str]) -> str:
    cats = json.dumps([{k: c.get(k) for k in ("category_id", "name", "definition")} for c in categories], ensure_ascii=False)
    return (
        "A first implementation failed the acceptance case below; a later repair fixed it.\n\n"
        f"## Case source\n```python\n{f.case_source[:5000]}\n```\n\n"
        f"## Document sentences the case cites\n" + ("\n".join(f"- {c}" for c in f.citations) or "(none)") + "\n\n"
        f"## Failure output before the repair\n```\n{f.error_output or '(not stored)'}\n```\n\n"
        f"## Repair diff (excerpt)\n```diff\n{f.diff_excerpt}\n```\n\n"
        f"## Milestone position\n{f.position_group}\n\n"
        f"## Category catalogue\n{cats}\n\n## Domain tag vocabulary\n{json.dumps(tags)}\n\n"
        "## Answer\nReturn one JSON object:\n"
        '{"symptom": "...", "cause": "...", "lesson": "...", "proposed_category": "<catalogue id, or NEW:<name>: <definition>>", '
        '"applies_when": {"domain_tags": [<from the vocabulary>], "position": [<first|middle|last>]}, '
        '"is_test_artifact_suspect": true|false, "artifact_reason": "..."}\n'
        "Rules: symptom, cause and lesson must be general -- never name the project, package, module, file, function, "
        "class, test or any identifier from the code above, and never quote the test. The lesson says what an implementer "
        "should do from the documents alone. is_test_artifact_suspect is true when the repair only adapts to how the "
        "test is built (a fake command or executable, a fake function's signature, environment variables, a mock's "
        "shape) rather than to a documented behaviour."
    )


def _cached_call(call: Call, system: str, prompt: str, *, tag: str) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{tag}-{sha256_text(system + prompt)[:16]}.json"
    if p.is_file():
        return json.loads(p.read_text())["reply"]
    reply = call(system, prompt)
    p.write_text(json.dumps({"prompt": prompt, "reply": reply}), encoding="utf-8")
    return reply


def _json(reply: str) -> dict[str, Any] | list:
    m = re.search(r"[\{\[].*[\}\]]", reply or "", re.S)
    if not m:
        raise ValueError("no JSON in reply")
    return json.loads(m.group(0))


def generate(facts: list[Fact], categories: list[dict], tags: list[str], call: Call, workers: int = 6) -> dict[str, dict[str, Any]]:
    def one(f: Fact):
        try:
            d = _json(_cached_call(call, GEN_SYSTEM, gen_prompt(f, categories, tags), tag="gen"))
            return f.record_id, d if isinstance(d, dict) else {"_error": "not an object"}
        except Exception as exc:  # noqa: BLE001
            return f.record_id, {"_error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, facts))


GENERAL_SYSTEM = "You judge whether a lesson holds for other observed failures. Answer with one JSON object only."


def generality(lesson: str, others: list[dict[str, str]], call: Call) -> tuple[str, str]:
    prompt = (
        f"## Lesson\n{lesson}\n\n## Other observed failures in the same category (other milestones)\n"
        + "\n".join(f"- symptom: {o['symptom']} | cause: {o['cause']}" for o in others[:6])
        + '\n\n## Answer\nDoes the lesson, applied before implementation, also hold for these failures?\n'
        '{"answer": "yes|no|unsure", "reason": "..."}'
    )
    d = _json(_cached_call(call, GENERAL_SYSTEM, prompt, tag="general"))
    ans = str((d or {}).get("answer") or "unsure").lower() if isinstance(d, dict) else "unsure"
    return (ans if ans in ("yes", "no", "unsure") else "unsure"), str((d or {}).get("reason") or "") if isinstance(d, dict) else ""


MERGE_SYSTEM = "You group lessons that say the same thing. Answer with one JSON object only."


def merge_groups(lessons: list[str], call: Call) -> list[list[int]]:
    if len(lessons) < 2:
        return [[i] for i in range(len(lessons))]
    prompt = ("## Lessons\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(lessons))
              + '\n\n## Answer\nGroup the indices of lessons that give the same advice (same behaviour, same fix). '
              'Every index appears exactly once.\n{"groups": [[0, 3], [1], [2]]}')
    d = _json(_cached_call(call, MERGE_SYSTEM, prompt, tag="merge"))
    groups = [[int(i) for i in g] for g in (d.get("groups") if isinstance(d, dict) else []) or []]
    seen = [i for g in groups for i in g]
    if sorted(seen) != list(range(len(lessons))):
        return [[i] for i in range(len(lessons))]     # malformed: merge nothing
    return groups


# --------------------------------------------------------------------------- filters


def reference_failures(task: str, suite: str, cache: dict[str, list[str]]) -> list[str]:
    """Case names the suite fails on the reference implementation (sealed script; training tasks only)."""
    key = f"{task}|{suite}"
    if key not in cache:
        out = CACHE_DIR / f"ref-{hashlib.sha1(key.encode()).hexdigest()[:12]}.json"
        if not out.is_file():
            subprocess.run([sys.executable, str(REFERENCE_SCRIPT), "--task", task, "--suite", suite, "--out", str(out)],
                           capture_output=True, text=True, timeout=1800, cwd=str(ROOT))
        try:
            data = json.loads(out.read_text())
        except (OSError, ValueError):
            data = {}
        if not data.get("total"):
            raise RuntimeError(f"reference check produced no result for {task} {suite}")
        failed = data.get("reference_failed") or []
        cache[key] = [str(c).split("::")[-1].split("[")[0] for c in failed]
    return cache[key]


def test_artifact(f: Fact, gen: dict[str, Any]) -> str:
    if gen.get("is_test_artifact_suspect") is True:
        return f"model: {gen.get('artifact_reason') or 'repair adapts to the test construction'}"
    err_hit = any(s in f.error_output for s in _ERR_SIGNALS)
    added = "\n".join(ln for ln in f.diff_excerpt.splitlines() if ln.startswith("+"))
    diff_hits = [p for p in _DIFF_SIGNALS if re.search(p, added)]
    if err_hit and len(diff_hits) >= 2:
        return f"diff adapts to the test environment ({', '.join(diff_hits[:4])}) after an environment-shaped failure"
    return ""


# --------------------------------------------------------------------------- the channel


def run_transfer(*, root: Path, train_tasks: list[str], test_tasks: list[str], call: Call, date: str | None = None,
                 out_dir: Path | None = None, workers: int = 6) -> dict[str, Any]:
    date = date or datetime.now(UTC).strftime("%Y-%m-%d")
    out_dir = out_dir or ROOT / "outputs" / "memory" / f"transfer_{date}"
    out_dir.mkdir(parents=True, exist_ok=True)
    from orchestra.memory.store import pin

    view = pin(root)
    cats = view.active_categories("first_pass")
    tags = view.domain_tags()
    cat_ids = [c["category_id"] for c in cats]
    facts, raw = collect_facts(train_tasks)
    stats: dict[str, Any] = {"raw_facts": raw, "unique_facts": len(facts), "by_task": {}, "rejected": {}, "pending_new": 0}
    for f in facts:
        stats["by_task"][f.task] = stats["by_task"].get(f.task, 0) + 1
    (out_dir / "facts.json").write_text(json.dumps([f.to_dict() for f in facts], indent=1), encoding="utf-8")
    gens = generate(facts, cats, tags, call, workers=workers)
    (out_dir / "generated.json").write_text(json.dumps(gens, indent=1, ensure_ascii=False), encoding="utf-8")
    errors = [g["_error"] for g in gens.values() if g.get("_error")]
    if facts and len(errors) * 2 > len(facts):
        # an infrastructure failure, not a judgement on the facts: stop before writing anything
        raise RuntimeError(f"{len(errors)}/{len(facts)} generation calls failed, e.g. {errors[0]}")

    sources = {r["record_id"]: r for r in read_yaml_list(root / "transfer" / "sources.yaml")}
    for f in facts:
        for rid in [f.record_id, *f.duplicates]:
            sources[rid] = {"record_id": rid, "task": f.task, "split": "train" if f.task in train_tasks else "test",
                            "milestone": f.milestone, "case": f.case_name, "kind": f.kind}
    rejected: list[dict[str, Any]] = []
    author_pending: list[dict[str, Any]] = []
    survivors: list[tuple[Fact, dict[str, Any]]] = []
    ref_cache: dict[str, list[str]] = {}

    def reject(f: Fact, stage: str, why: str, gen: dict[str, Any] | None = None):
        rejected.append({"record_id": f.record_id, "stage": stage, "reason": why, "generated": gen, "date": date})
        stats["rejected"][stage] = stats["rejected"].get(stage, 0) + 1

    for f in facts:
        g = gens.get(f.record_id) or {}
        if g.get("_error") or not all(str(g.get(k) or "").strip() for k in ("symptom", "cause", "lesson")):
            reject(f, "generation", g.get("_error") or "incomplete answer", g)
            continue
        if f.task in test_tasks:
            reject(f, "test_task", "record of a test task", g)
            continue
        if f.case_name in reference_failures(f.task, f.suite_dir, ref_cache):
            reject(f, "reference", "the case fails on the reference implementation (suite error)", g)
            author_pending.append({"record_id": f.record_id, "milestone": f.milestone, "case": f.case_name,
                                   "note": "gate case fails on the reference implementation", "date": date})
            continue
        why = test_artifact(f, g)
        if why:
            reject(f, "test_artifact", why, g)
            continue
        survivors.append((f, g))

    # 3. generality, within the proposed category, against other milestones
    passed: list[tuple[Fact, dict[str, Any], bool]] = []
    for f, g in survivors:
        cat = str(g.get("proposed_category") or "")
        others = [{"symptom": str(g2.get("symptom")), "cause": str(g2.get("cause"))}
                  for f2, g2 in survivors if str(g2.get("proposed_category")) == cat and f2.milestone != f.milestone]
        if not others:
            passed.append((f, g, True))
            continue
        ans, reason = generality(str(g["lesson"]), others, call)
        g["generality"] = {"answer": ans, "reason": reason, "compared": len(others)}
        if ans == "no":
            reject(f, "generality", reason or "lesson does not hold for the category's other milestones", g)
            continue
        passed.append((f, g, False))

    # candidates -> entries; NEW categories go to pending
    pending: list[dict[str, Any]] = read_yaml_list(root / "transfer" / "pending_pitfalls.yaml")
    entries: list[dict[str, Any]] = []
    for f, g, single in passed:
        cat = str(g.get("proposed_category") or "")
        aw = g.get("applies_when") or {}
        entry = {
            "category_id": cat, "symptom": str(g["symptom"]).strip(), "cause": str(g["cause"]).strip(),
            "lesson": str(g["lesson"]).strip(),
            "applies_when": {"domain_tags": [t for t in (aw.get("domain_tags") or []) if t in tags],
                             "position": [p for p in (aw.get("position") or []) if p in ("first", "middle", "last")]},
            "evidence": {"source_records": [f.record_id, *f.duplicates], "fixes_observed": 1 + len(f.duplicates),
                         "milestones_observed": 1, "milestones": [f"{f.task}:{f.milestone}"]},
            "state": "trial", "single_source": single, "origin": f"transfer:{date}",
        }
        if cat.startswith("NEW:") or cat not in cat_ids:
            pending.append({**entry, "proposed_category": cat, "date": date})
            stats["pending_new"] += 1
            continue
        entries.append(entry)

    # 4. leak checks (format, identifiers, sources, substrings) before anything is written
    ids = build_identifiers(train_tasks)
    for i, e in enumerate(entries):
        e["pitfall_id"] = f"T-{i:04d}"
    write_yaml(root / "transfer" / "sources.yaml", list(sources.values()))
    verdicts = validate_entries("first_pass", "pitfalls", entries, category_ids=cat_ids, domain_tags=tags, identifiers=ids,
                                sources=sources, test_tasks=test_tasks)
    ok_ids = {v.entry_id for v in verdicts if v.ok}
    for v in verdicts:
        if not v.ok:
            e = next(x for x in entries if x["pitfall_id"] == v.entry_id)
            rejected.append({"record_id": e["evidence"]["source_records"][0], "stage": "leak", "reason": "; ".join(v.reasons),
                             "generated": e, "date": date})
            stats["rejected"]["leak"] = stats["rejected"].get("leak", 0) + 1
    entries = [e for e in entries if e["pitfall_id"] in ok_ids]

    # merge within a category
    merged: list[dict[str, Any]] = []
    for cat in sorted({e["category_id"] for e in entries}):
        group = [e for e in entries if e["category_id"] == cat]
        for g_idx in merge_groups([e["lesson"] for e in group], call):
            items = [group[i] for i in g_idx]
            head = dict(items[0])
            if len(items) > 1:
                ev = head["evidence"]
                ev["source_records"] = sorted({r for it in items for r in it["evidence"]["source_records"]})
                ev["fixes_observed"] = sum(it["evidence"]["fixes_observed"] for it in items)
                ev["milestones"] = sorted({m for it in items for m in it["evidence"]["milestones"]})
                ev["milestones_observed"] = len(ev["milestones"])
                head["single_source"] = ev["milestones_observed"] < 2 and all(it.get("single_source") for it in items)
                head["merged_from"] = [it["pitfall_id"] for it in items]
            merged.append(head)

    existing = read_yaml_list(root / "first_pass" / "pitfalls.yaml")
    n0 = max([int(str(p["pitfall_id"]).split("-")[1]) for p in existing if re.fullmatch(r"P-\d+", str(p.get("pitfall_id")))] or [0])
    new_version = None
    for i, e in enumerate(merged, 1):
        e["pitfall_id"] = f"P-{n0 + i:04d}"
        e["evidence"].pop("milestones", None)
    if merged:
        from orchestra.memory.store import current_version

        for e in merged:
            e["memory_version_added"] = current_version(root) + 1
        new_version = commit_write(root, "first_pass", "pitfalls", existing + merged, reason=f"transfer channel {date}",
                                   origin=f"transfer:{date}", changed_ids=[e["pitfall_id"] for e in merged])
    write_yaml(root / "transfer" / "rejected.yaml", read_yaml_list(root / "transfer" / "rejected.yaml") + rejected)
    write_yaml(root / "transfer" / "pending_pitfalls.yaml", pending)
    write_yaml(root / "transfer" / "author_pending.yaml", read_yaml_list(root / "transfer" / "author_pending.yaml") + author_pending)
    stats.update({"written": len(merged), "new_version": new_version,
                  "written_by_category": {c: sum(1 for e in merged if e["category_id"] == c) for c in cat_ids},
                  "written_by_source_task": {}, "single_source": sum(1 for e in merged if e.get("single_source"))})
    for e in merged:
        for t in {sources[r]["task"] for r in e["evidence"]["source_records"] if r in sources}:
            stats["written_by_source_task"][t] = stats["written_by_source_task"].get(t, 0) + 1
    (out_dir / "stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    return stats


__all__ = ["Fact", "collect_facts", "generate", "run_transfer"]
