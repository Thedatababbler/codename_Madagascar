"""Assemble declared subtask inputs from root + dependency + communication."""

from __future__ import annotations

from enum import StrEnum

from orchestra.backends.base import ArtifactRef
from orchestra.communication.delivery import CommunicationDeliveryEngine
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


class CommunicationDeliveryBlocked(SubtaskInputAssemblyError):
    """Required communication payload cannot be delivered; target must not start."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class SubtaskInputAssembler:
    """Deterministic input assembly with M5 communication delivery."""

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        conflict_policy: ArtifactSlotConflictPolicy = ArtifactSlotConflictPolicy.ERROR,
        delivery_engine: CommunicationDeliveryEngine | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.conflict_policy = conflict_policy
        self.delivery_engine = delivery_engine or CommunicationDeliveryEngine(
            artifact_store
        )

    async def assemble(
        self,
        *,
        task_plan: TaskPlan,
        task_state: TaskExecutionState,
        subtask: SubtaskSpec,
        root_artifacts: ArtifactBundle,
    ) -> ArtifactBundle:
        slots: dict[str, ArtifactEnvelope] = dict(root_artifacts.slots)
        sources: dict[str, ArtifactSourceRef] = {
            slot: ArtifactSourceRef(
                source="root",
                producer_subtask_id=None,
                artifact_id=art.artifact_id,
            )
            for slot, art in root_artifacts.slots.items()
        }

        # 2. Implicit committed dependency artifacts
        implicit = await self._collect_committed_dependency_artifacts(
            task_state=task_state, subtask=subtask
        )
        self._merge_rows(
            slots,
            sources,
            implicit,
            source_label="implicit_dependency",
        )

        # 3. Communication-delivered payloads (rule-driven; required fail-closed)
        delivery = await self.delivery_engine.deliver_for_target(
            task_plan=task_plan,
            task_state=task_state,
            communication_plan=task_state.communication_plan,
            target_subtask_id=subtask.subtask_id,
        )
        if delivery.audit_records:
            task_state.delivery_ledger.extend(delivery.audit_records)
        if delivery.blocked:
            reason = (
                delivery.block_reason.value
                if delivery.block_reason is not None
                else "required_delivery_blocked"
            )
            raise CommunicationDeliveryBlocked(
                f"communication delivery blocked for {subtask.subtask_id}: {reason}",
                reason=reason,
            )
        if delivery.new_records:
            task_state.delivery_ledger.extend(delivery.new_records)
        self._merge_rows(
            slots,
            sources,
            [
                (slot, "communication", art)
                for slot, art in sorted(delivery.delivered_slots.items())
            ],
            source_label="communication_delivery",
            allow_equal_priority_overwrite=True,
        )

        # 4. Explicit SubtaskSpec selectors
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
        rows: list[tuple[str, str, ArtifactEnvelope]] = []
        for dep_id in sorted(subtask.dependencies):
            dep = task_state.subtasks.get(dep_id)
            if dep is None or dep.status is not SubtaskStatus.COMMITTED:
                continue
            for ref in sorted(
                dep.committed_artifacts, key=lambda r: (r.slot, r.artifact_id)
            ):
                art = await self.artifact_store.get(ref.artifact_id)
                rows.append((ref.slot, dep_id, art))
            if dep.final_output_artifact_id:
                already = {r.artifact_id for r in dep.committed_artifacts}
                if dep.final_output_artifact_id not in already:
                    art = await self.artifact_store.get(dep.final_output_artifact_id)
                    slot = (
                        "upstream"
                        if len(subtask.dependencies) == 1
                        else f"upstream:{dep_id}"
                    )
                    rows.append((slot, dep_id, art))
        return rows

    def _merge_rows(
        self,
        slots: dict[str, ArtifactEnvelope],
        sources: dict[str, ArtifactSourceRef],
        rows: list[tuple[str, str, ArtifactEnvelope]],
        *,
        source_label: str,
        allow_equal_priority_overwrite: bool = False,
    ) -> None:
        by_slot: dict[str, list[tuple[str, ArtifactEnvelope]]] = {}
        for slot, producer, art in rows:
            by_slot.setdefault(slot, []).append((producer, art))

        for slot in sorted(by_slot):
            candidates = by_slot[slot]
            if len(candidates) > 1 and not allow_equal_priority_overwrite:
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
                        candidates = [
                            min(candidates, key=lambda x: (x[0], x[1].artifact_id))
                        ]
                    else:
                        candidates = [
                            max(candidates, key=lambda x: (x[0], x[1].artifact_id))
                        ]
            producer, art = candidates[0]
            slots[slot] = art
            sources[slot] = ArtifactSourceRef(
                source=source_label,
                producer_subtask_id=producer if producer != "communication" else None,
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
                continue
            try:
                art = await self.artifact_store.get(ref.artifact_id)
            except KeyError as exc:
                raise SubtaskInputAssemblyError(
                    f"explicit selector missing artifact {ref.artifact_id} "
                    f"for slot {ref.slot}"
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
