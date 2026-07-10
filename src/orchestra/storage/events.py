from pathlib import Path

from orchestra.telemetry.events import TelemetryEvent


class AppendOnlyEventWriter:
    def __init__(self, run_dir: str | Path) -> None:
        self.path = Path(run_dir) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def append(self, event: TelemetryEvent) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
