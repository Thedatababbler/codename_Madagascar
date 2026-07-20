"""Apply GlobalEdit lists onto TaskPlan / CommunicationPlan / SchedulingPolicy."""

from __future__ import annotations

from orchestra.communication.delta import CommunicationTargetResolver
from orchestra.communication.plan import CommunicationPlan
from orchestra.control.slow_loop.schemas import (
    AggregationRuleEdit,
    ContextBudgetEdit,
    GlobalEdit,
    PendingBackendAssignmentEdit,
    PendingGraphTemplateEdit,
    PendingPriorityEdit,
    RemovePayloadContractEdit,
    SchedulingConcurrencyEdit,
    SerializationGroupEdit,
    TaskSchedulingPolicy,
    UpsertDeliveryRuleEdit,
    UpsertPayloadContractEdit,
)
from orchestra.decomposition.schemas import SubtaskSpec, TaskPlan


def _proposed_comm(
    *,
    contracts: list,
    delivery: list,
    budgets: dict,
    aggregations: list,
    version: int,
) -> CommunicationPlan:
    return CommunicationPlan(
        payload_contracts=contracts,
        context_budgets=budgets,
        delivery_schedule=delivery,
        aggregation_rules=aggregations,
        version=version,
    )


def apply_global_edits(
    *,
    task_plan: TaskPlan,
    communication_plan: CommunicationPlan,
    scheduling_policy: TaskSchedulingPolicy,
    edits: list[GlobalEdit],
    eligible_subtask_ids: set[str],
) -> tuple[TaskPlan, CommunicationPlan, TaskSchedulingPolicy, list[str]]:
    """Return new plans; rejected_edit_ids lists edits that touched ineligible ids."""
    rejected: list[str] = []
    contracts = list(communication_plan.payload_contracts)
    delivery = list(communication_plan.delivery_schedule)
    budgets = dict(communication_plan.context_budgets)
    aggregations = list(communication_plan.aggregation_rules)
    subtasks = [s.model_copy(deep=True) for s in task_plan.subtasks]
    by_id = {s.subtask_id: i for i, s in enumerate(subtasks)}
    policy = scheduling_policy.model_copy(deep=True)
    meta_updates: dict[str, dict] = {}
    resolver = CommunicationTargetResolver()

    for edit in edits:
        if isinstance(edit, UpsertPayloadContractEdit):
            if edit.contract.target_subtask_id not in eligible_subtask_ids:
                rejected.append(edit.type)
                continue
            # Changing an existing payload that targets an ineligible id is also
            # rejected even if the new target is eligible.
            existing = next(
                (c for c in contracts if c.payload_id == edit.contract.payload_id),
                None,
            )
            if (
                existing is not None
                and existing.target_subtask_id not in eligible_subtask_ids
            ):
                rejected.append(edit.type)
                continue
            contracts = [
                c for c in contracts if c.payload_id != edit.contract.payload_id
            ]
            contracts.append(edit.contract)
        elif isinstance(edit, RemovePayloadContractEdit):
            existing = next(
                (c for c in contracts if c.payload_id == edit.payload_id),
                None,
            )
            if existing is None:
                continue
            if existing.target_subtask_id not in eligible_subtask_ids:
                rejected.append(edit.type)
                continue
            contracts = [c for c in contracts if c.payload_id != edit.payload_id]
            delivery = [d for d in delivery if d.payload_id != edit.payload_id]
        elif isinstance(edit, UpsertDeliveryRuleEdit):
            proposed = _proposed_comm(
                contracts=contracts,
                delivery=delivery,
                budgets=budgets,
                aggregations=aggregations,
                version=communication_plan.version,
            )
            # Resolve against batch-proposed contracts (may include prior upserts).
            target = None
            for c in contracts:
                if c.payload_id == edit.rule.payload_id:
                    target = c.target_subtask_id
                    break
            if target is None:
                target = resolver.delivery_rule_target(edit.rule.rule_id, proposed)
            # Also reject if replacing an existing rule that targeted ineligible.
            old = next((d for d in delivery if d.rule_id == edit.rule.rule_id), None)
            if old is not None:
                old_tgt = resolver.payload_target(old.payload_id, proposed)
                if old_tgt is not None and old_tgt not in eligible_subtask_ids:
                    rejected.append(edit.type)
                    continue
            if target is None or target not in eligible_subtask_ids:
                rejected.append(edit.type)
                continue
            delivery = [d for d in delivery if d.rule_id != edit.rule.rule_id]
            delivery.append(edit.rule)
        elif isinstance(edit, ContextBudgetEdit):
            if edit.target_subtask_id not in eligible_subtask_ids:
                rejected.append(edit.type)
                continue
            budgets[edit.target_subtask_id] = edit.max_tokens
        elif isinstance(edit, AggregationRuleEdit):
            proposed = _proposed_comm(
                contracts=contracts,
                delivery=delivery,
                budgets=budgets,
                aggregations=[
                    a for a in aggregations if a.rule_id != edit.rule.rule_id
                ]
                + [edit.rule],
                version=communication_plan.version,
            )
            try:
                targets = resolver.aggregation_rule_targets(
                    edit.rule.rule_id, proposed, require_unique=False
                )
            except Exception:  # noqa: BLE001
                rejected.append(edit.type)
                continue
            if not targets or any(t not in eligible_subtask_ids for t in targets):
                rejected.append(edit.type)
                continue
            # Existing aggregation with ineligible targets cannot be replaced.
            old = next(
                (a for a in aggregations if a.rule_id == edit.rule.rule_id),
                None,
            )
            if old is not None:
                old_targets = resolver.aggregation_rule_targets(
                    old.rule_id,
                    _proposed_comm(
                        contracts=contracts,
                        delivery=delivery,
                        budgets=budgets,
                        aggregations=aggregations,
                        version=communication_plan.version,
                    ),
                )
                if any(t not in eligible_subtask_ids for t in old_targets):
                    rejected.append(edit.type)
                    continue
            aggregations = [a for a in aggregations if a.rule_id != edit.rule.rule_id]
            aggregations.append(edit.rule)
        elif isinstance(edit, PendingGraphTemplateEdit):
            if edit.subtask_id not in eligible_subtask_ids:
                rejected.append(edit.type)
                continue
            idx = by_id[edit.subtask_id]
            old = subtasks[idx]
            subtasks[idx] = SubtaskSpec(
                subtask_id=old.subtask_id,
                title=old.title,
                objective=old.objective,
                dependencies=list(old.dependencies),
                input_artifacts=list(old.input_artifacts),
                expected_outputs=list(old.expected_outputs),
                keystone_harness_id=old.keystone_harness_id,
                local_graph_template=edit.graph_template_id,
                budget=old.budget,
                priority=old.priority,
                metadata=dict(old.metadata),
            )
        elif isinstance(edit, PendingBackendAssignmentEdit):
            if edit.subtask_id not in eligible_subtask_ids:
                rejected.append(edit.type)
                continue
            meta_updates.setdefault(edit.subtask_id, {})
            meta_updates[edit.subtask_id]["backend_assignment"] = {
                "node_id": edit.node_id,
                "backend_id": edit.backend_id,
                "model_name": edit.model_name,
            }
        elif isinstance(edit, PendingPriorityEdit):
            if edit.subtask_id not in eligible_subtask_ids:
                rejected.append(edit.type)
                continue
            idx = by_id[edit.subtask_id]
            old = subtasks[idx]
            subtasks[idx] = SubtaskSpec(
                subtask_id=old.subtask_id,
                title=old.title,
                objective=old.objective,
                dependencies=list(old.dependencies),
                input_artifacts=list(old.input_artifacts),
                expected_outputs=list(old.expected_outputs),
                keystone_harness_id=old.keystone_harness_id,
                local_graph_template=old.local_graph_template,
                budget=old.budget,
                priority=edit.priority,
                metadata=dict(old.metadata),
            )
            policy.priority_overrides[edit.subtask_id] = edit.priority
        elif isinstance(edit, SchedulingConcurrencyEdit):
            policy.max_concurrent_subtasks = max(1, edit.max_concurrent_subtasks)
            policy.version += 1
        elif isinstance(edit, SerializationGroupEdit):
            if any(s not in eligible_subtask_ids for s in edit.subtask_ids):
                rejected.append(edit.type)
                continue
            groups = [
                g
                for g in policy.serialization_groups
                if set(g) != set(edit.subtask_ids)
            ]
            groups.append(sorted(edit.subtask_ids))
            policy.serialization_groups = groups
            policy.version += 1

    for sid, meta in meta_updates.items():
        idx = by_id[sid]
        old = subtasks[idx]
        new_meta = dict(old.metadata)
        new_meta.update(meta)
        subtasks[idx] = SubtaskSpec(
            subtask_id=old.subtask_id,
            title=old.title,
            objective=old.objective,
            dependencies=list(old.dependencies),
            input_artifacts=list(old.input_artifacts),
            expected_outputs=list(old.expected_outputs),
            keystone_harness_id=old.keystone_harness_id,
            local_graph_template=old.local_graph_template,
            budget=old.budget,
            priority=old.priority,
            metadata=new_meta,
        )

    new_comm = CommunicationPlan(
        payload_contracts=contracts,
        context_budgets=budgets,
        delivery_schedule=delivery,
        aggregation_rules=aggregations,
        version=communication_plan.version + 1,
    )
    new_plan = TaskPlan(
        task_id=task_plan.task_id,
        subtasks=subtasks,
        final_aggregation=task_plan.final_aggregation,
        communication_plan=new_comm,
        decomposition_rationale=task_plan.decomposition_rationale,
        plan_version=task_plan.plan_version + 1,
        decomposition_status=task_plan.decomposition_status,
        metadata=dict(task_plan.metadata),
    )
    return new_plan, new_comm, policy, rejected
