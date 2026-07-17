"""Assemble declared subtask inputs from root + dependency artifacts."""

from __future__ import annotations

from enum import StrEnum

from orchestra.backends.base import ArtifactRef
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import ArtifactBundle, ArtifactEnvelope, ArtifactSourceRef
from orchestra.storage.artifacts import ArtifactStore


class ArtifactSlotConflictPolicy(StrEnum):
    ERROR = "error"
    FIRST = "first"
    LAST = "last"


class SubtaskInputAssemblyError(RuntimeError):
    """Fail-closed input assembly (missing required / equal-priority conflict)."""


class SubtaskInputAssembler:
    """Deterministic M4 input assembly (no whole-state prompt dumping)."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        conflict_policy: ArtifactSlotConflictPolicy = ArtifactSlotConflictPolicy.ERROR,
    ) -> None:
        self.artifact_store = artifact_store
        self.conflict_policy = conflict_policy

    async def assemble(
        self,
        *,
        task_plan: TaskPlan,
        task_state: TaskExecutionState,
        subtask: SubtaskSpec,
        root_artifacts: ArtifactBundle,
    ) -> ArtifactBundle:
        del task_plan
        slots: dict[str, ArtifactEnvelope] = dict(root_artifacts.slots)
        sources: dict[str, ArtifactSourceRef] = {
            slot: ArtifactSourceRef(
                source="root",
                producer_subtask_id=None,
                artifact_id=art.artifact_id,
            )
            for slot, art in root_artifacts.slots.items()
        }

        implicit = await self._collect_committed_dependency_artifacts(
            task_state=task_state, subtask=subtask
        )
        self._merge_implicit_dependencies(slots, sources, implicit)

        await self._apply_explicit_selectors(
            slots, sources, subtask.input_artifacts, task_state
        )
        return ArtifactBundle(slots=slots, slot_sources=sources)

    async def _collect_committed_dependency_artifacts(
        self,
        *,
        task_state: TaskExecutionState,
        subtask: SubtaskSpec,
    ) -> list[tuple[str, str, ArtifactEnvelope]]:
        """Return (slot, producer_subtask_id, artifact) in deterministic order."""
        rows: list[tuple[str, str, ArtifactEnvelope]] = []
        for dep_id in sorted(subtask.dependencies):
            dep = task_state.subtasks.get(dep_id)
            if dep is None or dep.status is not SubtaskStatus.COMMITTED:
                continue
            # Only committed_artifacts — never candidate_artifacts.
            for ref in sorted(dep.committed_artifacts, key=lambda r: (r.slot, r.artifact_id)):
                art = await self.artifact_store.get(ref.artifact_id)
                rows.append((ref.slot, dep_id, art))
            if dep.final_output_artifact_id:
                # Implicit upstream slot only when not already listed.
                already = {r.artifact_id for r in dep.committed_artifacts}
                if dep.final_output_artifact_id not in already:
                    art = await self.artifact_store.get(dep.final_output_artifact_id)
                    # Multi-dep: namespaced slots avoid equal-priority collisions.
                    slot = (
                        "upstream"
                        if len(subtask.dependencies) == 1
                        else f"upstream:{dep_id}"
                    )
                    rows.append((slot, dep_id, art))
        return rows

    def _merge_implicit_dependencies(
        self,
        slots: dict[str, ArtifactEnvelope],
        sources: dict[str, ArtifactSourceRef],
        rows: list[tuple[str, str, ArtifactEnvelope]],
    ) -> None:
        # Group by slot among equal-priority implicit deps.
        by_slot: dict[str, list[tuple[str, ArtifactEnvelope]]] = {}
        for slot, producer, art in rows:
            by_slot.setdefault(slot, []).append((producer, art))

        for slot in sorted(by_slot):
            candidates = by_slot[slot]
            if len(candidates) > 1:
                # Distinct artifact IDs at equal priority → conflict.
                ids = {a.artifact_id for _, a in candidates}
                if len(ids) > 1:
                    if self.conflict_policy is ArtifactSlotConflictPolicy.ERROR:
                        detail = ", ".join(
                            f"{pid}:{a.artifact_id}" for pid, a in candidates
                        )
                        raise SubtaskInputAssemblyError(
                            f"equal-priority artifact slot conflict on {slot!r}: {detail}"
                        )
                    if self.conflict_policy is ArtifactSlotConflictPolicy.FIRST:
                        candidates = [min(candidates, key=lambda x: (x[0], x[1].artifact_id))]
                    else:
                        candidates = [max(candidates, key=lambda x: (x[0], x[1].artifact_id))]
            producer, art = candidates[0]
            # Implicit dependency overrides root; equal-priority already resolved.
            slots[slot] = art
            sources[slot] = ArtifactSourceRef(
                source="implicit_dependency",
                producer_subtask_id=producer,
                artifact_id=art.artifact_id,
            )

    async def _apply_explicit_selectors(
        self,
        slots: dict[str, ArtifactEnvelope],
        sources: dict[str, ArtifactSourceRef],
        selectors: list[ArtifactRef],
        task_state: TaskExecutionState,
    ) -> None:
        del task_state
        for ref in selectors:
            if not ref.artifact_id:
                # Placeholder selector (type-only) — does not override.
                continue
            try:
                art = await self.artifact_store.get(ref.artifact_id)
            except KeyError as exc:
                raise SubtaskInputAssemblyError(
                    f"explicit selector missing artifact {ref.artifact_id} for slot {ref.slot}"
                ) from exc
            slots[ref.slot] = art
            sources[ref.slot] = ArtifactSourceRef(
                source="explicit_selector",
                producer_subtask_id=None,
                artifact_id=art.artifact_id,
            )

    def declared_dependency_artifact_ids(
        self,
        *,
        task_state: TaskExecutionState,
        subtask: SubtaskSpec,
    ) -> list[str]:
        ids: list[str] = []
        for dep_id in sorted(subtask.dependencies):
            dep = task_state.subtasks.get(dep_id)
            if dep is None or dep.status is not SubtaskStatus.COMMITTED:
                continue
            for ref in dep.committed_artifacts:
                ids.append(ref.artifact_id)
            if dep.final_output_artifact_id:
                ids.append(dep.final_output_artifact_id)
        for ref in subtask.input_artifacts:
            if ref.artifact_id:
                ids.append(ref.artifact_id)
        return ids


def artifact_refs_from_committed(
    committed: list[ArtifactRef],
) -> list[str]:
    return [a.artifact_id for a in committed]
