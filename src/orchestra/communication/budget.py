"""Deterministic context budget packing for delivered payloads."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.compiler import CompiledCommunicationPlan
from orchestra.communication.payload import PayloadContract
from orchestra.communication.projection import PayloadProjectionResult


class ContextBudgetError(RuntimeError):
    """Required payload cannot fit in the target context budget."""


class ContextBudgetInfeasibleReason(StrEnum):
    CONTEXT_BUDGET_INFEASIBLE = "context_budget_infeasible"


class ContextBudgetResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_subtask_id: str
    max_tokens: int
    used_tokens: int
    included_payload_ids: list[str] = Field(default_factory=list)
    omitted_payload_ids: list[str] = Field(default_factory=list)
    overflowed: bool = False


def _payload_priority(contract: PayloadContract) -> tuple[int, int, str]:
    """Lower tuple sorts first. Required (metadata) before optional."""
    required = 0 if bool(contract.metadata.get("required")) else 1
    priority = int(contract.metadata.get("priority", 100))
    return (required, priority, contract.payload_id)


def pack_context_budget(
    *,
    target_subtask_id: str,
    compiled: CompiledCommunicationPlan,
    projections: list[PayloadProjectionResult],
    fail_closed_on_required: bool = True,
) -> ContextBudgetResult:
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
        required = bool(contract.metadata.get("required"))
        if used + proj.estimated_tokens <= max_tokens:
            included.append(contract.payload_id)
            used += proj.estimated_tokens
            continue
        overflowed = True
        if required:
            if fail_closed_on_required:
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
