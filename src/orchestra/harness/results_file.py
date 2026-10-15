"""The gate's structured result travels in a file, not in a tail of its stdout (2026-10-15).

``run_authoritative_harness_command`` kept the last 4000 characters of the check script's output and parsed the score
line out of that. A predecessor suite of 30+ cases prints a score line longer than the tail, so the line was cut, the
parse failed and the suite read as "not run" -- the regression check never covered a large suite (voluptuous
validator_library 35 cases, utilities_and_humanize 30, sandbox baseline 10-12). The check script now writes its payload
to ``--results-file``; the caller reads it and treats a missing file as "no result", never as a parse of the tail.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

FLAG = "--results-file"
CHECK_SCRIPT = "adamas_cpe_check.py"


def _supports(script: str) -> bool:
    """A check script materialised before 2026-10-15 does not know the flag (argparse would refuse it)."""
    try:
        return FLAG in Path(script).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def attach(command: list[str]) -> tuple[list[str], Path | None]:
    """``(command, results dir)``: the flag added when the command runs a check script that supports it."""
    script = next((a for a in command[1:] if a.endswith(CHECK_SCRIPT)), None)
    if script is None or FLAG in command or not _supports(script):
        return list(command), None
    holder = Path(tempfile.mkdtemp(prefix="adamas-results-"))
    return [*command, FLAG, str(holder / "results.json")], holder


def read(holder: Path | None) -> dict[str, Any] | None:
    """The payload the check script wrote, or None (missing or unreadable); the holder is removed."""
    if holder is None:
        return None
    try:
        data = json.loads((holder / "results.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        shutil.rmtree(holder, ignore_errors=True)


__all__ = ["CHECK_SCRIPT", "FLAG", "attach", "read"]
