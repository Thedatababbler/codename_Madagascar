"""Backend-agnostic session lineage registry and parent/workspace binding."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from orchestra.backends.base import BackendSessionRef
from orchestra.backends.capabilities import SessionPolicy
from orchestra.control.fast_loop.schemas import NodeSessionDirective
from orchestra.control.task_state import BackendSessionRecord, TaskExecutionState
from orchestra.workspaces.base import WorkspaceRef


class SessionLineageStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    DISCARDED = "discarded"


class AgentSessionLineageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lineage_id: str
    task_id: str
    subtask_id: str
    attempt_id: int
    candidate_id: str | None = None
    node_id: str
    backend_id: str
    policy: SessionPolicy
    session_ref: BackendSessionRef
    parent_session_ref: BackendSessionRef | None = None
    workspace_ref: str
    workspace_base_revision: str | None = None
    graph_hash: str
    state_version: int
    status: str = SessionLineageStatus.COMPLETED.value
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    lineage_reason: str = ""


def lineage_identity_key(record: AgentSessionLineageRecord) -> str:
    parent = (
        record.parent_session_ref.session_id if record.parent_session_ref else ""
    )
    cand = record.candidate_id or "main"
    return "|".join(
        [
            record.task_id,
            record.subtask_id,
            str(record.attempt_id),
            cand,
            record.node_id,
            record.backend_id,
            record.session_ref.session_id,
            parent,
            record.workspace_ref,
        ]
    )


def make_lineage_id(
    *,
    task_id: str,
    subtask_id: str,
    attempt_id: int,
    candidate_id: str | None,
    node_id: str,
    backend_id: str,
    session_id: str,
) -> str:
    cand = candidate_id or "main"
    return (
        f"{task_id}:{subtask_id}:{attempt_id}:{cand}:"
        f"{node_id}:{backend_id}:{session_id}"
    )


def append_lineage_records(
    existing: list[AgentSessionLineageRecord],
    new_records: list[AgentSessionLineageRecord],
) -> list[AgentSessionLineageRecord]:
    """Append with deterministic deduplication by identity key."""
    seen = {lineage_identity_key(r) for r in existing}
    out = list(existing)
    for record in new_records:
        key = lineage_identity_key(record)
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


class SessionParentResolutionError(ValueError):
    """Fail-closed parent session resolution."""


class SessionLineageResolver:
    """Resolve parent sessions by exact node/subtask/attempt lineage."""

    def resolve_parent(
        self,
        *,
        state: TaskExecutionState,
        subtask_id: str,
        node_id: str,
        backend_id: str,
        attempt_id: int | None = None,
        candidate_id: str | None = None,
    ) -> BackendSessionRef | None:
        matches: list[tuple[BackendSessionRef, str]] = []

        # Prefer durable lineage records (exact).
        for record in state.session_lineage_records or []:
            if record.subtask_id != subtask_id:
                continue
            if record.node_id != node_id:
                continue
            if record.backend_id != backend_id:
                continue
            if attempt_id is not None and record.attempt_id != attempt_id:
                continue
            if candidate_id is not None and record.candidate_id != candidate_id:
                continue
            matches.append((record.session_ref, f"lineage:{record.lineage_id}"))

        # Fall back to subtask backend_sessions (initial attempt).
        sub = state.subtasks.get(subtask_id)
        if sub is not None:
            for sess in sub.backend_sessions:
                if sess.node_id != node_id:
                    continue
                if sess.backend_id != backend_id:
                    continue
                if attempt_id is not None and sess.attempt_id != attempt_id:
                    continue
                if candidate_id is not None and sess.candidate_id != candidate_id:
                    continue
                matches.append(
                    (
                        sess.session_ref,
                        f"backend_session:{sess.node_id}:{sess.attempt_id}",
                    )
                )

        # Deduplicate by session_id.
        by_id: dict[str, BackendSessionRef] = {}
        for ref, _src in matches:
            by_id.setdefault(ref.session_id, ref)

        if not by_id:
            return None
        if len(by_id) > 1:
            raise SessionParentResolutionError(
                f"ambiguous parent sessions for "
                f"subtask={subtask_id!r} node={node_id!r} backend={backend_id!r}: "
                f"{sorted(by_id)}"
            )
        return next(iter(by_id.values()))

    def resolve_failed_initial_parent(
        self,
        *,
        state: TaskExecutionState,
        subtask_id: str,
        node_id: str,
        backend_id: str,
    ) -> BackendSessionRef | None:
        """Default parent: failed initial attempt session for the exact node."""
        sub = state.subtasks.get(subtask_id)
        if sub is None:
            return None
        # Prefer main (non-candidate) sessions for the node.
        mains = [
            s
            for s in sub.backend_sessions
            if s.node_id == node_id
            and s.backend_id == backend_id
            and s.candidate_id is None
        ]
        if not mains:
            return self.resolve_parent(
                state=state,
                subtask_id=subtask_id,
                node_id=node_id,
                backend_id=backend_id,
                candidate_id=None,
            )
        # Latest attempt wins.
        mains = sorted(mains, key=lambda s: s.attempt_id)
        return mains[-1].session_ref


class SessionWorkspaceCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compatible: bool
    reason: str | None = None


def validate_session_workspace_binding(
    directive: NodeSessionDirective,
    parent_lineage: AgentSessionLineageRecord | None,
    candidate_workspace: WorkspaceRef,
    *,
    canonical_workspace_ref: str | None = None,
    other_candidate_workspaces: set[str] | None = None,
) -> SessionWorkspaceCompatibility:
    """Ensure RESUME/FORK sessions bind only to the candidate's isolated workspace."""
    ws_path = str(candidate_workspace.path)
    if not ws_path:
        return SessionWorkspaceCompatibility(
            compatible=False, reason="candidate workspace path is empty"
        )
    if canonical_workspace_ref and ws_path == canonical_workspace_ref:
        return SessionWorkspaceCompatibility(
            compatible=False,
            reason="candidate workspace must not be the canonical workspace",
        )
    others = other_candidate_workspaces or set()
    if ws_path in others:
        return SessionWorkspaceCompatibility(
            compatible=False,
            reason="candidate workspace is shared with another candidate",
        )
    if directive.policy is SessionPolicy.FRESH:
        return SessionWorkspaceCompatibility(compatible=True)

    if directive.require_parent_session and directive.source_session_ref is None:
        return SessionWorkspaceCompatibility(
            compatible=False,
            reason="RESUME/FORK requires a validated parent session reference",
        )
    if directive.source_session_ref is None:
        return SessionWorkspaceCompatibility(
            compatible=False,
            reason="RESUME/FORK missing source_session_ref",
        )
    if parent_lineage is not None:
        if parent_lineage.node_id != (directive.source_node_id or directive.node_id):
            return SessionWorkspaceCompatibility(
                compatible=False,
                reason="parent lineage node_id does not match directive source node",
            )
        if parent_lineage.backend_id != directive.backend_id:
            return SessionWorkspaceCompatibility(
                compatible=False,
                reason="parent lineage backend_id mismatch",
            )
        if (
            parent_lineage.workspace_base_revision
            and candidate_workspace.base_revision
            and parent_lineage.workspace_base_revision
            != candidate_workspace.base_revision
            and directive.workspace_binding == "require_same_base_revision"
        ):
            return SessionWorkspaceCompatibility(
                compatible=False,
                reason="repository base revision incompatible with parent lineage",
            )
    return SessionWorkspaceCompatibility(compatible=True)


def records_from_backend_sessions(
    *,
    sessions: list[BackendSessionRecord],
    task_id: str,
    subtask_id: str,
    workspace_ref: str,
    graph_hash: str,
    state_version: int,
    policy: SessionPolicy = SessionPolicy.FRESH,
    lineage_reason: str = "captured_from_backend_session",
    workspace_base_revision: str | None = None,
) -> list[AgentSessionLineageRecord]:
    out: list[AgentSessionLineageRecord] = []
    for sess in sessions:
        out.append(
            AgentSessionLineageRecord(
                lineage_id=make_lineage_id(
                    task_id=task_id,
                    subtask_id=subtask_id,
                    attempt_id=sess.attempt_id,
                    candidate_id=sess.candidate_id,
                    node_id=sess.node_id,
                    backend_id=sess.backend_id,
                    session_id=sess.session_ref.session_id,
                ),
                task_id=task_id,
                subtask_id=subtask_id,
                attempt_id=sess.attempt_id,
                candidate_id=sess.candidate_id,
                node_id=sess.node_id,
                backend_id=sess.backend_id,
                policy=policy,
                session_ref=sess.session_ref,
                parent_session_ref=(
                    BackendSessionRef(
                        backend_id=sess.backend_id,
                        session_id=sess.session_ref.parent_session_id,
                        parent_session_id=None,
                    )
                    if sess.session_ref.parent_session_id
                    else None
                ),
                workspace_ref=workspace_ref,
                workspace_base_revision=workspace_base_revision,
                graph_hash=graph_hash,
                state_version=state_version,
                lineage_reason=lineage_reason,
            )
        )
    return out


def coerce_lineage_records(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise ValueError(
        "session_lineage_records must be list[AgentSessionLineageRecord]; "
        f"got {type(value).__name__}"
    )
