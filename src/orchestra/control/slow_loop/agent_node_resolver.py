"""Resolve a real future agent node for Slow Loop backend adaptation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from orchestra.backends.capabilities import SessionPolicy
from orchestra.backends.catalog import capabilities_for
from orchestra.decomposition.schemas import SubtaskSpec
from orchestra.ir.graph import OrchestraGraph, load_graph
from orchestra.ir.nodes import AgentNodeSpec, NodeKind

IMPLEMENTER_ROLES = ("implementer", "coder", "solver")


class AgentNodeResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str
    node_id: str | None = None
    current_backend_id: str | None = None
    current_model_name: str | None = None
    eligible: bool = False
    reason: str | None = None


class FutureAgentNodeResolver:
    def resolve(
        self,
        *,
        subtask: SubtaskSpec,
        purpose: str = "backend_adaptation",
        allowed_backend_ids: set[str] | None = None,
        graph: OrchestraGraph | None = None,
    ) -> AgentNodeResolution:
        del purpose
        allowed = allowed_backend_ids or set()
        try:
            loaded = graph or load_graph(subtask.local_graph_template)
        except Exception as exc:  # noqa: BLE001
            return AgentNodeResolution(
                subtask_id=subtask.subtask_id,
                eligible=False,
                reason=f"graph_load_failed:{exc}",
            )

        preferred = str(
            (subtask.metadata or {}).get("preferred_adaptive_node_id") or ""
        )
        agents: list[AgentNodeSpec] = [
            n for n in loaded.nodes if n.node_kind is NodeKind.AGENT
        ]
        assert all(isinstance(n, AgentNodeSpec) for n in agents)

        def _backend_id(node: AgentNodeSpec) -> str:
            return str(node.resolved_backend().type)

        def _model_name(node: AgentNodeSpec) -> str | None:
            if node.model is None:
                return None
            return str(node.model.name)

        def _fresh_ok(node: AgentNodeSpec) -> bool:
            backend = node.resolved_backend()
            caps = capabilities_for(str(backend.type))
            if caps is None:
                return False
            if SessionPolicy.FRESH not in caps.supported_session_policies:
                return False
            policy = getattr(backend, "thread_policy", None)
            return policy in (None, "fresh")

        ranked: list[AgentNodeSpec] = []
        if preferred:
            for node in agents:
                if node.node_id == preferred and _fresh_ok(node):
                    ranked.append(node)
                    break
        if not ranked:
            role_hits: list[AgentNodeSpec] = []
            for node in agents:
                role = str(getattr(node, "contract_id", "") or node.node_id).lower()
                blob = f"{role} {node.node_id}".lower()
                if any(r in blob for r in IMPLEMENTER_ROLES) and _fresh_ok(node):
                    role_hits.append(node)
            ranked.extend(sorted(role_hits, key=lambda n: n.node_id))
        if not ranked:
            ranked = sorted(
                [n for n in agents if _fresh_ok(n)],
                key=lambda n: n.node_id,
            )
        # Prefer nodes whose *current* backend is allowlisted when set; otherwise
        # still return an eligible node so alternate backends can be applied.
        if allowed and ranked:
            preferred_allowed = [
                n for n in ranked if _backend_id(n) in allowed
            ]
            if preferred_allowed:
                ranked = preferred_allowed + [
                    n for n in ranked if n not in preferred_allowed
                ]

        if not ranked:
            return AgentNodeResolution(
                subtask_id=subtask.subtask_id,
                eligible=False,
                reason="NO_ELIGIBLE_FUTURE_AGENT_NODE",
            )
        chosen = ranked[0]
        return AgentNodeResolution(
            subtask_id=subtask.subtask_id,
            node_id=chosen.node_id,
            current_backend_id=_backend_id(chosen),
            current_model_name=_model_name(chosen),
            eligible=True,
            reason=None,
        )
