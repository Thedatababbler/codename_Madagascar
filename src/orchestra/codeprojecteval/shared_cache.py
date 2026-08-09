"""A JSON cache that survives concurrent writers.

The collected-test counts are computed once per repository and reused by every
scoring run. Read-modify-write on a plain file is fine while runs are serial and
silently lossy once they are not: two evaluations that finish together each read
the same snapshot, and the second one writes back a copy that has forgotten the
first one's entry. The next run then recomputes it, which is slow but harmless,
until a run reads a half-written file and fails outright.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:  # pragma: no cover - POSIX only
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on a sibling of ``path``.

    The lock lives beside the cache rather than on it so that the cache itself
    can be replaced atomically while the lock is held.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover - non-POSIX
        yield
        return
    with open(path.with_suffix(path.suffix + ".lock"), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: Path) -> dict[str, Any]:
    """The cache's current contents, or empty if it is absent or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace the cache atomically, so no reader ever sees a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def get_or_compute(
    path: Path | None, key: str, compute: Callable[[], Any]
) -> Any:
    """Return ``key`` from the cache, computing and storing it if absent.

    ``compute`` runs outside the lock. It can take minutes, and holding the lock
    across it would serialise every concurrent run behind the first one -- the
    opposite of why the cache exists. A racing writer may therefore compute the
    same value twice, which costs time but cannot corrupt the file: the entry is
    re-read under the lock before being written.
    """
    if path is None:
        return compute()
    with locked(path):
        cached = read_json(path).get(key)
    if cached is not None:
        return cached

    value = compute()
    with locked(path):
        payload = read_json(path)
        # Re-read rather than reusing the earlier snapshot: another run may have
        # added its own entry while this one was computing.
        payload[key] = value
        write_json(path, payload)
    return value
