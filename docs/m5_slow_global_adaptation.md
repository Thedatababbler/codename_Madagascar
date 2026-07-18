# Milestone 5 — Backend-Agnostic Slow Global Adaptation

## 1. Fast Loop vs Slow Loop

| | Fast Loop (M4) | Slow Loop (M5) |
|--|----------------|----------------|
| Scope | Current failing subtask | Future unleased subtasks |
| Mutates | Local graph candidates | TaskPlan / CommunicationPlan / scheduling |
| Repository | Candidate workspaces + canonical commit | **Never** touches canonical repo |
| Past | Immutable after commit | Strictly immutable |

```text
Past is immutable. Only future execution may change.
```

## 2. Trigger (safe checkpoints only)

After a wave of coordinator commits + checkpoint, before the next lease/start:

```text
commit wave → checkpoint → release leases
→ prepare/apply Slow Loop revision transaction
→ checkpoint → recompute deliverability → assemble future inputs
→ materialize future graphs → acquire leases → execute next wave
```

Triggers include context pressure, budget pressure, repeated failures,
canonical conflicts, missing payloads, and periodic commit checkpoints.

## 3. Future-only + subtask lease

Slow Loop may edit only:

```text
PENDING + UNLEASED
READY + UNLEASED
```

Forbidden: LEASED / RUNNING / AWAITING_CANONICAL_COMMIT / COMMITTED / FAILED /
SKIPPED. Invalid edits reject the **entire** revision (no partial apply).

## 4. GlobalObservation

Built only from public telemetry, committed artifacts, task state, public
harness results, and costs. Never from private/hidden evaluators.

## 5. GlobalDiagnosis (rule-based)

Deterministic reasons: context/budget pressure, missing/redundant payload,
backend instability, scheduling contention, canonical conflict risk.

## 6. GlobalEdit types

Payload/delivery/context/aggregation edits; pending graph template; pending
backend/model assignment (allowlist); priority; concurrency; serialization
groups. M5 forbids subtask add/delete, DAG rewiring, committed rollback,
harness/private evaluator changes, RESUME/FORK, managed_agents.

## 7. DeliveryRule is mandatory for delivery

`PayloadContract` alone never delivers. Flow:

```text
target → contracts → enabled DeliveryRule → evaluate trigger/condition
→ project → pack budget → ledger → target slot
```

Supported triggers: `ON_SOURCE_COMMIT`, `BEFORE_TARGET_START`.
`MANUAL` is unsupported (fail closed). Disabled rules and
`condition.kind=never` do not deliver; condition failures are audited as
`SKIPPED_CONDITION_FALSE` (not model failures).

Required payload with no enabled rule → `REQUIRED_RULE_MISSING` (target blocked).

## 8. Delivery ledger resume / replay

`DeliveryRecord` stores `rule_id`, `projected_artifact_id`,
`projected_artifact_hash`, `target_slot`, token estimates.

Idempotency key:

```text
communication_plan_version + rule_id + payload_id
+ source_artifact_id + target_subtask_id + target_slot
```

On resume: reload the persisted projected artifact from ArtifactStore
(hash-checked). Do **not** re-project and do **not** append a second ledger
row. Missing/mismatched projected artifact → `DELIVERY_LEDGER_CORRUPTION`.

## 9. Required payload fail-closed

`PayloadContract.required` (typed; metadata `required` migrated) and
`required_fields`:

- required source not committed / artifact missing → target stays READY/PENDING,
  `communication_block_reason` set, **no lease/backend**
- required field missing → `PROJECTION_INFEASIBLE` / `REQUIRED_FIELD_MISSING`
- required payload cannot fit context budget → `CONTEXT_BUDGET_INFEASIBLE`

These are communication delivery failures, not model failures.

## 10. Strict recursive token projection

Deterministic `TokenEstimator` (char/4, versioned). Recursive trim for
dict/list/string. Final assertion:

```text
final_estimated_tokens <= contract.max_tokens
```

Otherwise `PayloadProjectionInfeasible`. Optional fields may be omitted;
required fields cannot be silently dropped.

## 11. Deterministic aggregation

Strategies: `LIST`, `MERGE_DICT_FAIL_ON_CONFLICT`, `CONCAT_TEXT`.
No LLM summarization. Same target slot with multiple payloads and no
`AggregationRule` → fail closed. Dict key conflicts → `AGGREGATION_CONFLICT`.

## 12. Communication dependency + cycle validation

Required payloads require source to be a DAG ancestor of target.
Combined graph of task dependencies + required communication edges must be
acyclic.

## 13. Future graph materialization

`FutureGraphMaterializer` loads the immutable base graph, applies pending
graph/backend/model assignments via capability + allowlist pools, validates
with the graph compiler, and writes a run-scoped YAML snapshot:

```text
<run_dir>/plan_revisions/<revision_id>/graphs/<subtask_id>.yaml
```

Scheduler executes the materialized snapshot (or live materialization from
`backend_assignment` metadata). Original repo YAML is never modified.
CodeAgent/Codex remain FRESH-only; unsupported capabilities reject the revision.

## 14. Declared future delta validation

`FuturePlanValidator` re-runs `apply_global_edits()` and requires the
candidate proposed state to match. Extra mutations
(`objective` / `dependencies` / harness / undeclared fields) →
`UNDECLARED_PLAN_MUTATION`.

## 15. Atomic revision + checkpoint transaction

Controller prepares `PreparedSlowLoopRevision` (staging, VALIDATED).
Coordinator commit:

```text
stage revision files → write APPLIED revision.json → save checkpoint
→ atomic rename revision dir → swap in-memory state
```

Checkpoint failure keeps the previous plan active; staging is not promoted.
Checkpoint stores `active_plan_revision_id`, `active_plan_hash`,
`active_communication_hash`. Mismatch on recovery →
`PLAN_REVISION_CORRUPTION`.

## 16. Scheduler next-wave consumption

Next wave uses active scheduling policy, CommunicationPlan, delivery engine,
and materialized graphs. Required delivery blocks prevent lease/execution.
Leased/current wave specs are frozen during revision.

## 17. Hidden evaluator isolation

Private/hidden artifact types cannot appear in PayloadContracts, observation,
diagnosis, projection, or GlobalEdit evidence.

## 18. Boundary vs M6

M5 does **not** implement Pareto archive, weighted objective optimization,
genetic algorithms, global candidate execution search, LLM-generated
arbitrary TaskPlans, subtask add/delete, re-decomposition, dependency
rewiring, committed rollback, Codex native subagents, CodeAgent
`managed_agents`, or Codex RESUME/FORK.

**M5 correctness-complete; M6 not started.**
