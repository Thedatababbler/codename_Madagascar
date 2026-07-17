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
FailureDiagnosis
        ↓
≤ K LocalCandidates (rule-based)
        ↓
capability negotiation (fail closed)
        ↓
per-candidate workspace + backend run
        ↓
authoritative AdaMAS harness
        ↓
deterministic selector
        ↓
atomic commit + checkpoint
```

## 3. Shared interface (CodeAgent & Codex)

Both backends implement `AgentBackend` and declare `BackendCapabilities`.
`FastLoopController` never branches on `backend_id`. Differences are expressed
only via capabilities and adapter behavior.

## 4. Capability negotiation

`validate_candidate_against_capabilities` rejects unsupported session policies
and edits **before** execution. There is **no silent downgrade**
(`FORK`/`RESUME` → `FRESH`). Compatibility rejects are not counted as model
failures.

## 5. Why CodeAgent is FRESH-only

CodeAgent does not expose a durable session state AdaMAS can safely resume or
fork across candidate workspaces. M4-A therefore declares:

```text
supported_session_policies = {FRESH}
supports_session_state = false
```

## 6. Codex RESUME / FORK (optional M4-B)

Schema includes `SessionPolicy.RESUME` / `FORK`, but Codex M4-A only advertises
`FRESH`. Stateful candidates must wait until the adapter can prove safe
workspace rebinding and session lineage. Until then, RESUME/FORK candidates are
filtered out.

## 7. Candidate workspace isolation

Layout:

```text
<run_dir>/tasks/<task_id>/subtasks/<subtask_id>/
  base/repo/
  candidates/<candidate_id>/repo/
```

All candidates fork from an immutable base revision (git worktree or local
clone). Writable candidates never share a directory. Absolute workspace paths
are execution state and are not part of the reproducible graph hash.

## 8. Authoritative harness

Only `HarnessExecutor` / `repository_test_harness` decides pass/fail. Agent
self-reported “tests passed” text has no adjudicatory power. Candidates share
the same public command, timeout, and fixture snapshot.

## 9. Failure diagnosis

Maps harness / output-contract / tool / timeout / model / infra / invalid-config
outcomes to `FailureDiagnosis` with `retryable`, `recommended_edit_types`, and
`infrastructure_related`. Infra failures allow at most one retry **without**
graph edits and are not scored as graph quality.

## 10. Bounded search budget

`FastLoopBudget` caps candidates, backend calls, tokens, cost, wall time, and
attempts. Exhaustion sets `FastLoopState.exhausted = true` and fails the
subtask.

## 11. Deterministic selector

Not Pareto. Order: valid + harness-pass → higher public quality → lower cost →
fewer stability incidents → lower latency → candidate id.

Search cost and selected execution cost are recorded separately.

## 12. Checkpoint / resume

`FastLoopState` is stored on `TaskExecutionState.fast_loop_states`. Each
candidate completion checkpoints. On resume, completed candidates are not
re-run; `RUNNING` resets to restartable `PENDING`; committed winners are not
re-committed. Graph checkpoints use candidate-scoped task ids to avoid hash
drift against the base attempt.

## 13. Boundary vs M5 / M6

| In M4 | Not in M4 |
|-------|-----------|
| Local Fast Loop | Slow global update (M5) |
| ReadySubtaskScheduler | Future communication rewrite |
| Deterministic winner | Pareto archive (M6) |
| FRESH candidates | Hidden-test tuning |

## 14. Subprocess harness security boundary

`repository_test_harness` redacts secret env vars and kills the process group on
timeout, but remains **trusted fixture only; not a security boundary**. External
benchmark repos require OfficialLCBSandbox / low-privilege worker / container
before use.
