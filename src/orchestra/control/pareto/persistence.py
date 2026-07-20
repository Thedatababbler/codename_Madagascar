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
        self._atomic_json("estimated_archive.json", self._dump_archive(
            archive.estimated_complete, archive.estimated_partial
        ))

    def save_realized_archive(self, archive: ParetoArchive) -> None:
        self._atomic_json("realized_archive.json", self._dump_archive(
            archive.realized_complete, archive.realized_partial
        ))

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
        for _ctx, entries in (raw.get("complete", raw) or {}).items():
            for item in entries:
                cand = ParetoOrchestraCandidate.model_validate(item)
                archive.insert(cand, ParetoEvaluationKind.ESTIMATED)
        for _ctx, entries in (raw.get("partial", {}) or {}).items():
            for item in entries:
                cand = ParetoOrchestraCandidate.model_validate(item)
                archive.insert(cand, ParetoEvaluationKind.ESTIMATED)
        return archive

    def load_realized_archive(self, config=None) -> ParetoArchive:
        from orchestra.control.pareto.schemas import (
            ParetoConfig,
            ParetoEvaluationKind,
            ParetoOrchestraCandidate,
        )
        archive = ParetoArchive(config or ParetoConfig())
        path = self.directory / "realized_archive.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for entries in (raw.get("complete", raw) or {}).values():
                for item in entries:
                    cand = ParetoOrchestraCandidate.model_validate(item)
                    archive.insert(cand, ParetoEvaluationKind.REALIZED)
            for entries in (raw.get("partial", {}) or {}).values():
                for item in entries:
                    cand = ParetoOrchestraCandidate.model_validate(item)
                    archive.insert(cand, ParetoEvaluationKind.REALIZED)
        return archive

    def load_decisions(self) -> list[dict[str, Any]]:
        path = self.directory / "decisions.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line]

    def _append(self, name: str, value: Any) -> None:
        target = self.directory / name
        event_id = value.get("event_id") if isinstance(value, dict) else None
        if event_id and target.exists():
            existing = target.read_text(encoding="utf-8").splitlines()
            if any(
                json.loads(line).get("event_id") == event_id
                for line in existing
                if line
            ):
                return
        with target.open("a", encoding="utf-8") as handle:
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
    def _dump_archive(complete, partial):
        return {
            "schema_version": 2,
            "complete": {key: [item.model_dump(mode="json") for item in entries]
                         for key, entries in complete.items()},
            "partial": {key: [item.model_dump(mode="json") for item in entries]
                        for key, entries in partial.items()},
        }
