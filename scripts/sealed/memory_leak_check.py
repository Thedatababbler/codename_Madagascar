#!/usr/bin/env python3
"""Substring leak check for memory entries (memory spec §2.3 items 1-2).

Input: a JSON file ``{entry_id: text}``. Every 20-character window of each text
(whitespace normalised) is looked up in the held-out sources of every task in
both dataset roots, and in the verification suites named by the manifests in
configs/memory.yaml. Output, one line per entry and nothing else:

    <entry_id> heldout=PASS|FAIL verification=PASS|FAIL

A window found in the sources of 3 or more distinct tasks is common phrasing
(user decision): it is exempt, and listed (the memory text's own window, the
entry id and the number of tasks; no source) for the caller's log.

With ``--out`` the same verdicts are written as JSON. No held-out text, path or
case id leaves this script.

    uv run python scripts/sealed/memory_leak_check.py --entries <json> [--out <json>] [--tasks a,b]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402
from _guard import DATASET_ROOT, NL2_DATASET_ROOT, SHINGLE, _windows, heldout_dir  # noqa: E402


def all_tasks() -> list[str]:
    out = []
    for root in (DATASET_ROOT, NL2_DATASET_ROOT):
        if root.is_dir():
            out += sorted(p.name for p in root.iterdir() if (p / "config.json").is_file())
    return out


def verification_dirs() -> list[tuple[str, Path]]:
    """(task, suite dir) of every verification suite named by the manifests."""
    cfg = yaml.safe_load((ROOT / "configs" / "memory.yaml").read_text(encoding="utf-8")) or {}
    out: set[tuple[str, Path]] = set()
    for rel in ((cfg.get("transfer") or {}).get("verification_manifests") or []):
        p = ROOT / rel
        if not p.is_file():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            items = [(str(k).split(":", 1)[0], v) for k, v in data.items()]
        else:
            items = [(str(d.get("task") or ""), d.get("suite_dir")) for d in data if isinstance(d, dict)]
        out |= {(t, Path(str(d))) for t, d in items if d and Path(str(d)).is_dir()}
    return sorted(out)


GENERIC_TASKS = 3     # a window found in this many distinct tasks' sources is common phrasing (user decision)


def check(entries: dict[str, str], *, tasks: list[str] | None = None, n: int = SHINGLE,
          extra_heldout: list[Path] | None = None, generic_tasks: int = GENERIC_TASKS) -> dict:
    """Per entry PASS/FAIL against held-out and verification sources.

    A window that occurs in the held-out or verification sources of at least
    ``generic_tasks`` distinct tasks is common phrasing, not a leak: it does not
    count as a hit, and it is listed under ``_exemptions`` (entry, window,
    number of tasks) for the caller's log. ``extra_heldout``: directories
    treated as held-out of one extra pseudo-task each (the unit tests' fake samples).
    """
    windows: dict[str, set[str]] = {}
    for eid, text in entries.items():
        for w in _windows(str(text or ""), n):
            windows.setdefault(w, set()).add(eid)
    seen: dict[str, dict[str, set[str]]] = {"heldout": {}, "verification": {}}   # kind -> window -> tasks

    def scan(files, kind, task):
        for f in files:
            try:
                src = " ".join(f.read_text(encoding="utf-8", errors="replace").split())
            except OSError:
                continue
            for i in range(0, max(0, len(src) - n + 1)):
                w = src[i : i + n]
                if w in windows:
                    seen[kind].setdefault(w, set()).add(task)

    for t in tasks or all_tasks():
        try:
            d = heldout_dir(t)
        except (OSError, ValueError, KeyError):
            continue
        if d.is_dir():
            scan(d.rglob("*.py"), "heldout", t)
    for i, d in enumerate(extra_heldout or []):
        scan(Path(d).rglob("*.py"), "heldout", f"__extra{i}")
    for t, d in verification_dirs():
        scan(d.rglob("*.py"), "verification", t or str(d))
    res: dict = {}
    exemptions = []
    for eid in entries:
        res[eid] = {}
    for w, ids in windows.items():
        all_tasks_w = seen["heldout"].get(w, set()) | seen["verification"].get(w, set())
        generic = len(all_tasks_w) >= generic_tasks
        for eid in ids:
            for kind in ("heldout", "verification"):
                if w in seen[kind]:
                    if generic:
                        exemptions.append({"entry": eid, "window": w, "tasks": len(all_tasks_w), "kind": kind})
                    else:
                        res[eid][kind] = "FAIL"
    for eid in entries:
        for kind in ("heldout", "verification"):
            res[eid].setdefault(kind, "PASS")
    uniq = {(e["entry"], e["window"]): e for e in exemptions}
    res["_exemptions"] = sorted(uniq.values(), key=lambda e: (e["entry"], e["window"]))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", required=True)
    ap.add_argument("--out")
    ap.add_argument("--tasks", default="")
    ap.add_argument("--extra-heldout", action="append", default=[])
    a = ap.parse_args()
    entries = json.loads(Path(a.entries).read_text(encoding="utf-8"))
    res = check(entries, tasks=[t for t in a.tasks.split(",") if t] or None, extra_heldout=[Path(p) for p in a.extra_heldout])
    for eid, r in res.items():
        if eid == "_exemptions":
            continue
        print(f"{eid} heldout={r['heldout']} verification={r['verification']}")
    print(f"exempted windows (common phrasing, >= {GENERIC_TASKS} tasks): {len(res['_exemptions'])}")
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
