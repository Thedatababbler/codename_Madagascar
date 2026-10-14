#!/usr/bin/env python3
"""Similarity of the clean-baseline batch and its old counterparts to the reference (numbers only; 2026-10-14).

Same measure as reference_similarity.py (token 5-grams of the package, share of the reference's 5-grams found).
    uv run python scripts/sealed/clean_baseline_similarity.py <task>=<batch>[,<batch>...] ...
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _guard import reference_root, task_config  # noqa: E402
from reference_similarity import grams  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
out = {}
for arg in sys.argv[1:]:
    task, batches = arg.split("=", 1)
    pkg = str(task_config(task).get("source_code") or task).split("/")[0]
    ref = grams(reference_root(task), [pkg])
    for b in batches.split(","):
        repo = next((ROOT / b / task / "tasks").glob("rb_*/canonical/repo"))
        g = grams(repo, [pkg])
        out[f"{task}|{b}"] = round(len(g & ref) / len(ref), 4) if g and ref else 0.0
print(json.dumps(out, indent=1))
