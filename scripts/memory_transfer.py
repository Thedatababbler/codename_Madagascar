#!/usr/bin/env python3
"""Run the transfer channel once (memory spec §5): training-task repair records -> first-pass pitfalls.

Memory replay only (no implementer, repairer or author runs); model calls at
temperature 0 for generation, generality and merging, cached under
outputs/memory/transfer_cache. ``--collect-only`` counts the raw facts and
makes no call.

    uv run python scripts/memory_transfer.py [--collect-only]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestra.memory.store import load_config, memory_root  # noqa: E402
from orchestra.memory.transfer import collect_facts, run_transfer  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect-only", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    cfg = load_config()
    tr = cfg.get("transfer") or {}
    train, test = list(tr.get("train_tasks") or []), list(tr.get("test_tasks") or [])
    if a.collect_only:
        facts, raw = collect_facts(train)
        by = {}
        for f in facts:
            by.setdefault(f.task, [0, 0])
            by[f.task][0] += 1
            by[f.task][1] += bool(f.error_output)
        print(json.dumps({"raw": raw, "unique": len(facts), "by_task [unique, with error output]": by}, indent=1))
        return 0
    from orchestra.control.evolution.evolver import default_call
    from orchestra.settings import load_env_file

    load_env_file(ROOT / ".env")

    stats = run_transfer(root=memory_root(cfg), train_tasks=train, test_tasks=test, call=default_call, workers=a.workers)
    print(json.dumps(stats, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
