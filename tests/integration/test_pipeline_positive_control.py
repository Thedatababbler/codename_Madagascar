"""The whole-pipeline positive control (scripts/pipeline_positive_control.py) must pass: every pipeline change runs it.

It runs as its own process, outside pytest, so the real sandbox applies (the test suite's exemption is pytest-only).
No model is called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_pipeline_positive_control():
    env = {k: v for k, v in os.environ.items() if k not in ("ADAMAS_SANDBOX_TEST_UNCONFINED", "PYTEST_CURRENT_TEST")}
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "pipeline_positive_control.py")], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=1800)
    report = json.loads((ROOT / "outputs" / "pipeline_positive_control" / "last.json").read_text())
    failed = [c for c in report["checks"] if not c["ok"]]
    assert proc.returncode == 0 and report["ok"], json.dumps(failed, indent=1)[:4000]
