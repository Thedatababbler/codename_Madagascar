#!/usr/bin/env python3
"""Record, then delete, the original-project copies the sweep found (2026-10-11, user's item 1.2).

Two groups:
1. ``--roots <json>``: directories (temp and scratch copies) removed whole;
2. own-environment originals: a task's own project installed in its own venv
   (found by import from an empty directory), removed from site-packages with
   its dist-info, editable finder and ``.pth``.

Before anything is removed, every file is recorded (path, size, mtime, sha256,
and whether it is byte-identical to a reference, held-out or check_tests file)
in ``<out>/deleted_record.json``. That record names held-out files by path, for
the people reviewing the cleanup; stdout gives counts and hashes only.

    uv run python scripts/sealed/copy_delete.py --roots outputs/sandbox/copy_sweep/delete_roots.json [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import task_config  # noqa: E402
from copy_sweep import fingerprints, sha256, tasks  # noqa: E402

ENV_ROOTS = {"cpe": Path("/root/codex-benchmarks/cpe_envs"), "nl2": Path("/root/codex-benchmarks/nl2repo/envs")}


def env_of(task: str) -> Path:
    return ENV_ROOTS["nl2" if task.startswith("nl2_") else "cpe"] / task


def own_env_originals() -> list[tuple[str, list[Path]]]:
    """(task, paths to remove) for every task whose own venv imports its own project."""
    out = []
    for t in tasks():
        pkg = str(task_config(t).get("source_code") or t).split("/")[-1]
        py = env_of(t) / "bin" / "python"
        if not py.exists():
            continue
        r = subprocess.run([str(py), "-c", f"import {pkg}, sys; print({pkg}.__file__)"], capture_output=True, text=True,
                           cwd="/tmp", env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
        if r.returncode != 0:
            continue
        site = next(env_of(t).glob("lib/python*/site-packages"))
        rm: list[Path] = []
        loc = Path(r.stdout.strip())
        if site in loc.parents:
            rm.append(loc.parent if loc.name == "__init__.py" else loc)
        norm = pkg.replace("-", "_").lower()
        dist = t.removeprefix("nl2_").replace("-", "_").lower()
        for p in site.iterdir():
            n = p.name.lower()
            if n.endswith((".dist-info", ".egg-info", ".egg-link", ".pth")) or n.startswith("__editable__"):
                stem = n.removeprefix("__editable__.").removeprefix("__editable___")
                if stem.startswith((norm, dist, dist.replace("pytorch_", ""))):
                    rm.append(p)
        out.append((t, sorted(set(rm))))
    return out


def record(paths: list[Path], by_hash: dict) -> tuple[list[dict], dict]:
    rows, agg = [], {"files": 0, "bytes": 0, "reference": 0, "heldout_only": 0, "check_tests": 0, "mtimes": []}
    for root in paths:
        files = [root] if root.is_file() or root.is_symlink() else [f for f in root.rglob("*") if f.is_file() and not f.is_symlink()]
        for f in files:
            try:
                st = f.lstat()
                h = sha256(f) if f.is_file() else ""
            except OSError:
                continue
            m = sorted(set(by_hash.get(h, [])))
            kinds: dict[str, set] = {}
            for t, k in m:
                kinds.setdefault(t, set()).add(k)
            ho = any("heldout" in ks and "check_tests" not in ks for ks in kinds.values())
            rows.append({"root": str(root), "path": str(f), "size": st.st_size,
                         "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)), "sha256": h,
                         "matches": [list(x) for x in m], "heldout_only": ho})
            agg["files"] += 1
            agg["bytes"] += st.st_size
            agg["mtimes"].append(rows[-1]["mtime"])
            agg["reference"] += any(k == "reference" for _, k in m)
            agg["check_tests"] += any(k == "check_tests" for _, k in m)
            agg["heldout_only"] += ho
    agg["mtime_min"], agg["mtime_max"] = (min(agg["mtimes"]), max(agg["mtimes"])) if agg["mtimes"] else ("", "")
    del agg["mtimes"]
    return rows, agg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", required=True)
    ap.add_argument("--out", default=str(ROOT / "outputs" / "sandbox" / "copy_sweep"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    by_hash, _, _, _ = fingerprints()
    roots = [Path(p) for p in json.loads(Path(a.roots).read_text()) if Path(p).exists()]
    own = own_env_originals()
    rec: dict = {"roots": [], "own_env": [], "dry_run": a.dry_run, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    summary: dict = {"roots": [], "own_env": []}
    for r in roots:
        rows, agg = record([r], by_hash)
        rec["roots"].append({"root": str(r), **agg, "file_rows": rows})
        # the cpe_probe_repos check_tests directories: held-out content called out separately (user's item 1.2)
        sub = {}
        if r.name == "cpe_probe_repos":
            for ct in sorted(r.glob("*/check_tests")):
                _, sagg = record([ct], by_hash)
                sub[str(ct)] = {"files": sagg["files"], "heldout_only": sagg["heldout_only"]}
        summary["roots"].append({"root": str(r), **agg, **({"check_tests_dirs": sub} if sub else {}),
                                 "heldout_only_hashes": sorted({x["sha256"][:16] for x in rows if x["heldout_only"]})[:50]})
    for t, paths in own:
        rows, agg = record(paths, by_hash)
        rec["own_env"].append({"task": t, "paths": [str(p) for p in paths], **agg, "file_rows": rows})
        summary["own_env"].append({"task": t, "paths": [str(p) for p in paths], **agg})
    out = Path(a.out)
    (out / ("deleted_record.dry.json" if a.dry_run else "deleted_record.json")).write_text(json.dumps(rec, indent=1))
    if not a.dry_run:
        for r in roots:
            shutil.rmtree(r, ignore_errors=True) if r.is_dir() else r.unlink(missing_ok=True)
        for _t, paths in own:
            for p in paths:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists() or p.is_symlink():
                    p.unlink()
        summary["remaining"] = [str(r) for r in roots if r.exists()] + [str(p) for _t, ps in own for p in ps if p.exists()]
    (out / ("deleted_summary.dry.json" if a.dry_run else "deleted_summary.json")).write_text(json.dumps(summary, indent=1))
    tot = {k: sum(x[k] for x in summary["roots"]) for k in ("files", "bytes", "reference", "heldout_only", "check_tests")}
    print(json.dumps({"roots": len(roots), **tot, "own_env": [{k: v for k, v in x.items() if k in ("task", "paths", "files", "reference", "heldout_only")}
                                                         for x in summary["own_env"]], "dry_run": a.dry_run,
                      "remaining": summary.get("remaining")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
