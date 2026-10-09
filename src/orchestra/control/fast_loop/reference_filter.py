"""Training tasks: gate cases that fail on the reference implementation leave the gate (author fake-object fix §6.5).

Once per milestone, after custody froze the authored suite, the sealed script
``scripts/sealed/reference_check.py`` runs the frozen suite on the reference
implementation. The cases it fails are written to
``<frozen suite>.suite_suspect.json``:
- the gate's check script drops them from the score;
- the controller adds them to ``suite_suspect``, so they also leave the
  unified acceptance;
- the author's pending list gets one entry per case.

Test tasks are skipped: there is no reference to use. With
``author.train_reference_filter: false`` nothing happens.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "sealed" / "reference_check.py"


def enabled() -> bool:
    try:
        from orchestra.sandbox.policy import load_config

        return bool((load_config().get("author") or {}).get("train_reference_filter", True))
    except Exception:  # noqa: BLE001
        return True


def train_tasks() -> set[str]:
    from orchestra.memory.store import load_config

    return set((load_config().get("transfer") or {}).get("train_tasks") or [])


def suspects_path(frozen: Path) -> Path:
    return Path(str(frozen).rstrip("/") + ".suite_suspect.json")


def _key(node: str) -> str:
    tail = str(node).split("spec_tests/", 1)[-1]
    head, _, rest = tail.partition("::")
    return (Path(head).name + "::" + rest).split("[", 1)[0]


def sealed_reference_failures(task: str, frozen: Path) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "ref.json"
        subprocess.run([sys.executable, str(SCRIPT), "--task", task, "--suite", str(frozen), "--out", str(out)],
                       capture_output=True, text=True, timeout=1800, cwd=str(ROOT))
        data = json.loads(out.read_text()) if out.is_file() else {}
    if not data.get("total"):
        raise RuntimeError(f"reference check produced no result for {task} {frozen}")
    return list(data.get("reference_failed") or [])


def apply(task_id: str, frozen: Path, *, runner: Callable[[str, Path], list[str]] | None = None,
          pending_path: Path | None = None) -> list[str] | None:
    """The case keys removed from the gate, or None when the step does not apply (test task, switch off, no suite)."""
    task = task_id.removeprefix("rb_")
    frozen = Path(frozen)
    if not enabled() or task not in train_tasks() or not frozen.is_dir():
        return None
    p = suspects_path(frozen)
    if p.is_file():
        return list(json.loads(p.read_text()).get("cases") or [])
    failed = (runner or sealed_reference_failures)(task, frozen)
    cases = sorted({_key(c) for c in failed})
    p.write_text(json.dumps({"task": task, "suite": str(frozen), "cases": cases, "reason": "fails on the reference implementation",
                             "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}, indent=1), encoding="utf-8")
    if cases:
        from orchestra.memory.store import memory_root, read_yaml_list, write_yaml

        pp = pending_path or (memory_root() / "transfer" / "author_pending.yaml")
        rows = read_yaml_list(pp)
        rows += [{"task": task, "suite": frozen.name, "case": c, "note": "gate case fails on the reference implementation (removed from the gate)",
                  "date": datetime.now(UTC).strftime("%Y-%m-%d")} for c in cases]
        write_yaml(pp, rows)
    return cases


__all__ = ["apply", "enabled", "sealed_reference_failures", "suspects_path", "train_tasks"]
