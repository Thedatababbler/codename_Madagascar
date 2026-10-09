"""Checks every memory entry passes before it is written (memory spec §2.3, §3.4).

1. format: required fields, legal state, known category, tags from the vocabulary,
   first-pass patterns without a template (the compiler cannot apply one yet);
2. held-out and verification substrings: ``scripts/sealed/memory_leak_check.py``
   (run as a subprocess, so no held-out path is ever handed to this module);
3. training-task identifiers: task, package and milestone names, case names and
   multi-part public symbols anywhere; single plain words that are module or
   symbol names only when written as code (backticks, a call, a dotted path);
4. sources: an entry whose ``evidence.source_records`` names a record of a test
   task is refused.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestra.memory.store import ID_FIELD, ROOT, STATES, read_yaml_list

READ_ONLY_ROLES = ("contract_critic", "spec_auditor", "behaviour_critic")
LEAK_SCRIPT = ROOT / "scripts" / "sealed" / "memory_leak_check.py"
#: free-text fields that reach a prompt
TEXT_FIELDS = ("name", "definition", "signals", "symptom", "cause", "lesson", "instruction", "rule", "trigger", "intent", "example")


@dataclass
class Verdict:
    entry_id: str
    ok: bool
    reasons: list[str] = field(default_factory=list)


def entry_id(name: str, entry: Mapping[str, Any]) -> str:
    return str(entry.get(ID_FIELD.get(name, "id")) or "")


def entry_text(entry: Mapping[str, Any]) -> str:
    """Every prompt-bound string of an entry, flattened: the free-text fields (and all
    strings nested under them), a pattern's instruction, a migrated row's instruction and intent."""
    out: list[str] = []

    def strings(v: Any) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, Mapping):
            for x in v.values():
                strings(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                strings(x)

    for k in TEXT_FIELDS:
        if k in entry:
            strings(entry[k])
    for sub in ("action", "row"):
        d = entry.get(sub)
        if isinstance(d, Mapping):
            for k in ("instruction", "intent"):
                if d.get(k):
                    out.append(str(d[k]))
    return "\n".join(out)


# --------------------------------------------------------------------------- 1. format


def format_errors(bank: str, name: str, entry: Mapping[str, Any], *, category_ids: Iterable[str] = (),
                  domain_tags: Iterable[str] = (), roles: Iterable[str] | None = None) -> list[str]:
    errs: list[str] = []
    eid = entry_id(name, entry)
    if not eid:
        errs.append(f"missing {ID_FIELD.get(name)}")
    state = str(entry.get("state") or "")
    if state not in STATES and name != "categories":
        errs.append(f"state {state!r} not in {STATES}")
    if name == "categories":
        if state not in ("active", "candidate", "retired"):
            errs.append(f"category state {state!r} must be active, candidate or retired")
        for k in ("name", "definition"):
            if not str(entry.get(k) or "").strip():
                errs.append(f"category needs {k}")
        return errs
    cats = set(category_ids)
    cid = str(entry.get("category_id") or "")
    if not cid:
        errs.append("missing category_id")
    elif cats and cid not in cats:
        errs.append(f"unknown category {cid!r}")
    tags = set(domain_tags)
    aw = entry.get("applies_when") or {}
    if not isinstance(aw, Mapping):
        errs.append("applies_when must be a mapping")
    else:
        bad = [t for t in (aw.get("domain_tags") or []) if tags and t not in tags]
        if bad:
            errs.append(f"domain tags outside the vocabulary: {bad}")
        bad_pos = [p for p in (aw.get("position") or []) if p not in ("first", "middle", "last")]
        if bad_pos:
            errs.append(f"position must be first/middle/last, got {bad_pos}")
    if name == "pitfalls":
        for k in ("symptom", "cause", "lesson"):
            if not str(entry.get(k) or "").strip():
                errs.append(f"pitfall needs {k}")
    if name == "patterns" and bank == "first_pass":
        action = entry.get("action") or {}
        if action.get("template"):
            errs.append("template replacement is not supported (the compiler cannot apply it yet)")
        role = action.get("add_readonly_role")
        if role and role not in READ_ONLY_ROLES:
            errs.append(f"add_readonly_role must be one of {READ_ONLY_ROLES}, got {role!r}")
        if role and roles is not None and role not in set(roles):
            errs.append(f"unknown role {role!r}")
        if not (action.get("instruction") or role or action.get("budget_delta")):
            errs.append("a pattern needs an instruction, a read-only role or a budget change")
    if name == "rules" and not str(entry.get("rule") or "").strip():
        errs.append("rule needs rule text")
    return errs


# --------------------------------------------------------------------------- 3. identifiers

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]{2,}")
_CODE_SPANS = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class Identifiers:
    strong: frozenset[str]   # banned anywhere (lower-case)
    weak: frozenset[str]     # banned when written as code

    def to_dict(self) -> dict[str, list[str]]:
        return {"strong": sorted(self.strong), "weak": sorted(self.weak)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Identifiers:
        return cls(frozenset(d.get("strong") or ()), frozenset(d.get("weak") or ()))


def identifier_leaks(text: str, ids: Identifiers) -> list[str]:
    found: set[str] = set()
    for tok in _WORD.findall(text or ""):
        if tok.lower() in ids.strong:
            found.add(tok)
    code_bits: list[str] = list(_CODE_SPANS.findall(text or ""))
    code_bits += re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text or "")          # name(
    code_bits += re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)", text or "")  # a.b
    for bit in code_bits:
        for tok in re.split(r"[^A-Za-z0-9_]+", bit):
            if tok.lower() in ids.weak or tok.lower() in ids.strong:
                found.add(tok)
    return sorted(found)


def _multi_part(word: str) -> bool:
    return ("_" in word and len(word) > 4) or (sum(c.isupper() for c in word) >= 2 and any(c.islower() for c in word))


def build_identifiers(train_tasks: Iterable[str], *, extra_case_names: Iterable[str] = ()) -> Identifiers:
    """Task / package / milestone / module / public-symbol / case names of the training tasks."""
    from orchestra.codeprojecteval.public_symbols import derive_public_symbols, load_docs

    strong: set[str] = set()
    weak: set[str] = set()
    for t in train_tasks:
        t_low = t.lower()
        strong.add(t_low)
        strong.update(p for p in re.split(r"[^a-z0-9]+", t_low) if len(p) >= 4)
        root = _dataset_root(t)
        cfg_p = root / t / "config.json"
        if cfg_p.is_file():
            cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
            pkg = str(cfg.get("source_code") or t).split("/")[0].lower()
            if len(pkg) >= 3:
                strong.add(pkg)
            try:
                inv = derive_public_symbols(load_docs(root / t / "docs"))
                for n in inv.names():
                    leaf = n.split(".")[-1]
                    if len(leaf) < 3:
                        continue
                    (strong if _multi_part(leaf) else weak).add(leaf.lower())
            except Exception:  # noqa: BLE001 -- a task without parseable docs still has its names
                pass
        plan_p = ROOT / "configs" / "datasets" / "cpe_feature_plans" / f"{t}.plan.json"
        if plan_p.is_file():
            plan = json.loads(plan_p.read_text(encoding="utf-8"))
            for m in plan.get("milestones") or []:
                strong.add(str(m.get("milestone_id") or "").lower())
                for fp in m.get("focus_paths") or []:
                    stem = Path(str(fp)).stem.lower()
                    if stem.startswith("__") or len(stem) < 3:
                        continue
                    (strong if _multi_part(stem) else weak).add(stem)
    for case in extra_case_names:
        name = str(case).split("::")[-1].split("[")[0].lower()
        if len(name) >= 3:
            strong.add(name)
            if name.startswith("test_") and len(name) > 9:
                strong.add(name[5:])
    strong.discard("")
    std = stdlib_names()
    # a standard-library or builtin name cannot identify a training task (unittest, Mock, environ, ...)
    return Identifiers(frozenset(strong - std), frozenset(weak - strong - std))


def stdlib_names() -> frozenset[str]:
    import builtins
    import importlib

    names = {n.lower() for n in getattr(sys, "stdlib_module_names", ())} | {n.lower() for n in dir(builtins)}
    for mod in ("os", "os.path", "sys", "subprocess", "shutil", "socket", "ssl", "unittest", "unittest.mock", "urllib.request",
                "urllib.parse", "http.client", "json", "pathlib", "tempfile", "io", "re", "typing", "collections", "functools",
                "itertools", "datetime", "logging", "threading", "asyncio", "contextlib", "dataclasses", "enum", "abc", "inspect"):
        try:
            names |= {n.lower() for n in dir(importlib.import_module(mod)) if not n.startswith("_")}
        except ImportError:
            continue
    return frozenset(names)


def _dataset_root(task: str) -> Path:
    nl2 = Path("/root/codex-benchmarks/nl2repo/cpe_format")
    if task.startswith("nl2_") and (nl2 / task).is_dir():
        return nl2
    return Path("/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset")


# --------------------------------------------------------------------------- 2. substrings (sealed)


def substring_check(texts: Mapping[str, str], *, extra_heldout: Iterable[Path] = (), tasks: Iterable[str] = ()) -> dict[str, dict[str, str]]:
    """Run the sealed script; returns {id: {"heldout": PASS|FAIL, "verification": PASS|FAIL}}."""
    if not texts:
        return {}
    with tempfile.TemporaryDirectory() as d:
        inp, out = Path(d) / "in.json", Path(d) / "out.json"
        inp.write_text(json.dumps(dict(texts)), encoding="utf-8")
        cmd = [sys.executable, str(LEAK_SCRIPT), "--entries", str(inp), "--out", str(out)]
        for p in extra_heldout:
            cmd += ["--extra-heldout", str(p)]
        if tasks:
            cmd += ["--tasks", ",".join(tasks)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, cwd=str(ROOT))
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError(f"memory_leak_check failed: {proc.stderr[-500:]}")
        return json.loads(out.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- 4. sources


def source_errors(entry: Mapping[str, Any], sources: Mapping[str, Mapping[str, Any]], test_tasks: Iterable[str]) -> list[str]:
    recs = list(((entry.get("evidence") or {}).get("source_records")) or [])
    tests = set(test_tasks)
    errs = []
    for r in recs:
        info = sources.get(str(r))
        if info is None:
            errs.append(f"source record {r} has no provenance")
        elif info.get("task") in tests or info.get("split") == "test":
            errs.append(f"source record {r} comes from a test task")
    return errs


def load_sources(root: Path) -> dict[str, dict[str, Any]]:
    return {str(d.get("record_id")): d for d in read_yaml_list(Path(root) / "transfer" / "sources.yaml")}


# --------------------------------------------------------------------------- all four


def validate_entries(bank: str, name: str, entries: list[Mapping[str, Any]], *, category_ids: Iterable[str],
                     domain_tags: Iterable[str], identifiers: Identifiers, sources: Mapping[str, Mapping[str, Any]],
                     test_tasks: Iterable[str], roles: Iterable[str] | None = None, substring: bool = True,
                     extra_heldout: Iterable[Path] = (), heldout_tasks: Iterable[str] = ()) -> list[Verdict]:
    cats = list(category_ids)
    tags = list(domain_tags)
    verdicts = []
    texts: dict[str, str] = {}
    for e in entries:
        eid = entry_id(name, e) or f"<no id {len(verdicts)}>"
        reasons = format_errors(bank, name, e, category_ids=cats, domain_tags=tags, roles=roles)
        text = entry_text(e)
        leaks = identifier_leaks(text, identifiers)
        if leaks:
            reasons.append(f"training-task identifiers: {leaks}")
        reasons += source_errors(e, sources, test_tasks)
        texts[eid] = text
        verdicts.append(Verdict(eid, not reasons, reasons))
    if substring and texts:
        res = substring_check(texts, extra_heldout=extra_heldout, tasks=heldout_tasks)
        for v in verdicts:
            r = res.get(v.entry_id) or {}
            if r.get("heldout") == "FAIL":
                v.reasons.append("shares a 20-character substring with a held-out source")
            if r.get("verification") == "FAIL":
                v.reasons.append("shares a 20-character substring with a verification suite")
            v.ok = not v.reasons
    return verdicts


__all__ = [
    "Identifiers", "READ_ONLY_ROLES", "TEXT_FIELDS", "Verdict", "build_identifiers", "entry_id", "entry_text",
    "format_errors", "identifier_leaks", "load_sources", "source_errors", "substring_check", "validate_entries",
]
