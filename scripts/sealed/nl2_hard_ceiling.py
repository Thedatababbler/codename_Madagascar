#!/usr/bin/env python3
"""Documents-only ceiling of the NL2Repo hard-level candidates, before any solving agent runs (sealed; 2026-10-16).

For each task: every held-out case labelled A (documents give the detail) / B (behaviour only) / C (not described) /
U (no project symbol) by the three-level script (doc_label), attribution by the task's frozen plan; a sealed judge
(gpt-5.6-luna, capped at 2 quota points by this script) re-labels up to 40 cases per task stratified by test file;
script-judge agreement; documents characters per reference line. Case ids, labels and numbers only leave this script;
the judge's prompts and replies go to the sealed log directory.

    uv run python scripts/sealed/nl2_hard_ceiling.py labels nl2_cerberus nl2_autopep8 nl2_deepdiff nl2_pypinyin
    uv run python scripts/sealed/nl2_hard_ceiling.py judge [--cap 2.0]
    uv run python scripts/sealed/nl2_hard_ceiling.py summary
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard as G  # noqa: E402
import nl2_ceiling as NC  # noqa: E402

import os as _os

OUT = Path(_os.environ.get("NL2_CEILING_OUT") or (ROOT / "outputs" / "nl2repo_hard" / "ceiling"))
SEALED_LOG = Path(_os.environ.get("NL2_CEILING_LOG") or "/root/.adamas_sealed/nl2_hard_ceiling_judge")
PLANS = ROOT / "configs" / "datasets" / "nl2repo_hard_plans"
LETTER = {"detail_specified": "A", "behaviour_only": "B", "not_specified": "C", "unknown": "U"}


def hid(case: str) -> str:
    """The id a case leaves this script under (names of held-out tests are not written out)."""
    import hashlib
    return "c" + hashlib.sha1(case.encode()).hexdigest()[:12]


def fid(path: str) -> str:
    import hashlib
    return "f" + hashlib.sha1(path.encode()).hexdigest()[:8]


def _write(path: Path, data, task: str) -> None:
    text = json.dumps(data, indent=1, sort_keys=True)
    G.assert_no_source_overlap(text, task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def cmd_labels(a) -> None:
    import doc_label
    import heldout_attribution as HA

    for task in a.tasks:
        root = G.dataset_root_for(task)
        HA.DATASET_ROOT = root
        doc_label.DATASET_ROOT = root
        cfg = G.task_config(task)
        src = str(cfg.get("source_code"))
        packages = [Path(src).name.removesuffix(".py").split("/")[-1]]
        syms = HA.case_symbols(task, packages)
        keys = NC.heldout_case_keys(task)
        plan = PLANS / f"{task}.plan.json"
        attribution = HA.attribute(task, plan) if plan.is_file() else {}
        seen = {c for cs in attribution.values() for c in cs}
        attr = dict(attribution)
        attr["__unattributed__"] = [k for k in keys if k not in seen]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / f"{task}.json").write_text(json.dumps({"attribution": attr, "case_symbols": syms}))
            doc_label.ATTRIBUTION_ROOT = Path(tmp)
            labels = doc_label.label_task(task)
        ref = G.reference_root(task) / src
        files = [ref] if ref.is_file() else [p for p in ref.rglob("*.py") if "__pycache__" not in p.parts]
        loc = sum(len(p.read_text(errors="replace").splitlines()) for p in files)
        docs_chars = sum(len(p.read_text(errors="replace")) for p in (root / task / "docs").glob("*") if p.is_file())
        out = {"task": task, "cases": len(keys), "reference_lines": loc, "docs_chars": docs_chars,
               "docs_chars_per_line": round(docs_chars / loc, 2) if loc else None,
               "labels": {hid(k): LETTER.get(v["depth"], "U") for k, v in labels.items()},
               "file_of": {hid(k): fid(k.split("::", 1)[0]) for k in keys}}
        _write(OUT / f"{task}.json", out, task)
        print(task, len(keys), Counter(out["labels"].values()), "docs/line", out["docs_chars_per_line"])


def sample_by_file(task: str, keys: list[str], n: int = 40, seed: int = 20261016) -> list[str]:
    if len(keys) <= n:
        return sorted(keys)
    strata: dict[str, list[str]] = defaultdict(list)
    for k in keys:
        strata[k.split("::", 1)[0]].append(k)
    quota = {f: max(1, round(n * len(v) / len(keys))) for f, v in strata.items()}
    while sum(quota.values()) > n:
        quota[max(quota, key=lambda f: quota[f])] -= 1
    while sum(quota.values()) < n:
        quota[max(strata, key=lambda f: len(strata[f]) - quota[f])] += 1
    rng = random.Random(f"{seed}:{task}")
    out = []
    for f, v in sorted(strata.items()):
        out += rng.sample(sorted(v), min(len(v), quota[f]))
    return sorted(out)


def cmd_judge(a) -> None:
    import joint_experiment as JE
    from quota_spend import Spend

    SEALED_LOG.mkdir(parents=True, exist_ok=True)
    JE.read_window = NC._window_reader(a.model)
    spend = Spend()
    res_p = OUT / "judge.json"
    state = json.loads(res_p.read_text()) if res_p.is_file() else {"labels": {}, "calls": [], "stopped": "",
                                                                   "window_before": spend.w0}
    tokens = int(state.get("tokens") or 0)
    for f in sorted(OUT.glob("nl2_*.json")):
        task = f.stem
        lab = json.loads(f.read_text())
        sample = sample_by_file(task, NC.heldout_case_keys(task))
        todo = [c for c in sample if hid(c) not in state["labels"].get(task, {})]
        docs = NC._docs_text(task)
        for i in range(0, len(todo), a.batch):
            if spend.poll() >= a.cap or tokens >= int(a.cap * 2_000_000):
                state["stopped"] = f"cap reached: {spend.used()} points read, {tokens} tokens"
                _write(res_p, state, task)
                print(state["stopped"])
                return
            chunk = todo[i:i + a.batch]
            ids = {f"t{j + 1:02d}": c for j, c in enumerate(chunk)}
            tests = "\n\n".join(f"### test {tid}\n```python\n{NC._test_source(task, c)}\n```" for tid, c in ids.items())
            user = f"{NC.JUDGE_RULES}\n\n## Design documents\n\n{docs}\n\n## Tests\n\n{tests}"
            try:
                text, usage = NC._call(a.model, NC.JUDGE_SYSTEM, user)
            except Exception as exc:  # noqa: BLE001 -- stop at the first failure (the proxy's failure counter)
                detail = exc.read().decode(errors="replace")[:300] if hasattr(exc, "read") else ""
                state["stopped"] = f"call failure: {type(exc).__name__} {detail[:200]}"
                _write(res_p, state, task)
                print(task, state["stopped"])
                return
            tokens += int(usage.get("total_tokens") or 0)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            (SEALED_LOG / f"{stamp}_{task}_{i}.json").write_text(json.dumps({"ids": ids, "prompt": user, "reply": text, "usage": usage}))
            m = re.search(r"\{.*\}", text, re.S)
            try:
                verdicts = json.loads(m.group(0)) if m else {}
            except ValueError:
                verdicts = {}
            got = {hid(ids[t]): str(v).strip().upper()[:1] for t, v in verdicts.items() if t in ids and str(v).strip().upper()[:1] in "ABCU"}
            state["labels"].setdefault(task, {}).update(got)
            state["calls"].append({"task": task, "n": len(chunk), "answered": len(got), "tokens": int(usage.get("total_tokens") or 0)})
            state["tokens"] = tokens
            state["spend_points_read"] = spend.used()
            _write(res_p, state, task)
            print(task, f"{len(got)}/{len(chunk)}", usage.get("total_tokens"), "points", spend.used(), flush=True)
    state["stopped"] = state.get("stopped") or "done"
    state["window_after"] = spend.last
    _write(res_p, state, "nl2_cerberus")
    print("judge done", len(state["calls"]), "calls", tokens, "tokens", spend.used(), "points")


def cmd_summary(_a) -> None:
    judge = json.loads((OUT / "judge.json").read_text()).get("labels", {}) if (OUT / "judge.json").is_file() else {}
    rows = []
    for f in sorted(OUT.glob("nl2_*.json")):
        lab = json.loads(f.read_text())
        task = f.stem
        s = Counter(lab["labels"].values())
        n = sum(s.values())
        jl = judge.get(task, {})
        jc = Counter(jl.values())
        jn = sum(jc.values())
        pairs = [(lab["labels"].get(c, "U"), v) for c, v in jl.items()]
        rows.append({
            "task": task, "cases": lab["cases"], "reference_lines": lab["reference_lines"], "docs_chars": lab["docs_chars"],
            "docs_chars_per_line": lab["docs_chars_per_line"],
            "script": {k: round(s[k] / n, 4) for k in "ABCU"} if n else {}, "script_counts": dict(s),
            "judged": jn, "judge": {k: round(jc[k] / jn, 4) for k in "ABCU"} if jn else {}, "judge_counts": dict(jc),
            "agree_three_level": round(sum(x == y for x, y in pairs) / len(pairs), 4) if pairs else None,
            "agree_a_vs_not": round(sum((x == "A") == (y == "A") for x, y in pairs) / len(pairs), 4) if pairs else None,
            "confusion": dict(Counter(f"{x}->{y}" for x, y in pairs)),
        })
    rows.sort(key=lambda r: -(r["judge"].get("A") or 0))
    text = json.dumps(rows, indent=1)
    for r in rows:
        G.assert_no_source_overlap(json.dumps(r), r["task"])
    (OUT / "summary.json").write_text(text)
    print(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    lb = sub.add_parser("labels")
    lb.add_argument("tasks", nargs="+")
    lb.set_defaults(fn=cmd_labels)
    j = sub.add_parser("judge")
    j.add_argument("--cap", type=float, default=2.0)
    j.add_argument("--batch", type=int, default=10)
    j.add_argument("--model", default="gpt-5.6-luna")
    j.set_defaults(fn=cmd_judge)
    sub.add_parser("summary").set_defaults(fn=cmd_summary)
    a = ap.parse_args()
    a.fn(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
