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
→ SlowLoopController.maybe_update
→ checkpoint → recompute ready → acquire leases
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

## 7. Communication delivery

`CommunicationPlanCompiler` → payload projection (field select + deterministic
truncation) → context budget packing → `DeliveryRecord` ledger.

Precedence for `SubtaskInputAssembler`:

```text
explicit > communication_delivery > implicit_dependency > root
```

Only **committed** source artifacts may be delivered. Resume does not
re-deliver the same `(plan_version, payload, source_artifact, target)`.

## 8. Plan revision

Immutable snapshots under `plan_revisions/<revision_id>/`. Active plan swaps
only after validation + atomic apply + checkpoint. Failure policy default:
`keep_previous_plan`.

## 9. Scheduling policy

`TaskSchedulingPolicy` (concurrency, priorities, serialization groups) applies
to the **next** wave only. Hard-capped by scheduler constructor limits.

## 10. Backend compatibility

No `if backend_id == ...` in SlowLoopController. Assignments go through
allowlists + capability/model pools. CodeAgent and Codex remain FRESH-only.

## 11. Hidden evaluator isolation

Private/hidden artifact types cannot appear in PayloadContracts, observation,
diagnosis, delivery ledger, or telemetry payloads.

## 12. Boundary vs M6

M5 does **not** implement Pareto archive, genetic search, large candidate
execution, re-decomposition, or committed rollback.
