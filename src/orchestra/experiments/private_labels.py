"""Offline-only private label artifacts.

Online selection, estimation, feasibility, activation, and scheduling must never
read these files. Access is instrumented so tests can prove isolation.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_ACCESS_LOG: list[dict[str, Any]] = []


def reset_private_label_access_log() -> None:
    with _LOCK:
        _ACCESS_LOG.clear()


def private_label_access_log() -> list[dict[str, Any]]:
    with _LOCK:
        return list(_ACCESS_LOG)


def load_private_labels(
    path: str | Path,
    *,
    purpose: str,
    allow_online: bool = False,
) -> dict[str, Any]:
    """Load a private-label artifact.

    Raises ``RuntimeError`` when ``allow_online`` is false and ``purpose``
    indicates an online control-plane consumer.
    """
    path = Path(path)
    record = {
        "path": str(path.resolve()) if path.exists() else str(path),
        "purpose": purpose,
        "allow_online": allow_online,
    }
    with _LOCK:
        _ACCESS_LOG.append(record)
    online_purposes = {
        "selector",
        "estimator",
        "feasibility",
        "activation",
        "scheduler",
        "online_control",
        "candidate_generation",
    }
    if not allow_online and purpose in online_purposes:
        raise RuntimeError(
            f"private-label artifact refused for online purpose={purpose!r}: {path}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"private labels must be a JSON object: {path}")
    return payload


def project_offline_private_score(
    labels: dict[str, Any],
    *,
    task_id: str,
) -> float | None:
    """Post-execution diagnostic score only (never used online)."""
    tasks = labels.get("tasks") or {}
    entry = tasks.get(task_id) or labels.get(task_id) or {}
    if not isinstance(entry, dict):
        return None
    score = entry.get("private_score")
    if score is None:
        return None
    return float(score)
