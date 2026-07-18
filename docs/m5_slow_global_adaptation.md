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
`condition.kind=never` does not deliver. Semantics:

- optional + condition false → audit `SKIPPED_CONDITION_FALSE`; target may continue
- required + condition false → `REQUIRED_CONDITION_UNSATISFIED`; target blocked

Multiple enabled rules are evaluated by `priority desc, rule_id asc`; the first
satisfied rule delivers (fallback). If none satisfy and the contract is
required → block.

Required payload with no enabled rule → `REQUIRED_RULE_MISSING` (target blocked).

`required=True` means the target must possess the payload before start; it is
not “conditionally required”.

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

## 11. Deterministic aggregation + FinalDeliveryUnit budget

Strategies: `LIST`, `MERGE_DICT_FAIL_ON_CONFLICT`, `CONCAT_TEXT`.
No LLM summarization. Same target slot with multiple payloads and no
`AggregationRule` → fail closed. Dict key conflicts → `AGGREGATION_CONFLICT`.

Context budget packs **FinalDeliveryUnit**s (single projection or one
aggregated artifact). Token estimates are for the final injected artifact
payload. An aggregate that exceeds the target budget is omitted/blocked and
never written to `delivered_slots`.

## 12. Communication dependency + cycle validation

Required payloads require source to be a DAG ancestor of target.
Combined graph of task dependencies + required communication edges must be
acyclic.

## 13. Future graph materialization (final logical paths)

`FutureGraphMaterializer` writes physically to staging, but stores **final**
logical paths in the TaskPlan / execution config:

```text
physical write:  plan_revisions/.staging-rev-…/graphs/s2.yaml
stored path:     plan_revisions/rev-…/graphs/s2.yaml
```

Active checkpoints never contain `.staging-` paths. Missing or hash-mismatched
active snapshots → `PLAN_REVISION_GRAPH_*` fail-closed (no silent fallback to
the original repo YAML).

## 14. Declared future delta validation

`FuturePlanValidator` re-runs `apply_global_edits()` and requires the
candidate proposed state to match. Extra mutations
(`objective` / `dependencies` / harness / undeclared fields) →
`UNDECLARED_PLAN_MUTATION`.

## 15. Atomic revision + checkpoint transaction

Crash-safe order (active pointer never references a missing revision):

```text
1. prepare staging (+ fsync)
2. atomic rename staging → final revision (PROMOTED / orphan-safe)
3. fsync plan_revisions parent
4. save checkpoint (activates pointer)
5. swap live in-memory state
```

Checkpoint is the sole source of truth for which revision is active.
Promote-then-crash leaves an **orphan** final revision; old checkpoint stays
active. Hash-identical retry is idempotent; hash collision →
`PLAN_REVISION_ID_COLLISION`.

## 16. Scheduler next-wave consumption

```text
mark dependency-ready → communication preflight (persist projections)
→ blocked targets keep READY + communication_block_reason (no lease)
→ only deliverable targets acquire leases → execute
```

Blocked targets do not consume concurrency slots. Upstream commit clears
blocks so the next iteration can re-preflight.

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
