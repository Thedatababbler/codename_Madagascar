"""A sealed module whose only job is to prove the guard lets sealed callers through."""

from __future__ import annotations

from _guard import heldout_dir  # noqa: F401  (imported when scripts/sealed is on sys.path)


def probe(task: str) -> str:
    return str(heldout_dir(task))
