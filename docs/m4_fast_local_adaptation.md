# Milestone 4 — Backend-Agnostic Fast Local Adaptation

## 1. Problem

When a subtask’s current local graph fails, AdaMAS must diagnose the failure,
generate a **bounded** set of local edit candidates, evaluate each in an
**isolated workspace** with the same AdaMAS harness, deterministically select a
winner, and **atomically commit** it — without Slow Loop / Pareto / global
topology search.

## 2. Fast Loop lifecycle

```text
execute current subtask graph
        ↓
backend or harness failure
        ↓
FailureDiagnosis (failed_node_ids / primary_failed_node_id)
        ↓
≤ K LocalCandidates (rule-based; rejected candidates audited)
        ↓
capability negotiation (fail closed → CandidateStatus.REJECTED)
        ↓
budget gates (generate / queue / launch / infra-retry / commit)
        ↓
per-candidate workspace + backend run
        ↓
authoritative AdaMAS harness (candidate workspace)
        ↓
deterministic selector
        ↓
PREPARE: apply WorkspaceChangeSet to canonical/base
        ↓
authoritative harness (post-apply canonical) — must pass
        ↓
COMMIT: finalize + checkpoint  (else rollback → COMMIT_VALIDATION_FAILED)
```

## 3. Ownership boundary

```text
AdaMAS owns:
- task/subtask scheduling
- local graph adaptation
- candidate workspace isolation
- harness evaluation
- winner selection
- atomic commit
- checkpoint
- upstream artifact / repository revision propagation

CodeAgent/Codex own only:
- the inner execution loop of one agent node
```

`FastLoopController` never branches on `backend_id`. Differences are expressed
only via `BackendCapabilities` and adapters.

## 4. Cost accounting (single source of truth)

Each candidate’s **execution** cost lives only on `CandidateRecord.cost`.

| Field | Meaning |
|-------|---------|
| `initial_execution_cost` | Cost of the attempt that triggered Fast Loop |
| `candidate_search_cost` / `FastLoopState.search_cost` | **Derived** sum of all candidate costs (including losers) |
| `selected_execution_cost` | Winner’s execution cost |
| `control_plane_cost` | Control-plane extras only (deterministic generator → 0) |
| `total_method_cost` | `initial + search + control_plane` |

Do **not** persist a separately incremented `search_cost` that duplicates
candidate costs. Loser costs remain visible after discard.

## 5. Winner commit & WorkspaceChangeSet

```python
class WorkspaceChangeSet(BaseModel):
    tracked_patch: str
    modified_files: list[str]
    added_untracked_files: list[str]
    deleted_files: list[str]
    renamed_files: list[RenameRecord]
    file_manifest_hash: str
```

Changed files are parsed via `git status --porcelain=v1 -z`. Commit steps:

1. Verify canonical base revision has not drifted
2. Apply tracked patch
3. Safely copy untracked files (path confinement; no `../`; no symlink escape; no `.git`)
4. Apply deletes / renames
5. Verify changed-file manifest vs candidate
6. Re-run authoritative harness on post-apply workspace
7. Finalize git commit only if harness passes; else `git reset --hard` + `clean -fdx`

## 6. Canonical post-apply harness

Winner pass in a **candidate** workspace is not sufficient. After apply:

- harness re-runs on the post-apply canonical/base tree;
- failure → full rollback; winner `COMMIT_VALIDATION_FAILED`; subtask not `COMMITTED`;
- success → finalize + checkpoint.

Harness remains: independent process/group, timeout kills the tree, API keys
redacted, fixed cwd/command, trusted marker, agent self-report ignored.
Still: **trusted fixtures only; not a security boundary**.

## 7. Upstream data propagation (deterministic M4 minimum)

### Artifacts

`SubtaskInputAssembler` builds inputs with **deterministic precedence**
(low → high):

1. Root task default artifacts  
2. Implicit direct-dependency **committed** artifacts  
3. Explicit `SubtaskSpec.input_artifacts` selectors  

Equal-priority collisions on the same slot fail closed
(`SubtaskInputAssemblyError` / `ArtifactSlotConflictPolicy.ERROR`).
`candidate_artifacts` never propagate; only `committed_artifacts` after a
successful coordinator canonical commit. Slot lineage is recorded in
`ArtifactBundle.slot_sources`.

Whole `TaskExecutionState` is never dumped into prompts.

### Repository revisions

Each task maintains a **canonical task workspace**. Workers **never** mutate it.
They return a `WorkspaceChangeSet` + `base_canonical_revision`. The scheduler
coordinator, under `_state_lock`, runs a **staging transaction**:

```text
canonical HEAD
  → commit_staging/<subtask>-<attempt>/
  → apply changeset (strict; controlled reapply on revision drift)
  → authoritative harness
  → promote staging → canonical  (else leave canonical untouched)
  → WorkspaceCommitRecord + COMMITTED
```

Parallel siblings fork from the same base revision. If S1 commits first
(R0→R1), S2’s changeset is reapplied onto R1 staging. Content conflicts →
`CANONICAL_MERGE_CONFLICT` (subtask **not** `COMMITTED`). Harness failure on
staging → `CANONICAL_VALIDATION_FAILED`. `COMMITTED` means the change is in
canonical.

## 8. Concurrent checkpoint strategy

Workers must not mutate the shared `TaskExecutionState` or write shared
checkpoints (`FastLoopController.persist_checkpoints=False` under the
scheduler).

```text
subtask runner → SubtaskExecutionResult (local only)
scheduler (_state_lock) → transactional canonical commit
                       → merge state → increment state_version
                       → atomic checkpoint (unique tmp + fsync)
```

`WorkspaceCommitRecord` enables idempotent recovery (already-COMMITTED
records are not re-applied). `max_concurrent_subtasks > 1` requires
`allow_concurrent_subtasks=True` (fail closed otherwise). Default remains
serial (`1`).

## 9. Failed-node diagnosis

`FailureDiagnosis` carries `failed_node_ids` and `primary_failed_node_id`.
Priority: node result → backend node → output-contract producer → harness
lineage → `None`. Candidate generator edits the primary failed **agent** node;
it must not silently edit the first agent in the graph.

## 10. Rejected candidate auditing

Capability / budget rejects become `CandidateRecord` with
`status=REJECTED` and `CandidateRejectionReason` (e.g.
`UNSUPPORTED_SESSION_POLICY`, `BUDGET_EXCEEDED`). They:

- do not start backends;
- do not count as model failures;
- do not add execution cost;
- persist through checkpoint / telemetry.

## 11. Budget enforcement

`FastLoopBudgetTracker` (monotonic clock) gates:

- candidate generation
- queue admission
- backend launch (with reservation for parallel starts)
- infrastructure retry
- winner commit

Exhaustion sets `FastLoopState.exhausted = true`. Unstarted candidates are
`REJECTED` with `BUDGET_EXCEEDED`.

## 12. Model pools (no hardcoded aliases)

```yaml
backend_model_pools:
  smolagents_code:
    allowed_models: [model_a, model_b]
  codex_sdk:
    allowed_models: [model_c, model_d]
```

`BackendModelPool` supplies the only alternate models for `ModelOverrideEdit`.
Empty pool → no model-override candidate. No cross-provider guessing.

## 13. CodeAgent / Codex

| Backend | M4-A session policy | Notes |
|---------|---------------------|-------|
| CodeAgent | `FRESH` only | No managed_agents; no canonical commits; no harness bypass |
| Codex | `FRESH` default; **Hybrid** optional | `NodeSessionDirective` per node; RESUME/FORK capability-gated; see `docs/hybrid_codex_fast_loop.md` |

## 14. Workspace isolation

- each candidate: independent writable workspace
- canonical base immutable during candidate evaluation
- losers cannot pollute winner or canonical
- uncommitted candidate changes never propagate to siblings/downstream

## 15. Checkpoint / resume

`FastLoopState` lives on `TaskExecutionState.fast_loop_states` with
`state_version`. Completed candidates are not re-run; `RUNNING` resets to
restartable `PENDING`; committed winners are not re-committed.

## 16. Boundary vs M5 / M6

M4 is **correctness-complete** for Fast Loop + concurrent canonical commit +
deterministic artifact assembly. Still out of scope:

| In M4 | **Not** in M4 |
|-------|----------------|
| Local Fast Loop + hardening above | Slow global update (M5) |
| ReadySubtaskScheduler + locked merge | Communication optimization / re-decomposition |
| Deterministic winner + post-apply harness | Pareto archive / GA (M6) |
| FRESH candidates + model pools | Hidden-test optimization |
| Deterministic artifact/repo propagation | Cross-subtask smart routing |
| Serialized canonical commit transactions | Hidden-test optimization |

Hybrid Codex (branch `hybrid_codex`): RESUME/FORK/FRESH critic via
`HybridCodexLocalCandidateGenerator` + `NodeSessionDirective` — see
`docs/hybrid_codex_fast_loop.md`.

## 17. Subprocess harness security boundary

`repository_test_harness` / `run_authoritative_harness_command` redact secrets
and kill process groups on timeout, but remain **trusted fixture only; not a
security boundary**. External benchmark repos require OfficialLCBSandbox /
low-privilege worker / container before use.
