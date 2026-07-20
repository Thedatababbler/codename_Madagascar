"""Append-only search trace export."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class OrchestraSearchTrace:
    def __init__(self, run_dir: str | Path) -> None:
        directory = Path(run_dir) / "pareto"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "search_traces.jsonl"

    def write(self, event: dict[str, Any] | Any) -> None:
        payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")) + "\n"
            )
