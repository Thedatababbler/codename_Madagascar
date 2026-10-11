#!/usr/bin/env python3
"""Similarity of every stored final repository to every original-project copy the sweep found (2026-10-11, item 1.3).

Copies come from ``copy_sweep.py``'s summary: package directories and archives
at least 0.65 similar to the reference, and every directory holding verbatim
reference files, outside agent-output trees (AdaMAS and baseline run outputs,
builds of agent repositories): agent-written code is audited, not used as a copy. For each task, each run's final
repository (``tasks/rb_*/canonical/repo``) is compared with each copy by the
same token 5-gram measure as ``reference_similarity.py`` (share of the copy's
5-grams found in the run). Per (task, copy) the outlier rule is the same:
above min(Q3 + 3 * IQR, 1.8 * median, 0.6) over the task's non-empty runs.

Outputs one number per (run, copy) and the outliers; no source text.

    uv run python scripts/sealed/copy_similarity.py [--sweep outputs/sandbox/copy_sweep] [--out .../copy_similarity.json]
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import task_config  # noqa: E402
from copy_sweep import archive_members, grams_from_texts  # noqa: E402
from reference_similarity import grams  # noqa: E402


def pkg_of(task: str) -> str:
    return str(task_config(task).get("source_code") or task).split("/")[-1]


AGENT_OUTPUT = (str(ROOT / "outputs") + "/", "/root/projects/mas-baselines/outputs", "/tmp/aiofiles-")
MIN_COPY_SIM = 0.65   # agent-written code tops out near 0.6 against the reference; upstream copies sit at 0.66-1.0


def _is_copy(path: str, similarity: float) -> bool:
    return similarity >= MIN_COPY_SIM and not path.startswith(AGENT_OUTPUT)


def copies(sweep: Path) -> dict[str, list[tuple[str, set]]]:
    """task -> [(copy label, 5-grams)]: original-project copies, not agent output (``_is_copy``)."""
    s = json.loads((sweep / "summary.json").read_text(encoding="utf-8"))
    out: dict[str, list[tuple[str, set]]] = {}
    seen: set[tuple[str, str]] = set()
    for pd in s["package_dirs"]:
        if _is_copy(pd["path"], pd["similarity"]) and (pd["task"], pd["path"]) not in seen:
            seen.add((pd["task"], pd["path"]))
            p = Path(pd["path"])
            out.setdefault(pd["task"], []).append((f"dir:{p}", grams(p.parent, [p.name])))
    for ar in s["archives"]:
        if _is_copy(ar["path"], ar["similarity"]) and (ar["task"], ar["path"]) not in seen:
            seen.add((ar["task"], ar["path"]))
            pref = ar["package_prefix"]
            mem = archive_members(Path(ar["path"])) or []
            texts = [b.decode("utf-8", "replace") for nm, b in mem if nm.startswith(pref)
                     and not re.search(r"(^|/)(tests?|testing)/|/test_[^/]*$", nm[len(pref):])]
            out.setdefault(ar["task"], []).append((f"archive:{ar['path']}", grams_from_texts(texts)))
    for loc in s["exact_locations"]:
        for key in loc["matches"]:
            task, kind = key.rsplit(":", 1)
            if kind != "reference" or (task, loc["dir"]) in seen or loc["dir"].startswith(AGENT_OUTPUT):
                continue
            seen.add((task, loc["dir"]))
            d = Path(loc["dir"])
            # the directory holding verbatim reference files: walk up to the package directory if it is named so
            pkg = pkg_of(task)
            while d.name != pkg and d.parent != d and pkg in d.parts:
                d = d.parent
            if d.name == pkg and not any(lbl == f"dir:{d}" for lbl, _ in out.get(task, [])):
                out.setdefault(task, []).append((f"dir:{d}", grams(d.parent, [pkg])))
    return {t: [(lbl, g) for lbl, g in v if g] for t, v in out.items()}


def cut_of(vals: list[float]) -> tuple[float, float]:
    nz = sorted(v for v in vals if v > 0)
    if not nz:
        return 0.6, 0.0
    med = statistics.median(nz)
    if len(nz) >= 4:
        q = statistics.quantiles(nz, n=4)
        cut = min(q[2] + 3 * (q[2] - q[0]), 0.6)
    else:
        cut = 0.6
    if len(nz) >= 3:
        cut = min(cut, 1.8 * med)
    return round(cut, 4), round(med, 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", default=str(ROOT / "outputs" / "sandbox" / "copy_sweep"))
    ap.add_argument("--out", default=str(ROOT / "outputs" / "sandbox" / "copy_sweep" / "copy_similarity.json"))
    a = ap.parse_args()
    cps = copies(Path(a.sweep))
    repos: dict[str, list[Path]] = {}
    for d in glob.glob(str(ROOT / "outputs" / "**" / "tasks" / "rb_*" / "canonical" / "repo"), recursive=True):
        repos.setdefault(Path(d).parent.parent.name.removeprefix("rb_"), []).append(Path(d))
    result: dict = {}
    for task, cl in sorted(cps.items()):
        pkg = pkg_of(task)
        run_g = {str(p.relative_to(ROOT)): grams(p, [pkg]) for p in sorted(repos.get(task, []))}
        per_copy = {}
        for lbl, cg in cl:
            sims = {r: (round(len(g & cg) / len(cg), 4) if g else 0.0) for r, g in run_g.items()}
            cut, med = cut_of(list(sims.values()))
            per_copy[lbl] = {"copy_grams": len(cg), "median_nonzero": med, "cut": cut, "max": max(sims.values(), default=0.0),
                             "outliers": {r: v for r, v in sims.items() if v > cut}, "runs": sims}
        result[task] = per_copy
        flagged = sorted({r for c in per_copy.values() for r in c["outliers"]})
        print(f"{task:30s} copies={len(cl):3d} runs={len(run_g):3d} flagged_runs={len(flagged)}")
        for r in flagged:
            best = max(((lbl, c["runs"][r]) for lbl, c in per_copy.items() if r in c["outliers"]), key=lambda x: x[1])
            print(f"    {r}  max {best[1]} vs {best[0]}")
    Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
