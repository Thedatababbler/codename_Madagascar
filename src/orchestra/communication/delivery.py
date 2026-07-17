"""Execute CommunicationPlan deliveries for a target subtask."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.budget import ContextBudgetResult, pack_context_budget
from orchestra.communication.compiler import (
    CommunicationPlanCompiler,
    CompiledCommunicationPlan,
)
from orchestra.communication.ledger import (
    DeliveryRecord,
    DeliveryStatus,
    already_delivered,
)
from orchestra.communication.plan import CommunicationPlan
from orchestra.communication.projection import PayloadProjectionResult, project_payload
from orchestra.control.task_state import SubtaskStatus, TaskExecutionState
from orchestra.decomposition.schemas import TaskPlan
from orchestra.ir.artifacts import ArtifactEnvelope
from orchestra.storage.artifacts import ArtifactStore


class DeliveryBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_subtask_id: str
    projections: list[PayloadProjectionResult] = Field(default_factory=list)
    budget: ContextBudgetResult | None = None
    new_records: list[DeliveryRecord] = Field(default_factory=list)
    delivered_slots: dict[str, ArtifactEnvelope] = Field(default_factory=dict)


class CommunicationDeliveryEngine:
    def __init__(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store
        self.compiler = CommunicationPlanCompiler()

    def compile(
        self,
        *,
        task_plan: TaskPlan,
        communication_plan: CommunicationPlan,
        task_state: TaskExecutionState,
    ) -> CompiledCommunicationPlan:
        # Only past terminal statuses are forbidden delivery targets.
        # READY/LEASED/PENDING targets are valid (delivery happens at launch).
        completed = {
            sid
            for sid, sub in task_state.subtasks.items()
            if sub.status
            in {
                SubtaskStatus.COMMITTED,
                SubtaskStatus.FAILED,
                SubtaskStatus.SKIPPED,
            }
        }
        return self.compiler.compile(
            task_plan=task_plan,
            communication_plan=communication_plan,
            completed_subtask_ids=completed,
        )

    async def deliver_for_target(
        self,
        *,
        task_plan: TaskPlan,
        task_state: TaskExecutionState,
        communication_plan: CommunicationPlan,
        target_subtask_id: str,
    ) -> DeliveryBatchResult:
        compiled = self.compile(
            task_plan=task_plan,
            communication_plan=communication_plan,
            task_state=task_state,
        )
        contracts = compiled.payloads_by_target.get(target_subtask_id, [])
        projections: list[PayloadProjectionResult] = []
        ledger = list(getattr(task_state, "delivery_ledger", []) or [])

        for contract in sorted(contracts, key=lambda c: c.payload_id):
            source = task_state.subtasks.get(contract.source_subtask_id)
            if source is None or source.status is not SubtaskStatus.COMMITTED:
                continue
            # Prefer committed_artifacts matching type; else final_output.
            source_art: ArtifactEnvelope | None = None
            for ref in source.committed_artifacts:
                if (
                    not contract.artifact_type
                    or ref.artifact_type == contract.artifact_type
                ):
                    source_art = await self.artifact_store.get(ref.artifact_id)
                    break
            if source_art is None and source.final_output_artifact_id:
                source_art = await self.artifact_store.get(
                    source.final_output_artifact_id
                )
            if source_art is None:
                continue
            if already_delivered(
                ledger,
                payload_id=contract.payload_id,
                communication_plan_version=compiled.version,
                source_artifact_id=source_art.artifact_id,
                target_subtask_id=target_subtask_id,
            ):
                # Re-fetch prior projection for assembler if stored.
                continue
            proj = project_payload(
                contract=contract,
                source=source_art,
                task_id=task_state.task_id,
            )
            await self.artifact_store.put(proj.projected_artifact)
            projections.append(proj)

        budget = pack_context_budget(
            target_subtask_id=target_subtask_id,
            compiled=compiled,
            projections=projections,
        )
        included = set(budget.included_payload_ids)
        new_records: list[DeliveryRecord] = []
        delivered_slots: dict[str, ArtifactEnvelope] = {}
        for proj in projections:
            if proj.payload_id not in included:
                continue
            contract = compiled.payloads_by_id[proj.payload_id]
            slot = str(
                contract.metadata.get("slot") or f"comm:{contract.payload_id}"
            )
            delivered_slots[slot] = proj.projected_artifact
            rec = DeliveryRecord(
                delivery_id=f"del-{uuid.uuid4().hex[:12]}",
                communication_plan_version=compiled.version,
                payload_id=proj.payload_id,
                source_subtask_id=contract.source_subtask_id,
                target_subtask_id=target_subtask_id,
                source_artifact_id=proj.source_artifact_id,
                projected_artifact_id=proj.projected_artifact.artifact_id,
                delivered_at_state_version=task_state.state_version,
                status=DeliveryStatus.DELIVERED,
            )
            new_records.append(rec)

        return DeliveryBatchResult(
            target_subtask_id=target_subtask_id,
            projections=projections,
            budget=budget,
            new_records=new_records,
            delivered_slots=delivered_slots,
        )
