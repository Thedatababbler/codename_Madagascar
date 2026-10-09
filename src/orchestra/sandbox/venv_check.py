"""The task's own project package must not be installed in its environment (sandbox spec A §3).

``check`` reports, for one task environment and the task's top-level packages:
- whether ``pip list`` names a distribution of that package;
- whether ``import <pkg>`` succeeds from an empty directory outside any
  workspace, and from where it was imported.

``remove`` deletes what makes it importable: the dist-info / egg-info,
``.pth`` and egg-link entries, and the package directory or module itself
from site-packages.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_PROBE = r'''
import importlib.util, json, sys
out = {}
for name in sys.argv[1:]:
    try:
        spec = importlib.util.find_spec(name)
    except Exception as e:
        spec = None
    out[name] = (spec.origin or str(list(spec.submodule_search_locations or []))) if spec else None
print(json.dumps(out))
'''


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def site_packages(env_python: Path) -> Path | None:
    out = subprocess.run([str(env_python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                         capture_output=True, text=True, check=False).stdout.strip()
    return Path(out) if out and Path(out).is_dir() else None


def check(env_python: Path, packages: list[str]) -> dict[str, Any]:
    names = [p for p in packages if p]
    pip = subprocess.run([str(env_python), "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],
                         capture_output=True, text=True, check=False)
    try:
        dists = [_norm(d["name"]) for d in json.loads(pip.stdout or "[]")]
    except ValueError:
        dists = []
    with tempfile.TemporaryDirectory() as d:
        proc = subprocess.run([str(env_python), "-I", "-c", _PROBE, *names], cwd=d, capture_output=True, text=True, check=False)
    try:
        found = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        found = {n: f"probe failed: {proc.stderr[-200:]}" for n in names}
    pip_hits = [n for n in names if _norm(n) in dists]
    importable = {n: loc for n, loc in found.items() if loc}
    return {"python": str(env_python), "packages": names, "pip_list_hits": pip_hits, "importable_outside_workspace": importable,
            "clean": not pip_hits and not importable}


def remove(env_python: Path, packages: list[str]) -> list[str]:
    site = site_packages(env_python)
    if site is None:
        return []
    names = {_norm(p) for p in packages if p}
    removed = []
    for entry in sorted(site.iterdir()):
        stem = _norm(entry.name)
        base = _norm(entry.name.split(".")[0]) if not entry.is_dir() or entry.suffix in (".dist-info", ".egg-info") else _norm(entry.name)
        dist_hit = any(stem.startswith(n + "_") and (entry.name.endswith(".dist-info") or entry.name.endswith(".egg-info"))
                       or stem in (n + "_egg_link",) for n in names)
        meta_hit = entry.suffix in (".pth", ".egg-link") and any(n in stem for n in names)
        pkg_hit = base in names and (entry.is_dir() or entry.suffix in (".py", ".so"))
        if dist_hit or meta_hit or pkg_hit or any(stem.startswith(f"__editable__{n}") or stem.startswith(f"__editable___{n}") for n in names):
            try:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                removed.append(entry.name)
            except OSError:
                pass
    return removed


__all__ = ["check", "remove", "site_packages"]
