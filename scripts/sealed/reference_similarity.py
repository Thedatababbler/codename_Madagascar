#!/usr/bin/env python3
"""Similarity of every stored final repository to its task's reference implementation (sandbox spec A, S5 part 2).

For each run's canonical repository (``tasks/rb_*/canonical/repo``) of a task with
a reference, the non-test Python sources of both sides are tokenised (comments,
docstrings and whitespace dropped, names kept) and compared as sets of token
5-grams (Jaccard, weighted by the reference). One number per repository leaves
this script; no code, path of the reference or token does.

Outliers per task (non-empty repositories only): above min(Q3 + 3 * IQR, 1.8 * median, 0.6).

    uv run python scripts/sealed/reference_similarity.py [--out outputs/sandbox/s5b.json]
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import statistics
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import reference_root, task_config  # noqa: E402

N = 5
_SKIP = ("test", "tests", "unit_tests", "check_tests", "spec_tests", "spec_tests_soft", "repair_evidence", "docs", ".git",
         "__pycache__", "bin", "lib", "lib64", "include", "share", ".venv", "venv", "build", "dist", "examples")


def tokens(src: str) -> list[str]:
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out
    prev = None
    for t in toks:
        if t.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
                      tokenize.ENDMARKER):
            continue
        if t.type == tokenize.STRING and (prev is None or prev in (":", "\n")):
            prev = "str"
            continue                                   # docstrings and bare strings
        out.append("S" if t.type == tokenize.STRING else t.string)
        prev = t.string
    return out


def grams(root: Path, package_dirs: list[str] | None = None) -> set[tuple]:
    g: set[tuple] = set()
    for p in root.rglob("*.py"):
        rel = p.relative_to(root).parts
        if any(part in _SKIP or part.startswith("test_") for part in rel) or p.name.startswith("test_") or p.name == "conftest.py":
            continue
        if package_dirs and rel[0] not in package_dirs and not (rel[0] == "src" and len(rel) > 1 and rel[1] in package_dirs):
            continue
        try:
            t = tokens(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        g |= {tuple(t[i:i + N]) for i in range(max(0, len(t) - N + 1))}
    return g


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "outputs" / "sandbox" / "s5b.json"))
    a = ap.parse_args()
    repos: dict[str, list[Path]] = {}
    for d in glob.glob(str(ROOT / "outputs" / "**" / "tasks" / "rb_*" / "canonical" / "repo"), recursive=True):
        task = Path(d).parent.parent.name.removeprefix("rb_")
        repos.setdefault(task, []).append(Path(d))
    result: dict[str, dict] = {}
    for task, paths in sorted(repos.items()):
        try:
            cfg = task_config(task)
            ref = reference_root(task)
        except Exception:  # noqa: BLE001 -- no reference for this task
            continue
        pkg = str(cfg.get("source_code") or task).split("/")[0]
        ref_g = grams(ref, [pkg])
        if not ref_g:
            continue
        sims = {}
        for p in sorted(paths):
            g = grams(p, [pkg])
            sims[str(p.relative_to(ROOT))] = round(len(g & ref_g) / len(ref_g), 4) if g else 0.0
        vals = sorted(sims.values())
        nz = [v for v in vals if v > 0]                     # empty / failed repositories carry no signal
        if len(nz) >= 4:
            qs = statistics.quantiles(nz, n=4)
            q1, q3 = qs[0], qs[2]
        else:
            q1, q3 = (min(nz), max(nz)) if nz else (0.0, 0.0)
        med = statistics.median(nz) if nz else 0.0
        cut = min(q3 + 3 * (q3 - q1), 0.6) if len(nz) >= 4 else 0.6
        cut = min(cut, 1.8 * med) if med and len(nz) >= 3 else cut
        result[task] = {"n": len(vals), "n_nonzero": len(nz), "median_nonzero": round(med, 4), "q1": round(q1, 4),
                        "q3": round(q3, 4), "max": vals[-1], "outlier_cut": round(cut, 4),
                        "outliers": {k: v for k, v in sims.items() if v > cut}, "repos": sims}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    for t, r in result.items():
        print(f"{t:32s} n={r['n']:3d} nonzero={r['n_nonzero']:3d} median={r['median_nonzero']:.3f} q3={r['q3']:.3f} "
              f"max={r['max']:.3f} cut={r['outlier_cut']:.3f} outliers={len(r['outliers'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
