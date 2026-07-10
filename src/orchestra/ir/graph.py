import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from orchestra.ir.edges import EdgeSpec
from orchestra.ir.nodes import NodeSpec


class OrchestraGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    graph_id: str
    version: str
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]
    initial_artifact_slots: dict[str, str]
    final_output_slot: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    def clone(self) -> "OrchestraGraph":
        return self.model_copy(deep=True)

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def load_graph(path: str | Path) -> OrchestraGraph:
    return OrchestraGraph.model_validate(
        yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    )
