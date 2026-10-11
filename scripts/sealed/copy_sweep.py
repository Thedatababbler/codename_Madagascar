#!/usr/bin/env python3
"""Machine-wide sweep for copies of the tasks' original projects (2026-10-11, user's item 1).

Fingerprints, per task of both dataset roots:
- the reference package's files (sha256), and its token 5-grams (reference_similarity.grams);
- the held-out suite's files (``unit_tests``) and the visible ``check_tests`` files (sha256).

The walk covers the whole file system except /proc, /sys, /dev, /run and the
two dataset roots themselves, and finds:
1. ``exact``: any file byte-identical to a reference, held-out or check_tests file
   (files under 64 bytes are ignored: empty ``__init__.py`` and the like);
2. ``package_dir``: any directory named like a task package (with Python files),
   scored by 5-gram similarity to that task's reference;
3. ``archive``: wheels, sdists, zips, eggs and pip-cache bodies holding a task
   package (``<pkg>/__init__.py``), scored the same way;
4. ``git_clone``: a ``.git/config`` whose remote URL names a task project.

Outputs:
- ``<out>/manifest.json``: every location with its files (path, size, mtime,
  sha256). Held-out files appear there by path, for the people reviewing the
  sweep; this file is not for model context.
- ``<out>/summary.json`` and stdout: one row per location. A held-out match is
  shown as a count and the hashes only, never a file name.

    uv run python scripts/sealed/copy_sweep.py [--out outputs/sandbox/copy_sweep] [--skip-outputs]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _guard import DATASET_ROOT, NL2_DATASET_ROOT, heldout_dir, reference_root, task_config  # noqa: E402
from reference_similarity import N, grams, tokens  # noqa: E402

PRUNE = ("/proc", "/sys", "/dev", "/run", str(DATASET_ROOT), str(NL2_DATASET_ROOT))
MIN_EXACT = 64
ARCHIVE_MAX = 80 * 1024 * 1024
ARCHIVE_EXT = (".whl", ".zip", ".egg", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")
SIM_FLAG = 0.5        # a package directory or archive this similar to the reference is a copy


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tasks() -> list[str]:
    out = []
    for root in (DATASET_ROOT, NL2_DATASET_ROOT):
        if root.is_dir():
            out += sorted(p.name for p in root.iterdir() if (p / "config.json").is_file())
    return out


def project_names(task: str, pkg: str) -> set[str]:
    base = task.removeprefix("nl2_").lower()
    names = {base, base.replace("-", ""), base.replace("-", "_"), pkg.lower()}
    if base == "python-hl7":
        names.add("hl7")
    return {n for n in names if len(n) >= 3}


def fingerprints() -> tuple[dict, dict, dict[str, list[str]], dict[str, set[str]]]:
    """(hash -> [(task, kind)], task -> ref grams, pkg -> [tasks], task -> project names)."""
    by_hash: dict[str, list[tuple[str, str]]] = {}
    ref_grams: dict[str, set] = {}
    by_pkg: dict[str, list[str]] = {}
    names: dict[str, set[str]] = {}
    for t in tasks():
        cfg = task_config(t)
        src = str(cfg.get("source_code") or t)
        pkg = src.split("/")[-1]
        by_pkg.setdefault(pkg, []).append(t)
        names[t] = project_names(t, pkg)
        ref = reference_root(t)
        pdir = ref / src
        ref_grams[t] = grams(pdir.parent, [pkg]) if pdir.is_dir() else set()
        kinds = [("reference", pdir), ("heldout", heldout_dir(t)), ("check_tests", ref / str(cfg.get("check_tests") or "check_tests"))]
        for kind, d in kinds:
            if not d.is_dir():
                continue
            for f in d.rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts and f.stat().st_size >= MIN_EXACT:
                    by_hash.setdefault(sha256(f), []).append((t, kind))
    return by_hash, ref_grams, by_pkg, names


def sim(g: set, ref: set) -> float:
    return round(len(g & ref) / len(ref), 4) if g and ref else 0.0


def grams_from_texts(texts: list[str]) -> set:
    g: set = set()
    for s in texts:
        t = tokens(s)
        g |= {tuple(t[i:i + N]) for i in range(max(0, len(t) - N + 1))}
    return g


def archive_members(p: Path) -> list[tuple[str, bytes]] | None:
    """(name, bytes) of the Python members of an archive, or None if it is not one we can read."""
    out = []
    try:
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                for i in z.infolist():
                    if i.filename.endswith(".py") and i.file_size < 4 * 1024 * 1024:
                        out.append((i.filename, z.read(i)))
            return out
        if any(str(p).endswith(e) for e in (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
            with tarfile.open(p) as tf:
                for m in tf.getmembers():
                    if m.isfile() and m.name.endswith(".py") and m.size < 4 * 1024 * 1024:
                        f = tf.extractfile(m)
                        if f is not None:
                            out.append((m.name, f.read()))
            return out
    except Exception:  # noqa: BLE001 -- unreadable or not an archive
        return None
    return None


def file_row(p: Path) -> dict:
    st = p.stat()
    return {"path": str(p), "size": st.st_size, "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)), "sha256": sha256(p)}


def classify_location(path: str) -> str:
    if path.startswith(str(ROOT / "outputs")):
        return "adamas_outputs"
    if path.startswith(str(ROOT)):
        return "adamas_repo"
    if "/.cache/pip" in path or "/.cache/uv" in path or "/pip-cache" in path:
        return "package_cache"
    if re.search(r"/(dist|site)-packages/", path + "/"):
        if path.startswith(("/usr/", "/.uv/", "/root/.local/share/uv/python")):
            return "system_site_packages"
        return "venv_site_packages"
    if path.startswith("/tmp/claude-"):
        return "assistant_scratch"
    if path.startswith("/tmp/"):
        return "tmp"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "outputs" / "sandbox" / "copy_sweep"))
    ap.add_argument("--skip-outputs", action="store_true", help="leave AdaMAS outputs/ out of the walk")
    ap.add_argument("--root", default="/")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    by_hash, ref_grams, by_pkg, names = fingerprints()
    sizes: set[int] = set()                           # sizes of fingerprinted files, a cheap pre-filter
    for t in tasks():
        cfg = task_config(t)
        ref = reference_root(t)
        for d in (ref / str(cfg.get("source_code") or t), heldout_dir(t), ref / str(cfg.get("check_tests") or "check_tests")):
            if d.is_dir():
                sizes |= {f.stat().st_size for f in d.rglob("*") if f.is_file() and f.stat().st_size >= MIN_EXACT}
    prune = list(PRUNE) + ([str(ROOT / "outputs")] if a.skip_outputs else []) + [str(out)]
    exact: dict[str, list[dict]] = {}                  # containing dir -> rows
    pkg_dirs: list[dict] = []
    archives: list[dict] = []
    clones: list[dict] = []
    n_files = 0
    t0 = time.time()
    for dirpath, dirnames, filenames in os.walk(a.root, followlinks=False):
        if any(dirpath == p or dirpath.startswith(p.rstrip("/") + "/") for p in prune):
            dirnames[:] = []
            continue
        base = os.path.basename(dirpath)
        # run outputs hold agent-written packages by the thousand: the history similarity audit covers them, the
        # sweep only hashes their files for verbatim copies
        if base in by_pkg and any(f.endswith(".py") for f in filenames) and "__pycache__" not in dirpath \
                and not dirpath.startswith(str(ROOT / "outputs") + "/"):
            g = grams(Path(dirpath).parent, [base])
            for t in by_pkg[base]:
                s = sim(g, ref_grams.get(t, set()))
                pkg_dirs.append({"path": dirpath, "task": t, "similarity": s, "where": classify_location(dirpath)})
        if base == ".git" and "config" in filenames:
            try:
                cfg = (Path(dirpath) / "config").read_text(errors="replace").lower()
            except OSError:
                cfg = ""
            for url in re.findall(r"url\s*=\s*(\S+)", cfg):
                leaf = url.rstrip("/").split("/")[-1].removesuffix(".git")
                hit = [t for t, ns in names.items() if leaf in ns]
                if hit:
                    clones.append({"path": str(Path(dirpath).parent), "url": url, "tasks": hit, "where": classify_location(dirpath)})
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            n_files += 1
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_size in sizes:
                try:
                    h = sha256(Path(p))
                except OSError:
                    h = ""
                if h in by_hash:
                    exact.setdefault(dirpath, []).append({**file_row(Path(p)), "matches": sorted(set(by_hash[h]))})
            low = fn.lower()
            is_arch = low.endswith(ARCHIVE_EXT) or ("/http" in dirpath and "/.cache/pip" in dirpath and "." not in fn)
            if is_arch and st.st_size <= ARCHIVE_MAX:
                members = archive_members(Path(p))
                if not members:
                    continue
                for pkg, ts in by_pkg.items():
                    pref = None
                    for name, _ in members:
                        m = re.search(r"(?:^|/)" + re.escape(pkg) + r"/__init__\.py$", name)
                        if m:
                            pref = name[: m.start() + len(m.group(0)) - len("__init__.py")]
                            break
                    if pref is None:
                        continue
                    g = grams_from_texts([b.decode("utf-8", "replace") for nm, b in members if nm.startswith(pref)
                                          and not re.search(r"(^|/)(tests?|testing)/|/test_[^/]*$", nm[len(pref):])])
                    for t in ts:
                        archives.append({**file_row(Path(p)), "task": t, "package_prefix": pref, "similarity": sim(g, ref_grams.get(t, set())),
                                         "where": classify_location(p)})
        if n_files and n_files % 500000 < len(filenames):
            print(f"... {n_files} files, {time.time() - t0:.0f}s, at {dirpath[:80]}", file=sys.stderr, flush=True)

    # per-location rows; a held-out match is summarised by count and hash only
    locations = []
    for d, rows in sorted(exact.items()):
        kinds: dict[str, int] = {}
        for r in rows:
            for t, k in r["matches"]:
                kinds[f"{t}:{k}"] = kinds.get(f"{t}:{k}", 0) + 1
        locations.append({"type": "exact", "dir": d, "where": classify_location(d), "n_files": len(rows),
                          "bytes": sum(r["size"] for r in rows), "mtime_min": min(r["mtime"] for r in rows),
                          "mtime_max": max(r["mtime"] for r in rows), "matches": kinds,
                          "heldout_hashes": sorted({r["sha256"][:16] for r in rows if any(k == "heldout" for _, k in r["matches"])})})
    manifest = {"exact": exact, "package_dirs": pkg_dirs, "archives": archives, "git_clones": clones, "files_walked": n_files}
    for pd in pkg_dirs:                                # full file list of every flagged package directory
        if pd["similarity"] >= SIM_FLAG:
            pd["files"] = [file_row(f) for f in sorted(Path(pd["path"]).rglob("*")) if f.is_file()]
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    summary = {"files_walked": n_files, "seconds": round(time.time() - t0), "exact_locations": locations,
               "package_dirs": sorted(({k: v for k, v in pd.items() if k != "files"} for pd in pkg_dirs), key=lambda r: -r["similarity"]),
               "archives": sorted(({k: v for k, v in ar.items()} for ar in archives), key=lambda r: -r["similarity"]),
               "git_clones": clones}
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({"files_walked": n_files, "exact_locations": len(locations), "package_dirs": len(pkg_dirs),
                      "package_dirs_flagged": sum(pd["similarity"] >= SIM_FLAG for pd in pkg_dirs), "archives": len(archives),
                      "archives_flagged": sum(ar["similarity"] >= SIM_FLAG for ar in archives), "git_clones": len(clones)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
