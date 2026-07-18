"""Deterministic aggregation rules for combining delivered payloads."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestra.communication.ledger import DeliveryFailureReason
from orchestra.communication.projection import estimate_tokens
from orchestra.ir.artifacts import ArtifactEnvelope, create_artifact
from orchestra.schemas.artifacts import FinalAnswerArtifact


class AggregationStrategy(StrEnum):
    LIST = "list"
    MERGE_DICT_FAIL_ON_CONFLICT = "merge_dict_fail_on_conflict"
    CONCAT_TEXT = "concat_text"
    # Legacy aliases kept for TaskPlan.final_aggregation compatibility.
    IDENTITY = "identity"
    SELECT_FIRST = "select_first"
    MERGE_ARTIFACTS = "merge_artifacts"


class AggregationConflictError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = DeliveryFailureReason.AGGREGATION_CONFLICT


class AggregationRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    source_subtask_ids: list[str] = Field(default_factory=list)
    source_payload_ids: list[str] = Field(default_factory=list)
    strategy: AggregationStrategy | str = AggregationStrategy.LIST
    target_slot: str = ""
    delimiter: str = "\n"
    max_tokens: int = 4096
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("strategy", mode="before")
    @classmethod
    def _coerce_strategy(cls, value: Any) -> Any:
        if isinstance(value, AggregationStrategy):
            return value
        return str(value)


class AggregationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: Literal["identity", "select_first", "merge_artifacts"] = "identity"
    terminal_subtask_id: str | None = None
    rules: list[AggregationRule] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AggregationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_payload_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    projected_artifacts: list[ArtifactEnvelope] = Field(default_factory=list)


class AggregationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aggregation_rule_id: str
    aggregated_artifact: ArtifactEnvelope
    source_artifact_ids: list[str] = Field(default_factory=list)
    estimated_tokens: int
    target_slot: str = ""


def _extract_fields(art: ArtifactEnvelope) -> Any:
    payload = art.payload
    answer = payload.get("answer")
    if isinstance(answer, str):
        try:
            parsed = json.loads(answer)
            if isinstance(parsed, dict) and "fields" in parsed:
                return parsed["fields"]
            return parsed
        except json.JSONDecodeError:
            return answer
    return payload


def aggregate_payloads(
    *,
    rule: AggregationRule,
    inputs: AggregationInput,
    task_id: str,
) -> AggregationResult:
    strategy = AggregationStrategy(str(rule.strategy))
    arts = list(inputs.projected_artifacts)
    # Stable order: rule source_payload_ids, then payload_id / artifact_id tie-break.
    order_index = {pid: i for i, pid in enumerate(rule.source_payload_ids)}
    indexed = list(
        zip(
            inputs.source_payload_ids or [f"p{i}" for i in range(len(arts))],
            inputs.source_artifact_ids or [a.artifact_id for a in arts],
            arts,
            strict=False,
        )
    )
    indexed.sort(
        key=lambda row: (
            order_index.get(row[0], 10_000),
            row[0],
            row[1],
        )
    )
    ordered_arts = [row[2] for row in indexed]
    source_ids = [row[1] for row in indexed]
    fields_list = [_extract_fields(a) for a in ordered_arts]

    if strategy in {AggregationStrategy.IDENTITY, AggregationStrategy.SELECT_FIRST}:
        if not ordered_arts:
            raise AggregationConflictError("aggregation has no inputs")
        aggregated_value = fields_list[0]
    elif strategy in {
        AggregationStrategy.LIST,
        AggregationStrategy.MERGE_ARTIFACTS,
    }:
        aggregated_value = fields_list
    elif strategy == AggregationStrategy.MERGE_DICT_FAIL_ON_CONFLICT:
        merged: dict[str, Any] = {}
        for fields in fields_list:
            if not isinstance(fields, dict):
                raise AggregationConflictError(
                    "AGGREGATION_CONFLICT: MERGE_DICT requires dict fields"
                )
            for key in sorted(fields):
                if key in merged and merged[key] != fields[key]:
                    raise AggregationConflictError(
                        f"AGGREGATION_CONFLICT: key {key!r} has conflicting values"
                    )
                merged[key] = fields[key]
        aggregated_value = merged
    elif strategy == AggregationStrategy.CONCAT_TEXT:
        parts: list[str] = []
        for fields in fields_list:
            if isinstance(fields, str):
                parts.append(fields)
            elif isinstance(fields, dict):
                # Prefer answer/text fields; else stable JSON.
                if "answer" in fields and isinstance(fields["answer"], str):
                    parts.append(fields["answer"])
                elif "text" in fields and isinstance(fields["text"], str):
                    parts.append(fields["text"])
                else:
                    parts.append(
                        json.dumps(fields, sort_keys=True, ensure_ascii=False)
                    )
            else:
                parts.append(json.dumps(fields, sort_keys=True, ensure_ascii=False))
        aggregated_value = rule.delimiter.join(parts)
    else:
        raise AggregationConflictError(f"unsupported aggregation strategy {strategy}")

    def _answer_blob(value: Any) -> dict[str, Any]:
        return {
            "aggregation_rule_id": rule.rule_id,
            "strategy": str(strategy),
            "value": value,
            "source_artifact_ids": source_ids,
        }

    tokens = estimate_tokens(_answer_blob(aggregated_value))
    if tokens > rule.max_tokens:
        if strategy == AggregationStrategy.CONCAT_TEXT and isinstance(
            aggregated_value, str
        ):
            while (
                estimate_tokens(_answer_blob(aggregated_value)) > rule.max_tokens
                and len(aggregated_value) > 16
            ):
                aggregated_value = aggregated_value[: len(aggregated_value) // 2] + "…"
            tokens = estimate_tokens(_answer_blob(aggregated_value))
        elif strategy == AggregationStrategy.LIST and isinstance(aggregated_value, list):
            while (
                estimate_tokens(_answer_blob(aggregated_value)) > rule.max_tokens
                and len(aggregated_value) > 1
            ):
                aggregated_value = aggregated_value[:-1]
            tokens = estimate_tokens(_answer_blob(aggregated_value))
        if tokens > rule.max_tokens:
            raise AggregationConflictError(
                f"AGGREGATION_CONFLICT: aggregated payload exceeds max_tokens "
                f"{rule.max_tokens}"
            )

    answer = FinalAnswerArtifact(
        answer=json.dumps(
            _answer_blob(aggregated_value),
            sort_keys=True,
            ensure_ascii=False,
        ),
        source_node=f"agg:{rule.rule_id}",
        raw_output=None,
        extraction_status="ok",
    )
    art = create_artifact(
        answer,
        producer_node_id=f"agg:{rule.rule_id}",
        task_id=task_id,
        parent_artifact_ids=source_ids,
    )
    # Token estimate must match the artifact payload actually injected into slots.
    tokens = estimate_tokens(art.payload)
    if tokens > rule.max_tokens:
        raise AggregationConflictError(
            f"AGGREGATION_CONFLICT: final artifact exceeds max_tokens "
            f"{rule.max_tokens}"
        )
    slot = rule.target_slot or str(
        rule.metadata.get("slot") or f"agg:{rule.rule_id}"
    )
    return AggregationResult(
        aggregation_rule_id=rule.rule_id,
        aggregated_artifact=art,
        source_artifact_ids=source_ids,
        estimated_tokens=tokens,
        target_slot=slot,
    )
