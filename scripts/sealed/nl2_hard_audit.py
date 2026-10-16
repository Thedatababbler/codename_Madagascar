#!/usr/bin/env python3
"""Per-run audit for the NL2Repo hard-level comparison (sealed): numbers only.

- similarity of the run's final repository to the reference (token 5-gram Jaccard weighted by the reference, the
  measure of reference_similarity.py) and to the sealed upstream clone's package (the only other copy on the machine);
- the held-out conftest hash now and the upstream original's (before the blind collection fix), identical for all arms.

    uv run python scripts/sealed/nl2_hard_audit.py <run_dir> [<run_dir> ...]
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard as G  # noqa: E402
from reference_similarity import grams  # noqa: E402

UPSTREAM = Path("/root/.adamas_sealed/work/upstream")


def _sim(a: set, ref: set) -> float:
    return round(len(a & ref) / len(ref), 4) if ref else 0.0


def _sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.is_file() else None


def audit(run_dir: Path) -> dict:
    task = run_dir.name
    cfg = G.task_config(task)
    src = str(cfg.get("source_code"))
    repo = next(iter(run_dir.glob("tasks/*/canonical/repo")), None)
    ref_root = G.reference_root(task)
    ref = grams(ref_root, [src])
    up_name = (cfg.get("nl2repo") or {}).get("task") or task.removeprefix("nl2_")
    up = UPSTREAM / up_name
    out = {"run_dir": str(run_dir), "task": task}
    if repo is not None:
        mine = grams(repo, [src])
        out["similarity_to_reference"] = _sim(mine, ref)
        if up.is_dir():
            upg = grams(up, [src])
            out["similarity_to_upstream_clone"] = _sim(mine, upg)
    tests_rel = (cfg.get("nl2repo") or {}).get("tests_dir") or "tests"
    out["heldout_conftest_sha256_now"] = _sha(G.heldout_dir(task) / "conftest.py")
    out["heldout_conftest_sha256_upstream_original"] = _sha(up / tests_rel / "conftest.py")
    return out


def main() -> int:
    rows = [audit(Path(p)) for p in sys.argv[1:]]
    text = json.dumps(rows, indent=1)
    for r in rows:
        G.assert_no_source_overlap(json.dumps(r), r["task"])
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
