#!/usr/bin/env python3
"""Substring leak check for memory entries (memory spec §2.3 items 1-2).

Input: a JSON file ``{entry_id: text}``. Every 20-character window of each text
(whitespace normalised) is looked up in the held-out sources of every task in
both dataset roots, and in the verification suites named by the manifests in
configs/memory.yaml. Output, one line per entry and nothing else:

    <entry_id> heldout=PASS|FAIL verification=PASS|FAIL

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


def verification_dirs() -> list[Path]:
    cfg = yaml.safe_load((ROOT / "configs" / "memory.yaml").read_text(encoding="utf-8")) or {}
    dirs: list[Path] = []
    for rel in ((cfg.get("transfer") or {}).get("verification_manifests") or []):
        p = ROOT / rel
        if not p.is_file():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.values() if isinstance(data, dict) else [d.get("suite_dir") for d in data if isinstance(d, dict)]
        dirs += [Path(str(d)) for d in items if d and Path(str(d)).is_dir()]
    return sorted(set(dirs))


def check(entries: dict[str, str], *, tasks: list[str] | None = None, n: int = SHINGLE,
          extra_heldout: list[Path] | None = None) -> dict[str, dict[str, str]]:
    """``extra_heldout``: directories treated as held-out too (the unit tests' fake samples)."""
    windows: dict[str, set[str]] = {}
    for eid, text in entries.items():
        for w in _windows(str(text or ""), n):
            windows.setdefault(w, set()).add(eid)
    bad = {"heldout": set(), "verification": set()}

    def scan(files, kind):
        for f in files:
            try:
                src = " ".join(f.read_text(encoding="utf-8", errors="replace").split())
            except OSError:
                continue
            for i in range(0, max(0, len(src) - n + 1)):
                ids = windows.get(src[i : i + n])
                if ids:
                    bad[kind] |= ids

    for t in tasks or all_tasks():
        try:
            d = heldout_dir(t)
        except (OSError, ValueError, KeyError):
            continue
        if d.is_dir():
            scan(d.rglob("*.py"), "heldout")
    for d in extra_heldout or []:
        scan(Path(d).rglob("*.py"), "heldout")
    for d in verification_dirs():
        scan(d.rglob("*.py"), "verification")
    return {eid: {k: ("FAIL" if eid in bad[k] else "PASS") for k in ("heldout", "verification")} for eid in entries}


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
        print(f"{eid} heldout={r['heldout']} verification={r['verification']}")
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
