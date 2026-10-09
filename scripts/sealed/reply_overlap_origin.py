#!/usr/bin/env python3
"""Where a reply's held-out overlap comes from: counts only (no text, no path of the held-out suite).

Each 20-character window of the reply that occurs in the task's held-out sources is
looked up in the public material the agent had (the task documents and, if given,
the workspace it worked in). Output: {"overlapping_windows": n, "also_in_docs": a,
"also_in_workspace": b, "only_in_heldout": c}.

    uv run python scripts/sealed/reply_overlap_origin.py --task imapclient --reply <file> [--workspace <dir>]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import SHINGLE, _windows, dataset_root_for, heldout_source_windows  # noqa: E402


def _text_windows(paths) -> set[str]:
    out: set[str] = set()
    for p in paths:
        try:
            out |= _windows(p.read_text(encoding="utf-8", errors="replace"), SHINGLE)
        except OSError:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--reply", required=True, type=Path)
    ap.add_argument("--workspace", type=Path)
    a = ap.parse_args()
    reply = _windows(a.reply.read_text(encoding="utf-8", errors="replace"), SHINGLE)
    over = reply & heldout_source_windows(a.task)
    docs = _text_windows((dataset_root_for(a.task) / a.task / "docs").rglob("*"))
    ws = _text_windows(p for p in a.workspace.rglob("*") if p.is_file() and ".git" not in p.parts and p.stat().st_size < 2_000_000) if a.workspace else set()
    print(json.dumps({"overlapping_windows": len(over), "also_in_docs": len(over & docs), "also_in_workspace": len(over & ws),
                      "only_in_heldout": len(over - docs - ws)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
