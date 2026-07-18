"""Deterministic context budget packing for final delivery units."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.compiler import CompiledCommunicationPlan
from orchestra.communication.payload import PayloadContract
from orchestra.communication.projection import PayloadProjectionResult
from orchestra.ir.artifacts import ArtifactEnvelope


class ContextBudgetError(RuntimeError):
    """Required delivery unit cannot fit in the target context budget."""


class ContextBudgetInfeasibleReason(StrEnum):
    CONTEXT_BUDGET_INFEASIBLE = "context_budget_infeasible"


class FinalDeliveryUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_unit_id: str
    target_subtask_id: str
    target_slot: str
    source_payload_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    artifact: ArtifactEnvelope
    estimated_tokens: int
    required: bool = False
    priority: int = 100
    aggregation_rule_id: str | None = None


class FinalDeliveryBudgetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_subtask_id: str
    max_tokens: int
    used_tokens: int
    included_unit_ids: list[str] = Field(default_factory=list)
    omitted_unit_ids: list[str] = Field(default_factory=list)
    overflowed: bool = False


class ContextBudgetResult(BaseModel):
    """Legacy projection-based budget result (kept for compatibility)."""

    model_config = ConfigDict(extra="forbid")

    target_subtask_id: str
    max_tokens: int
    used_tokens: int
    included_payload_ids: list[str] = Field(default_factory=list)
    omitted_payload_ids: list[str] = Field(default_factory=list)
    overflowed: bool = False


def _unit_sort_key(unit: FinalDeliveryUnit) -> tuple[int, int, str]:
    required = 0 if unit.required else 1
    return (required, unit.priority, unit.delivery_unit_id)


def pack_final_delivery_units(
    *,
    target_subtask_id: str,
    max_tokens: int,
    units: list[FinalDeliveryUnit],
    fail_closed_on_required: bool = True,
) -> FinalDeliveryBudgetResult:
    ordered = sorted(units, key=_unit_sort_key)
    used = 0
    included: list[str] = []
    omitted: list[str] = []
    overflowed = False
    for unit in ordered:
        if used + unit.estimated_tokens <= max_tokens:
            included.append(unit.delivery_unit_id)
            used += unit.estimated_tokens
            continue
        overflowed = True
        if unit.required and fail_closed_on_required:
            raise ContextBudgetError(
                f"CONTEXT_BUDGET_INFEASIBLE: required delivery unit "
                f"{unit.delivery_unit_id} ({unit.estimated_tokens} tokens) "
                f"does not fit budget {max_tokens} for {target_subtask_id}"
            )
        omitted.append(unit.delivery_unit_id)
    return FinalDeliveryBudgetResult(
        target_subtask_id=target_subtask_id,
        max_tokens=max_tokens,
        used_tokens=used,
        included_unit_ids=included,
        omitted_unit_ids=omitted,
        overflowed=overflowed,
    )


def _payload_priority(contract: PayloadContract) -> tuple[int, int, str]:
    required = 0 if contract.is_required() else 1
    priority = int(contract.metadata.get("priority", 100))
    return (required, priority, contract.payload_id)


def pack_context_budget(
    *,
    target_subtask_id: str,
    compiled: CompiledCommunicationPlan,
    projections: list[PayloadProjectionResult],
    fail_closed_on_required: bool = True,
) -> ContextBudgetResult:
    """Legacy helper: pack individual projections (prefer pack_final_delivery_units)."""
    max_tokens = int(compiled.context_budgets.get(target_subtask_id, 10_000_000))
    by_id = {p.payload_id: p for p in projections}
    contracts = list(compiled.payloads_by_target.get(target_subtask_id, []))
    ordered = sorted(contracts, key=_payload_priority)

    used = 0
    included: list[str] = []
    omitted: list[str] = []
    overflowed = False

    for contract in ordered:
        proj = by_id.get(contract.payload_id)
        if proj is None:
            continue
        required = contract.is_required()
        if used + proj.estimated_tokens <= max_tokens:
            included.append(contract.payload_id)
            used += proj.estimated_tokens
            continue
        overflowed = True
        if required and fail_closed_on_required:
            raise ContextBudgetError(
                f"CONTEXT_BUDGET_INFEASIBLE: required payload "
                f"{contract.payload_id} ({proj.estimated_tokens} tokens) "
                f"does not fit budget {max_tokens} for {target_subtask_id}"
            )
        omitted.append(contract.payload_id)

    return ContextBudgetResult(
        target_subtask_id=target_subtask_id,
        max_tokens=max_tokens,
        used_tokens=used,
        included_payload_ids=included,
        omitted_payload_ids=omitted,
        overflowed=overflowed,
    )
