"""Memory files, versions and snapshots (memory spec §1, §6).

Layout under ``memory/``::

    VERSION                 current version number, +1 on every write
    versions/v<N>/          read-only copy of every bank at version N
    domain_tags.yaml        the fixed tag vocabulary (people maintain it)
    first_pass/  repair/  author/   SKILL.md, categories.yaml, ... , CHANGELOG.yaml
    transfer/               pending pitfalls, rejections, record provenance (never injected)

A run reads only a snapshot: ``pin()`` returns the view of the version current
when the task started, and later writes do not reach it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "configs" / "memory.yaml"
ENABLE_ENV = "ADAMAS_MEMORY_ENABLED"
ROOT_ENV = "ADAMAS_MEMORY_ROOT"

#: bank -> its dictionary files (besides SKILL.md and CHANGELOG.yaml)
BANKS: dict[str, tuple[str, ...]] = {
    "first_pass": ("categories", "pitfalls", "patterns"),
    "repair": ("categories", "patterns"),
    "author": ("categories", "rules"),
}
PREFIX = {"first_pass": "FP", "repair": "RP", "author": "AU"}
STATES = ("trial", "active", "candidate", "retired")
RECALLABLE = ("active", "trial")
#: the id field of each dictionary file
ID_FIELD = {"categories": "category_id", "pitfalls": "pitfall_id", "patterns": "pattern_id", "rules": "rule_id"}
TRANSFER_FILES = ("pending_pitfalls", "rejected", "sources", "author_pending")


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(experiment: dict[str, Any] | None = None) -> dict[str, Any]:
    """configs/memory.yaml, overridden by the experiment's ``memory:`` / ``transfer:`` blocks and the env switch."""
    base = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.is_file() else {}
    cfg = _deep_merge(base or {}, {k: v for k, v in (experiment or {}).items() if k in ("memory", "transfer", "functional_test")})
    flag = (os.environ.get(ENABLE_ENV) or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        cfg.setdefault("memory", {})["enabled"] = True
    elif flag in ("0", "false", "no", "off"):
        cfg.setdefault("memory", {})["enabled"] = False
    return cfg


def memory_root(cfg: dict[str, Any] | None = None) -> Path:
    env = (os.environ.get(ROOT_ENV) or "").strip()
    if env:
        return Path(env).resolve()
    rel = ((cfg or {}).get("memory") or {}).get("root") or "memory/"
    p = Path(rel)
    return (p if p.is_absolute() else ROOT / p).resolve()


def read_yaml_list(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a YAML list")
    return [dict(x) for x in data]


def write_yaml(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha8(text: str) -> str:
    return sha256_text(text)[:8]


def current_version(root: Path) -> int:
    p = Path(root) / "VERSION"
    if not p.is_file():
        return 0
    return int(p.read_text(encoding="utf-8").strip() or 0)


def snapshot_dir(root: Path, version: int) -> Path:
    return Path(root) / "versions" / f"v{version}"


def _make_read_only(path: Path) -> None:
    for p in sorted(path.rglob("*"), reverse=True):
        mode = p.stat().st_mode
        p.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def take_snapshot(root: Path, version: int) -> Path:
    """Copy every bank, the tag vocabulary and VERSION into versions/v<N> and make it read-only."""
    root = Path(root)
    dest = snapshot_dir(root, version)
    if dest.exists():
        return dest
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    for bank in BANKS:
        if (root / bank).is_dir():
            shutil.copytree(root / bank, tmp / bank)
    for name in ("domain_tags.yaml", "VERSION"):
        if (root / name).is_file():
            shutil.copy2(root / name, tmp / name)
    tmp.rename(dest)
    _make_read_only(dest)
    return dest


@dataclass
class MemoryView:
    """One pinned, read-only version of all banks."""

    root: Path            # the memory root (for provenance only)
    version: int
    directory: Path       # versions/v<N>
    _cache: dict[str, Any] = field(default_factory=dict)

    def entries(self, bank: str, name: str) -> list[dict[str, Any]]:
        key = f"{bank}/{name}"
        if key not in self._cache:
            self._cache[key] = read_yaml_list(self.directory / bank / f"{name}.yaml")
        return self._cache[key]

    def skill(self, bank: str) -> str:
        p = self.directory / bank / "SKILL.md"
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def domain_tags(self) -> list[str]:
        data = yaml.safe_load((self.directory / "domain_tags.yaml").read_text(encoding="utf-8")) if (self.directory / "domain_tags.yaml").is_file() else {}
        return [str(t) for t in ((data or {}).get("tags") or [])]

    def active_categories(self, bank: str) -> list[dict[str, Any]]:
        return [c for c in self.entries(bank, "categories") if c.get("state") == "active"]


def pin(root: Path | None = None) -> MemoryView:
    """The view a task uses from start to end: the snapshot of the version current now."""
    root = Path(root) if root else memory_root()
    v = current_version(root)
    d = snapshot_dir(root, v)
    if not d.is_dir():
        d = take_snapshot(root, v)
    return MemoryView(root=root, version=v, directory=d)


def append_changelog(root: Path, bank: str, entry: dict[str, Any]) -> None:
    p = Path(root) / bank / "CHANGELOG.yaml"
    log = read_yaml_list(p)
    log.append(entry)
    write_yaml(p, log)


def commit_write(root: Path, bank: str, name: str, entries: list[dict[str, Any]], *, reason: str, origin: str,
                 changed_ids: list[str]) -> int:
    """Write one dictionary file (already validated), bump VERSION, log it, snapshot the new version."""
    root = Path(root)
    new = current_version(root) + 1
    write_yaml(root / bank / f"{name}.yaml", entries)
    (root / "VERSION").write_text(f"{new}\n", encoding="utf-8")
    append_changelog(root, bank, {
        "version": new, "date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "file": f"{name}.yaml",
        "origin": origin, "reason": reason, "changed": list(changed_ids),
    })
    take_snapshot(root, new)
    return new


__all__ = [
    "BANKS", "CONFIG_PATH", "ENABLE_ENV", "ID_FIELD", "MemoryView", "PREFIX", "RECALLABLE", "ROOT", "STATES",
    "append_changelog", "commit_write", "current_version", "load_config", "memory_root", "pin", "read_yaml_list",
    "sha256_text", "sha8", "snapshot_dir", "take_snapshot", "write_yaml",
]
