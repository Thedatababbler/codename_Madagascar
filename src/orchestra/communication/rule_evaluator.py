"""Evaluate DeliveryRule triggers and conditions."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from orchestra.communication.payload import DeliveryRule, DeliveryTrigger, PayloadContract
from orchestra.control.task_state import SubtaskStatus


class DeliveryRuleDecision(StrEnum):
    SATISFIED = "satisfied"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_CONDITION_FALSE = "skipped_condition_false"
    SKIPPED_TRIGGER_NOT_MET = "skipped_trigger_not_met"
    UNSUPPORTED_TRIGGER = "unsupported_trigger"


class DeliveryEvaluationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    state_version: int
    source_subtask_id: str
    target_subtask_id: str
    source_status: SubtaskStatus
    target_status: SubtaskStatus
    source_artifact_ids: list[str] = Field(default_factory=list)
    active_communication_version: int
    target_leased: bool = False
    source_has_committed_artifact: bool = False


class DeliveryRuleEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    payload_id: str
    decision: DeliveryRuleDecision
    reason: str = ""


class DeliveryRuleEvaluator:
    def evaluate(
        self,
        *,
        rule: DeliveryRule,
        contract: PayloadContract,
        context: DeliveryEvaluationContext,
    ) -> DeliveryRuleEvaluationResult:
        del contract  # contract available for future condition extensions
        if not rule.enabled:
            return DeliveryRuleEvaluationResult(
                rule_id=rule.rule_id,
                payload_id=rule.payload_id,
                decision=DeliveryRuleDecision.SKIPPED_DISABLED,
                reason="rule disabled",
            )
        if rule.condition is not None and rule.condition.kind == "never":
            return DeliveryRuleEvaluationResult(
                rule_id=rule.rule_id,
                payload_id=rule.payload_id,
                decision=DeliveryRuleDecision.SKIPPED_CONDITION_FALSE,
                reason="SKIPPED_CONDITION_FALSE",
            )
        if rule.trigger is DeliveryTrigger.MANUAL:
            return DeliveryRuleEvaluationResult(
                rule_id=rule.rule_id,
                payload_id=rule.payload_id,
                decision=DeliveryRuleDecision.UNSUPPORTED_TRIGGER,
                reason="MANUAL trigger unsupported in M5",
            )
        if rule.trigger is DeliveryTrigger.ON_SOURCE_COMMIT:
            ok = (
                context.source_status is SubtaskStatus.COMMITTED
                and context.source_has_committed_artifact
                and context.target_status
                in {SubtaskStatus.PENDING, SubtaskStatus.READY}
                and not context.target_leased
            )
            if not ok:
                return DeliveryRuleEvaluationResult(
                    rule_id=rule.rule_id,
                    payload_id=rule.payload_id,
                    decision=DeliveryRuleDecision.SKIPPED_TRIGGER_NOT_MET,
                    reason="ON_SOURCE_COMMIT prerequisites unmet",
                )
            return DeliveryRuleEvaluationResult(
                rule_id=rule.rule_id,
                payload_id=rule.payload_id,
                decision=DeliveryRuleDecision.SATISFIED,
            )
        if rule.trigger is DeliveryTrigger.BEFORE_TARGET_START:
            ok = (
                context.target_status
                in {SubtaskStatus.PENDING, SubtaskStatus.READY}
                and not context.target_leased
                and context.source_status is SubtaskStatus.COMMITTED
                and context.source_has_committed_artifact
            )
            if not ok:
                return DeliveryRuleEvaluationResult(
                    rule_id=rule.rule_id,
                    payload_id=rule.payload_id,
                    decision=DeliveryRuleDecision.SKIPPED_TRIGGER_NOT_MET,
                    reason="BEFORE_TARGET_START prerequisites unmet",
                )
            return DeliveryRuleEvaluationResult(
                rule_id=rule.rule_id,
                payload_id=rule.payload_id,
                decision=DeliveryRuleDecision.SATISFIED,
            )
        return DeliveryRuleEvaluationResult(
            rule_id=rule.rule_id,
            payload_id=rule.payload_id,
            decision=DeliveryRuleDecision.UNSUPPORTED_TRIGGER,
            reason=f"unknown trigger {rule.trigger}",
        )
