"""Cross-process exclusive ownership for a single run checkpoint.

Uses an OS advisory file lock scoped to ``<run_dir>/.scheduler.lock``.
At most one live scheduler may own and mutate a given run at one time.
Ownership is released automatically when the owning process terminates
(or when :meth:`RunOwnership.release` is called).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


class RunOwnershipError(RuntimeError):
    """Raised when another live scheduler already owns the run."""


class RunOwnership:
    """Exclusive run-level ownership via ``fcntl.flock``."""

    def __init__(
        self,
        run_dir: str | Path,
        *,
        owner_id: str | None = None,
        blocking: bool = False,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.lock_path = self.run_dir / ".scheduler.lock"
        self.owner_id = owner_id or f"pid-{os.getpid()}"
        self.blocking = blocking
        self._fh: Any | None = None
        self.acquired_at: str | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self) -> RunOwnership:
        if fcntl is None:
            raise RunOwnershipError("fcntl advisory locks are unavailable on this platform")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+", encoding="utf-8")  # noqa: SIM115
        flags = fcntl.LOCK_EX
        if not self.blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fh.fileno(), flags)
        except BlockingIOError as exc:
            fh.close()
            raise RunOwnershipError(
                f"run already owned by another live scheduler: {self.lock_path}"
            ) from exc
        self._fh = fh
        self.acquired_at = datetime.now(UTC).isoformat()
        payload = {
            "owner_id": self.owner_id,
            "pid": os.getpid(),
            "acquired_at": self.acquired_at,
            "run_dir": str(self.run_dir),
        }
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(payload, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> RunOwnership:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        self.release()

    def read_owner_record(self) -> dict[str, Any] | None:
        if not self.lock_path.exists():
            return None
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
