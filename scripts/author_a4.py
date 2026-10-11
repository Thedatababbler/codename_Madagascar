#!/usr/bin/env python3
"""A4 (author fake-object fix): re-author 6 training milestones twice each, sandboxed author runs, capped spend.

Generation is ``author_eval.py generate --group A4`` (the author layer: inventory, audit with
the fake + interface rules, fix rounds; the new fixed rules; ``ADAMAS_SANDBOX_ENFORCED=1``
for these runs only, the global switch untouched), samples 0 and 1, stopped when the quota
window has moved by ``--cap`` points.

Analysis (no quota), against the historical gate suites of the same milestones (label A6,
a subset of A):
- reference-fail rate of the hard suite, miss rate and false positives by memory replay
  (``author_eval.py score/metrics``, held-out via the sealed matrix);
- audit violations under the full audit (fake + interface), case count, citation rate;
- the sandbox's after-the-fact audit of every A4 author run.

    uv run python scripts/author_a4.py run --cap 4
    uv run python scripts/author_a4.py analyze
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

EVAL = ROOT / "outputs" / "author_eval"
OUT = ROOT / "outputs" / "author_fix" / "a4"
MILESTONES = [
    ("cookiecutter", "template_source_resolution"),
    ("imapclient", "transport_session_and_authentication"),
    ("imapclient", "mailbox_message_and_extension_operations"),
    ("python-hl7", "hierarchical_containers"),
    ("python-hl7", "mllp_transport"),
    ("djangorestframework-simplejwt", "request_authentication"),
]
SAMPLES = (0, 1)


from quota_spend import Spend  # noqa: E402


def _generate(sample: int, sel: Path, jobs: int, spend: Spend, cap: float) -> tuple[int | None, str]:
    cmd = [sys.executable, str(ROOT / "scripts" / "author_eval.py"), "generate", "--label", "A4", "--group", "A4",
           "--sample", str(sample), "--select-file", str(sel), "--jobs", str(jobs)]
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=open(OUT / f"generate.s{sample}.log", "a"), stderr=subprocess.STDOUT,
                         start_new_session=True)
    stop = ""
    while p.poll() is None:
        time.sleep(60)
        if spend.poll() >= cap:
            stop = f"budget cap {cap} reached during sample {sample} (used {spend.used()})"
            os.killpg(p.pid, signal.SIGTERM)
            break
    try:
        p.wait(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
    return p.returncode, stop


def run(cap: float, *, spent_before: float = 0.0, wait_pid: int | None = None, jobs: int = 2) -> dict:
    """Samples 0 and 1; a sample already partly generated is completed (missing suites only)."""
    OUT.mkdir(parents=True, exist_ok=True)
    sel = OUT / "milestones.txt"
    sel.write_text("".join(f"{t}:{m}\n" for t, m in MILESTONES))
    spend = Spend(spent_before)
    stop, rcs = "", {}
    while wait_pid and Path(f"/proc/{wait_pid}").exists():      # a generation started by an earlier driver
        time.sleep(30)
        if spend.poll() >= cap:
            os.kill(wait_pid, signal.SIGTERM)
            stop = f"budget cap {cap} reached while waiting for pid {wait_pid}"
            break
    for s in SAMPLES:
        if stop or spend.poll() >= cap:
            stop = stop or f"budget cap {cap} reached before sample {s}"
            break
        rcs[s], stop = _generate(s, sel, jobs, spend, cap)
    spend.poll()
    rec = {"window_start": spend.w0 if not spend.resets else None, "window_end": spend.last, "resets": spend.resets,
           "spent_before": spent_before, "points_used": spend.used(), "cap": cap, "stop": stop, "returncodes": rcs}
    (OUT / "spend.json").write_text(json.dumps(rec, indent=1))
    return rec


def _subset_a6() -> None:
    """A6: the historical gate suites (A, sample 0) of the 6 milestones, records reused (no re-run)."""
    keep = set(MILESTONES)
    for name in ("suites.s0.json", "records.s0.json"):
        rows = json.loads((EVAL / "A" / name).read_text())
        (EVAL / "A6").mkdir(parents=True, exist_ok=True)
        (EVAL / "A6" / name).write_text(json.dumps([r for r in rows if (r["task"], r["milestone"]) in keep], indent=1))


def _full_audit(suite: Path, task: str, mid: str) -> dict:
    from author_eval import DATASET_ROOT, PLANS, _packages
    from orchestra.codeprojecteval.public_symbols import load_docs
    from orchestra.codeprojecteval.suite_audit import audit_suite
    from orchestra.control.author.node_review import lenient_symbols

    docs = load_docs(DATASET_ROOT / task / "docs")
    plan = json.loads((PLANS / f"{task}.plan.json").read_text())
    focus = next((m.get("focus_paths") or [] for m in plan["milestones"] if m["milestone_id"] == mid), [])
    a = audit_suite(suite, docs, packages=_packages(task), allowed_symbols=lenient_symbols(docs, str(PLANS / f"{task}.plan.json"), mid, focus),
                    fake_object_audit=True, interface=True)
    return {"cases": a.cases, "cited_ratio": a.cited_ratio, "violations": len(a.violations), "by_rule": a.by_rule(),
            "signatureless_cases": len(set(a.signatureless_cases))}


def analyze() -> dict:
    from orchestra.sandbox.audit import audit_run

    _subset_a6()
    env = {k: v for k, v in os.environ.items() if k != "ADAMAS_SANDBOX_ENFORCED"}   # scoring as A was scored
    labels = [("A6", 0)] + [("A4", s) for s in SAMPLES if (EVAL / "A4" / f"suites.s{s}.json").is_file()]
    for lab, s in labels:
        if lab != "A6":
            subprocess.run([sys.executable, str(ROOT / "scripts" / "author_eval.py"), "score", "--label", lab, "--sample", str(s)],
                           cwd=str(ROOT), env=env, check=True)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "author_eval.py"), "metrics", "--label", lab, "--sample", str(s)],
                       cwd=str(ROOT), env=env, check=True)
    res: dict = {"labels": {}}
    for lab, s in labels:
        suites = json.loads((EVAL / lab / f"suites.s{s}.json").read_text())
        metrics = json.loads((EVAL / lab / f"metrics.s{s}.json").read_text())
        per = []
        for e in suites:
            if not e.get("suite_dir"):
                per.append({"task": e["task"], "milestone": e["milestone"], "suite": "MISSING", "note": e.get("note")})
                continue
            row = {"task": e["task"], "milestone": e["milestone"], **_full_audit(Path(e["suite_dir"]), e["task"], e["milestone"])}
            if lab == "A4":
                rd = ROOT / "outputs" / "author_probe" / e["run_id"] / e["task"]
                au = audit_run(rd)
                man = rd / "sandbox_manifest.json"
                row.update({"sandbox_manifest": man.is_file(), "sandbox_commands": au["commands"], "sandbox_findings": len(au["findings"]),
                            "review_met": (e.get("review") or {}).get("met"), "fix_calls": (e.get("review") or {}).get("fix_calls"),
                            "prompt_tokens": e.get("prompt_tokens")})
            per.append(row)
        res["labels"][f"{lab}.s{s}"] = {"summary": metrics.get("summary"), "per_milestone": metrics.get("per_milestone") or metrics.get("rows"),
                                        "suites": per}
    sp = OUT / "spend.json"
    res["spend"] = json.loads(sp.read_text()) if sp.is_file() else None
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "analysis.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["run", "analyze"])
    ap.add_argument("--cap", type=float, default=4.0)
    ap.add_argument("--spent-before", type=float, default=0.0, help="points already spent by an earlier driver")
    ap.add_argument("--wait-pid", type=int, default=None, help="a generation an earlier driver left running")
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args()
    if a.what == "run":
        print(json.dumps(run(a.cap, spent_before=a.spent_before, wait_pid=a.wait_pid, jobs=a.jobs), indent=1))
    else:
        r = analyze()
        print(json.dumps({k: {"summary": v["summary"], "suites": v["suites"]} for k, v in r["labels"].items()}, indent=1, default=str)[:20000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
