"""Durable, content-addressed Pareto persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from orchestra.control.pareto.archive import ParetoArchive


class ParetoPersistence:
    def __init__(self, run_dir: str | Path) -> None:
        self.directory = Path(run_dir) / "pareto"
        self.directory.mkdir(parents=True, exist_ok=True)

    def append_event(self, event: dict[str, Any]) -> None:
        self._append("archive_events.jsonl", event)

    def append_decision(self, decision: Any) -> None:
        self._append(
            "decisions.jsonl",
            decision.model_dump(mode="json") if hasattr(decision, "model_dump") else decision,
        )

    def snapshot(self, archive: ParetoArchive) -> None:
        self.save_estimated_archive(archive)
        self.save_realized_archive(archive)

    def save_estimated_archive(self, archive: ParetoArchive) -> None:
        self._atomic_json("estimated_archive.json", self._dump_archive(archive.estimated))

    def save_realized_archive(self, archive: ParetoArchive) -> None:
        self._atomic_json("realized_archive.json", self._dump_archive(archive.realized))

    def load_estimated_archive(self, config=None) -> ParetoArchive:
        from orchestra.control.pareto.schemas import (
            ParetoConfig,
            ParetoEvaluationKind,
            ParetoOrchestraCandidate,
        )

        archive = ParetoArchive(config or ParetoConfig())
        path = self.directory / "estimated_archive.json"
        if not path.exists():
            return archive
        raw = json.loads(path.read_text(encoding="utf-8"))
        for _ctx, entries in (raw or {}).items():
            for item in entries:
                cand = ParetoOrchestraCandidate.model_validate(item)
                archive.insert(cand, ParetoEvaluationKind.ESTIMATED)
        return archive

    def _append(self, name: str, value: Any) -> None:
        with (self.directory / name).open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(value, sort_keys=True, default=str, separators=(",", ":")) + "\n"
            )

    def _atomic_json(self, name: str, value: Any) -> None:
        target = self.directory / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, sort_keys=True, default=str, separators=(",", ":")), encoding="utf-8"
        )
        os.replace(temporary, target)

    @staticmethod
    def _dump_archive(values):
        return {
            key: [item.model_dump(mode="json") for item in entries]
            for key, entries in values.items()
        }
