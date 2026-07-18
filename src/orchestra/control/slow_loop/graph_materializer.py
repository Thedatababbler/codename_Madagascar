"""Materialize future graph snapshots with backend/model assignments."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.capabilities import SessionPolicy
from orchestra.backends.catalog import KNOWN_BACKEND_CAPABILITIES, capabilities_for
from orchestra.control.slow_loop.schemas import (
    GlobalEdit,
    GlobalPlanRevision,
    PendingBackendAssignmentEdit,
    PendingGraphTemplateEdit,
)
from orchestra.decomposition.schemas import SubtaskSpec
from orchestra.ir.compiler import GraphCompiler
from orchestra.ir.graph import OrchestraGraph, load_graph
from orchestra.ir.nodes import (
    AgentNodeSpec,
    CodexSDKBackendConfig,
    NodeKind,
    SmolagentsCodeBackendConfig,
    StructuredLLMBackendConfig,
)


class PendingNodeAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    backend_id: str
    model_name: str | None = None


class FutureSubtaskExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    graph_revision_id: str
    graph_hash: str
    graph_path: str
    node_assignments: list[PendingNodeAssignment] = Field(default_factory=list)
    parent_graph_hash: str = ""


class MaterializedGraphPaths(BaseModel):
    model_config = ConfigDict(extra="forbid")

    staging_path: str
    final_path: str


class MaterializedGraphResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    graph: OrchestraGraph
    graph_hash: str
    graph_path: str  # always the logical final path
    staging_path: str = ""
    parent_graph_hash: str
    node_assignments: list[PendingNodeAssignment] = Field(default_factory=list)
    execution_config: FutureSubtaskExecutionConfig
    paths: MaterializedGraphPaths | None = None


class GraphMaterializationError(RuntimeError):
    pass


_BACKEND_CTORS = {
    "structured_llm": lambda: StructuredLLMBackendConfig(),
    "smolagents_code": lambda: SmolagentsCodeBackendConfig(),
    "codex_sdk": lambda: CodexSDKBackendConfig(),
}


class FutureGraphMaterializer:
    def __init__(self, compiler: GraphCompiler | None = None) -> None:
        if compiler is not None:
            self.compiler = compiler
        else:
            from orchestra.cli.validate_graph import build_compiler

            self.compiler = build_compiler("configs/contracts")

    def materialize(
        self,
        *,
        subtask: SubtaskSpec,
        base_graph: OrchestraGraph | None = None,
        active_plan_revision: GlobalPlanRevision | None = None,
        allowed_backend_pools: dict[str, list[str]] | None = None,
        backend_model_pools: dict[str, list[str]] | None = None,
        revision_graphs_dir: str | Path | None = None,
        staging_graphs_dir: str | Path | None = None,
        final_graphs_dir: str | Path | None = None,
        revision_id: str = "adhoc",
    ) -> MaterializedGraphResult:
        template = subtask.local_graph_template
        # Prefer explicit staging/final dirs; revision_graphs_dir is legacy alias.
        staging_dir = staging_graphs_dir or revision_graphs_dir
        final_dir = final_graphs_dir or staging_dir
        graph = base_graph or load_graph(template)
        parent_hash = graph.content_hash
        assignments: list[PendingNodeAssignment] = []

        edits: list[GlobalEdit] = []
        if active_plan_revision is not None:
            edits = list(active_plan_revision.edits)

        # Apply graph template edit from metadata / edits.
        meta = dict(subtask.metadata or {})
        backend_meta = meta.get("backend_assignment")
        model_meta = meta.get("model_assignment")

        for edit in edits:
            if (
                isinstance(edit, PendingGraphTemplateEdit)
                and edit.subtask_id == subtask.subtask_id
            ):
                graph = load_graph(edit.graph_template_id)
                parent_hash = graph.content_hash
            if (
                isinstance(edit, PendingBackendAssignmentEdit)
                and edit.subtask_id == subtask.subtask_id
            ):
                graph = self._assign_backend(
                    graph,
                    node_id=edit.node_id,
                    backend_id=edit.backend_id,
                    model_name=edit.model_name,
                    allowed_backend_pools=allowed_backend_pools or {},
                    backend_model_pools=backend_model_pools or {},
                )
                assignments.append(
                    PendingNodeAssignment(
                        node_id=edit.node_id,
                        backend_id=edit.backend_id,
                        model_name=edit.model_name,
                    )
                )

        if isinstance(backend_meta, dict) and backend_meta.get("backend_id"):
            node_id = str(backend_meta.get("node_id") or "")
            backend_id = str(backend_meta["backend_id"])
            model_name = backend_meta.get("model_name")
            if model_name is not None:
                model_name = str(model_name)
            if node_id and not any(a.node_id == node_id for a in assignments):
                graph = self._assign_backend(
                    graph,
                    node_id=node_id,
                    backend_id=backend_id,
                    model_name=model_name,
                    allowed_backend_pools=allowed_backend_pools or {},
                    backend_model_pools=backend_model_pools or {},
                )
                assignments.append(
                    PendingNodeAssignment(
                        node_id=node_id,
                        backend_id=backend_id,
                        model_name=model_name,
                    )
                )

        if isinstance(model_meta, dict) and model_meta.get("model_name"):
            node_id = str(model_meta.get("node_id") or "")
            model_name = str(model_meta["model_name"])
            if node_id:
                graph = self._assign_model(graph, node_id=node_id, model_name=model_name)
                for a in assignments:
                    if a.node_id == node_id:
                        a.model_name = model_name
                        break
                else:
                    backend_id = self._node_backend_id(graph, node_id)
                    assignments.append(
                        PendingNodeAssignment(
                            node_id=node_id,
                            backend_id=backend_id,
                            model_name=model_name,
                        )
                    )

        # Capability / schema validation via compiler.
        try:
            self.compiler.compile(graph)
        except Exception as exc:  # noqa: BLE001
            raise GraphMaterializationError(
                f"materialized graph failed validation: {exc}"
            ) from exc

        # Enforce FRESH-only for CodeAgent/Codex via capabilities.
        for node in graph.nodes:
            if node.node_kind is not NodeKind.AGENT:
                continue
            assert isinstance(node, AgentNodeSpec)
            backend = node.resolved_backend()
            caps = capabilities_for(str(backend.type))
            if caps is None:
                raise GraphMaterializationError(
                    f"unknown backend type {backend.type} on node {node.node_id}"
                )
            if SessionPolicy.FRESH not in caps.supported_session_policies:
                raise GraphMaterializationError(
                    f"backend {backend.type} must support FRESH"
                )
            policy = getattr(backend, "thread_policy", None)
            if policy not in (None, "fresh"):
                raise GraphMaterializationError(
                    f"non-FRESH session policy forbidden on {node.node_id}"
                )

        staging_path = ""
        final_path = template
        paths: MaterializedGraphPaths | None = None
        graph_hash = graph.content_hash
        if staging_dir is not None:
            s_dir = Path(staging_dir)
            f_dir = Path(final_dir) if final_dir is not None else s_dir
            s_dir.mkdir(parents=True, exist_ok=True)
            staging_path = str(s_dir / f"{subtask.subtask_id}.yaml")
            final_path = str(f_dir / f"{subtask.subtask_id}.yaml")
            # Embed stable revision metadata (no graph_hash field — it would
            # change content_hash and create a chicken-and-egg).
            self._write_graph_snapshot(
                path=staging_path,
                graph=graph,
                revision_id=revision_id,
                parent_graph_hash=parent_hash,
                graph_hash=None,
            )
            graph = load_graph(staging_path)
            graph_hash = graph.content_hash
            paths = MaterializedGraphPaths(
                staging_path=staging_path, final_path=final_path
            )
            if ".staging-" in final_path.replace("\\", "/"):
                raise GraphMaterializationError(
                    "PLAN_REVISION_GRAPH_PATH_INVALID: final path must not "
                    f"contain staging segment: {final_path}"
                )

        exec_cfg = FutureSubtaskExecutionConfig(
            subtask_id=subtask.subtask_id,
            graph_revision_id=revision_id,
            graph_hash=graph_hash,
            graph_path=final_path,
            node_assignments=assignments,
            parent_graph_hash=parent_hash,
        )
        return MaterializedGraphResult(
            subtask_id=subtask.subtask_id,
            graph=graph,
            graph_hash=graph_hash,
            graph_path=final_path,
            staging_path=staging_path,
            parent_graph_hash=parent_hash,
            node_assignments=assignments,
            execution_config=exec_cfg,
            paths=paths,
        )

    def _node_backend_id(self, graph: OrchestraGraph, node_id: str) -> str:
        node = next(n for n in graph.nodes if n.node_id == node_id)
        assert isinstance(node, AgentNodeSpec)
        return str(node.resolved_backend().type)

    def _assign_backend(
        self,
        graph: OrchestraGraph,
        *,
        node_id: str,
        backend_id: str,
        model_name: str | None,
        allowed_backend_pools: dict[str, list[str]],
        backend_model_pools: dict[str, list[str]],
    ) -> OrchestraGraph:
        allowed = {
            b for group in allowed_backend_pools.values() for b in group
        } or set(KNOWN_BACKEND_CAPABILITIES)
        if backend_id not in allowed and allowed_backend_pools:
            raise GraphMaterializationError(
                f"backend {backend_id} not in allowed pool"
            )
        caps = capabilities_for(backend_id)
        if caps is None:
            raise GraphMaterializationError(f"unknown backend {backend_id}")
        if SessionPolicy.FRESH not in caps.supported_session_policies:
            raise GraphMaterializationError(
                f"backend {backend_id} does not support FRESH"
            )
        if model_name and backend_model_pools:
            pool = backend_model_pools.get(backend_id, [])
            if pool and model_name not in pool:
                raise GraphMaterializationError(
                    f"model {model_name} not in pool for {backend_id}"
                )
        ctor = _BACKEND_CTORS.get(backend_id)
        if ctor is None:
            raise GraphMaterializationError(f"cannot construct backend {backend_id}")
        new_backend = ctor()
        nodes = []
        found = False
        for node in graph.nodes:
            if node.node_id != node_id:
                nodes.append(node)
                continue
            if node.node_kind is not NodeKind.AGENT:
                raise GraphMaterializationError(
                    f"node {node_id} is not an agent node"
                )
            assert isinstance(node, AgentNodeSpec)
            update: dict[str, Any] = {
                "backend": new_backend,
                "session_policy": "fresh",
            }
            if model_name and node.model is not None:
                update["model"] = node.model.model_copy(update={"name": model_name})
            elif model_name:
                from orchestra.backends.base import ModelSpec

                update["model"] = ModelSpec(
                    provider="openai_compatible",
                    name=model_name,
                    temperature=0.0,
                    max_tokens=4096,
                )
            nodes.append(node.model_copy(update=update))
            found = True
        if not found:
            raise GraphMaterializationError(f"unknown node_id {node_id}")
        return graph.model_copy(update={"nodes": nodes})

    def _assign_model(
        self, graph: OrchestraGraph, *, node_id: str, model_name: str
    ) -> OrchestraGraph:
        nodes = []
        found = False
        for node in graph.nodes:
            if node.node_id != node_id:
                nodes.append(node)
                continue
            if node.node_kind is not NodeKind.AGENT:
                raise GraphMaterializationError(
                    f"node {node_id} is not an agent node"
                )
            assert isinstance(node, AgentNodeSpec)
            if node.model is None:
                from orchestra.backends.base import ModelSpec

                model = ModelSpec(
                    provider="openai_compatible",
                    name=model_name,
                    temperature=0.0,
                    max_tokens=4096,
                )
            else:
                model = node.model.model_copy(update={"name": model_name})
            nodes.append(node.model_copy(update={"model": model}))
            found = True
        if not found:
            raise GraphMaterializationError(f"unknown node_id {node_id}")
        return graph.model_copy(update={"nodes": nodes})

    def _write_graph_snapshot(
        self,
        *,
        path: str,
        graph: OrchestraGraph,
        revision_id: str,
        parent_graph_hash: str,
        graph_hash: str | None,
    ) -> None:
        payload = graph.model_dump(mode="json")
        meta = {
            **dict(payload.get("metadata") or {}),
            "plan_revision_id": revision_id,
            "parent_graph_hash": parent_graph_hash,
        }
        if graph_hash is not None:
            meta["graph_hash"] = graph_hash
        payload["metadata"] = meta
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        tmp = path_obj.with_suffix(path_obj.suffix + f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=True, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path_obj)
