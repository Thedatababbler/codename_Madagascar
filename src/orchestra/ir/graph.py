import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from orchestra.ir.edges import EdgeSpec
from orchestra.ir.nodes import NodeSpec, normalize_agent_backend_config

logger = logging.getLogger(__name__)
_WARNED_LEGACY_BACKEND_NODES: set[str] = set()


def _strip_default_backends_for_hash(data: dict[str, Any]) -> dict[str, Any]:
    """Keep content hashes stable for legacy graphs that omit backend."""
    nodes = []
    for node in data.get("nodes", []):
        item = dict(node)
        backend = item.get("backend")
        if backend is None or backend in (
            {"type": "structured_llm"},
            {"type": "structured_llm", "max_steps": 1},
        ):
            item.pop("backend", None)
        if not item.get("tools"):
            item.pop("tools", None)
        if item.get("model") is None:
            item.pop("model", None)
        if item.get("output_contract") is None:
            item.pop("output_contract", None)
        # M3.5 optional harness command; omit when unset so legacy hashes stay stable.
        if not item.get("command"):
            item.pop("command", None)
        # M4 optional fast-loop overlays; omit defaults so legacy hashes stay stable.
        if not item.get("prompt_feedback"):
            item.pop("prompt_feedback", None)
        if item.get("session_policy") in (None, "fresh"):
            item.pop("session_policy", None)
        nodes.append(item)
    return {**data, "nodes": nodes}


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
        payload = _strip_default_backends_for_hash(self.model_dump(mode="json"))
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def load_graph(path: str | Path) -> OrchestraGraph:
    import os
    import re

    env_pattern = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]+))?\}")

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            return env_pattern.sub(
                lambda m: os.getenv(m.group(1), m.group(2) or m.group(0)), value
            )
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    raw = expand(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    nodes = []
    for node in raw.get("nodes", []):
        if (
            isinstance(node, dict)
            and node.get("node_kind") == "agent"
            and node.get("backend") is None
        ):
            node_id = str(node.get("node_id"))
            if node_id not in _WARNED_LEGACY_BACKEND_NODES:
                logger.warning(
                    "Agent node %r has no backend; defaulting to structured_llm",
                    node_id,
                )
                _WARNED_LEGACY_BACKEND_NODES.add(node_id)
        nodes.append(normalize_agent_backend_config(node))
    raw = {**raw, "nodes": nodes}
    return OrchestraGraph.model_validate(raw)
