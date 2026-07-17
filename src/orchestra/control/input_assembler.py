"""Assemble declared subtask inputs from root + dependency artifacts."""

from __future__ import annotations

from orchestra.backends.base import ArtifactRef
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import SubtaskSpec, TaskPlan
from orchestra.ir.artifacts import ArtifactBundle, ArtifactEnvelope
from orchestra.storage.artifacts import ArtifactStore


class SubtaskInputAssembler:
    """Deterministic M4 input assembly (no whole-state prompt dumping)."""

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store

    async def assemble(
        self,
        *,
        task_plan: TaskPlan,
        task_state: TaskExecutionState,
        subtask: SubtaskSpec,
        root_artifacts: ArtifactBundle,
    ) -> ArtifactBundle:
        del task_plan  # structure unused; dependency list comes from subtask
        slots: dict[str, ArtifactEnvelope] = dict(root_artifacts.slots)

        # Explicit SubtaskSpec.input_artifacts selectors (when artifact_id set).
        for ref in subtask.input_artifacts:
            if not ref.artifact_id:
                continue
            try:
                art = await self.artifact_store.get(ref.artifact_id)
            except KeyError:
                continue
            slots[ref.slot] = art

        # Direct dependency committed artifacts by slot.
        for dep_id in subtask.dependencies:
            dep = task_state.subtasks.get(dep_id)
            if dep is None or dep.status is not SubtaskStatus.COMMITTED:
                continue
            for ref in dep.committed_artifacts:
                if ref.slot in slots and ref.slot in root_artifacts.slots:
                    # Keep root unless selector overwrote; dependency fills gaps
                    # and always overlays non-root slots.
                    pass
                slots[ref.slot] = await self.artifact_store.get(ref.artifact_id)
            if dep.final_output_artifact_id and "upstream" not in slots:
                slots["upstream"] = await self.artifact_store.get(
                    dep.final_output_artifact_id
                )

        return ArtifactBundle(slots=slots)

    def declared_dependency_artifact_ids(
        self,
        *,
        task_state: TaskExecutionState,
        subtask: SubtaskSpec,
    ) -> list[str]:
        ids: list[str] = []
        for dep_id in sorted(subtask.dependencies):
            dep = task_state.subtasks.get(dep_id)
            if dep is None:
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
