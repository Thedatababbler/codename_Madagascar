"""Public-example harness for LiveCodeBench abc309_a (Nine).

These cases match the LiveCodeBench public examples (not private tests).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SOLUTION = Path(__file__).resolve().parent / "solution.py"

# LiveCodeBench public examples for abc309_a.
PUBLIC_CASES = [
    ("7 8\n", "Yes\n"),
    ("1 9\n", "No\n"),
    ("3 4\n", "No\n"),
]


def _run(stdin: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(SOLUTION)],
        input=stdin,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    return proc.stdout


def test_public_examples() -> None:
    for stdin, expected in PUBLIC_CASES:
        assert _run(stdin) == expected, f"failed on input={stdin!r}"
