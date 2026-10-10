#!/usr/bin/env python3
"""Re-check entries the strict substring rule rejected, under the common-phrasing exemption (2026-10-10).

- migrated repair rows rejected at migration: re-validated; those that pass are
  added back to memory/repair/patterns.yaml (version bump) and marked resolved
  in transfer/rejected.yaml;
- pending new-category pitfalls: re-validated and counted only; they still
  wait for people to review the category catalogue.

Exempted windows are logged to memory/transfer/leak_exemptions.yaml.

    uv run python scripts/memory_recheck.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orchestra.memory.store import commit_write, load_config, memory_root, read_yaml_list, write_yaml  # noqa: E402
from orchestra.memory.validate import build_identifiers, load_sources, validate_entries  # noqa: E402


def main() -> int:
    cfg = load_config()
    root = memory_root(cfg)
    train, test = cfg["transfer"]["train_tasks"], cfg["transfer"]["test_tasks"]
    ids = build_identifiers(train)
    tags = __import__("yaml").safe_load((root / "domain_tags.yaml").read_text())["tags"]
    report: dict = {}

    rejected = read_yaml_list(root / "transfer" / "rejected.yaml")
    mig = [r for r in rejected if r.get("stage") == "migration" and r.get("bank") == "repair" and not r.get("resolved")]
    rows = [r["entry"] for r in mig]
    cats = [c["category_id"] for c in read_yaml_list(root / "repair" / "categories.yaml")]
    v = validate_entries("repair", "patterns", rows, category_ids=cats, domain_tags=tags, identifiers=ids, sources={}, test_tasks=test)
    passed = [r for r, x in zip(rows, v, strict=True) if x.ok]
    report["migrated_repair_rows"] = {x.entry_id: (x.ok, x.reasons) for x in v}
    if passed:
        pats = read_yaml_list(root / "repair" / "patterns.yaml")
        have = {p["pattern_id"] for p in pats}
        add = [p for p in passed if p["pattern_id"] not in have]
        if add:
            ver = commit_write(root, "repair", "patterns", pats + add, origin="recheck:2026-10-10",
                               reason="migrated rows re-validated under the common-phrasing exemption",
                               changed_ids=[p["pattern_id"] for p in add])
            report["memory_version"] = ver
        ok_ids = {p["pattern_id"] for p in passed}
        for r in rejected:
            if r in mig and r["entry"]["pattern_id"] in ok_ids:
                r["resolved"] = "2026-10-10: passes under the common-phrasing exemption; added back"
        write_yaml(root / "transfer" / "rejected.yaml", rejected)

    pending = read_yaml_list(root / "transfer" / "pending_pitfalls.yaml")
    fp_cats = [c["category_id"] for c in read_yaml_list(root / "first_pass" / "categories.yaml")]
    probes = [{**p, "pitfall_id": f"pend-{i}", "category_id": fp_cats[0]} for i, p in enumerate(pending)]
    v = validate_entries("first_pass", "pitfalls", probes, category_ids=fp_cats, domain_tags=tags, identifiers=ids,
                         sources=load_sources(root), test_tasks=test)
    report["pending_new_category_pitfalls"] = {"total": len(v), "pass": sum(x.ok for x in v),
                                               "still_failing": {x.entry_id: x.reasons for x in v if not x.ok}}
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
