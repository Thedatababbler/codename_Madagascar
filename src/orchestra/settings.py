"""Runtime settings resolution without uncontrolled global environment mutation.

Precedence (highest last):
  1. process environment (already present)
  2. optional env-file values
  3. built-in defaults (returned in the mapping only)
  4. explicit CLI / caller overrides

Importing this module does not read files or mutate ``os.environ``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

_DEFAULTS: dict[str, str] = {
    "ANALYST_MODEL": "gpt-5-mini",
    "CODER_MODEL": "gpt-5-mini",
    "REPAIR_MODEL": "gpt-5-mini",
    "CODEAGENT_MODEL": "gpt-5-mini",
    "LCB_DATA_DIR": "/root/data/livecodebench/code_generation_lite",
    "LCB_REPOSITORY_PATH": "/root/projects/LiveCodeBench",
    "BBEH_DATA_DIR": "/root/projects/EvoMAS/dataset/bbeh/benchmark_tasks",
}


def _parse_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_runtime_settings(
    *,
    env_file: str | Path | None = ".env",
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
    include_defaults: bool = True,
    include_lcb_repository_default: bool = True,
) -> dict[str, str]:
    """Return resolved settings without mutating the process environment.

    ``include_lcb_repository_default=False`` is used by synthetic / API-free
    formal samples so they neither require nor inject ``LCB_REPOSITORY_PATH``.
    """
    base = dict(environ if environ is not None else os.environ)
    file_values = _parse_env_file(env_file) if env_file is not None else {}
    resolved: dict[str, str] = {}
    if include_defaults:
        defaults = dict(_DEFAULTS)
        if not include_lcb_repository_default:
            defaults.pop("LCB_REPOSITORY_PATH", None)
        # Prefer LCB_REPO_PATH alias when present in process/file.
        alias = base.get("LCB_REPO_PATH") or file_values.get("LCB_REPO_PATH")
        if include_lcb_repository_default and alias and "LCB_REPOSITORY_PATH" not in base:
            defaults["LCB_REPOSITORY_PATH"] = alias
        resolved.update(defaults)
    # File values overlay defaults but do not override process env.
    for key, value in file_values.items():
        if key not in base:
            resolved[key] = value
    # Process env wins over file/defaults for keys that are set.
    for key, value in base.items():
        resolved[key] = value
    if overrides:
        resolved.update({str(k): str(v) for k, v in overrides.items()})
    return resolved


def apply_runtime_settings(
    settings: Mapping[str, str],
    *,
    environ: MutableMapping[str, str] | None = None,
    only_missing: bool = True,
    keys: set[str] | None = None,
) -> None:
    """Optionally apply resolved settings into a mapping (default: ``os.environ``)."""
    target: MutableMapping[str, str] = environ if environ is not None else os.environ
    for key, value in settings.items():
        if keys is not None and key not in keys:
            continue
        if only_missing and key in target:
            continue
        target[key] = value


def load_env_file(path: str | Path = ".env") -> None:
    """Compatibility helper for CLI entry points.

    Loads ``.env`` values via ``setdefault`` only. Does **not** inject hard-coded
    ``LCB_REPOSITORY_PATH`` / model defaults into the process environment (those
    live in :func:`resolve_runtime_settings` and must be applied explicitly when
    a runner needs them). Prefer :func:`resolve_runtime_settings` for new code.
    """
    for key, value in _parse_env_file(path).items():
        os.environ.setdefault(key, value)


def settings_get(
    settings: Mapping[str, Any],
    key: str,
    default: str | None = None,
) -> str | None:
    value = settings.get(key)
    if value is None or value == "":
        return default
    return str(value)
