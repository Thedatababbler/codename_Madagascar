# Hybrid Codex Fast Loop

Hybrid Codex extends Milestone 4 Fast Loop with **session lifecycle control**
(FRESH / RESUME / FORK) while keeping **graph structure** and **harness/commit**
under AdaMAS control.

## Graph vs session control

| Layer | Owner | What it controls |
|-------|-------|------------------|
| **Graph YAML** | AdaMAS compile-time | Node topology, contracts, harness, `thread_policy: fresh` on Codex nodes |
| **Fast Loop edits** | Deterministic generator | Prompt feedback, optional verifier insertion, audit `session_policy` on nodes |
| **Node session directives** | Runtime execution | Per-node `SessionPolicy` + validated `source_session_ref` |
| **Codex SDK lifecycle** | Backend adapter | Real `thread_start` / `thread_resume` / `thread_fork` only |

Graph YAML must keep `CodexSDKBackendConfig.thread_policy: fresh`. RESUME and FORK
are **never** inferred from YAML alone; they require a `NodeSessionDirective` with
a validated parent session reference.

## Session policies

### FRESH

- Starts a new Codex thread via `thread_start`.
- No parent session reference on the `AgentRequest`.
- Used for initial attempts, independent critic/verifier nodes, and audited
  `fresh_fallback` when explicitly configured.

### RESUME

- Continues the same implementation trajectory after harness failure.
- Requires exact parent session for the **same node + subtask + backend**.
- AdaMAS passes `source_session_ref` into `AgentRequest.session_ref`; the lifecycle
  adapter calls `thread_resume(parent_thread_id, cwd=...)`.

### FORK

- Branches from a parent session to explore an alternative strategy.
- Requires the same parent resolution rules as RESUME.
- Lifecycle adapter calls `thread_fork` and **rejects fake forks** where the child
  thread id equals the parent id.

## Node session directives

`NodeSessionDirective` is the execution source of truth inside a candidate graph:

- `node_id`, `backend_id`, `policy`
- `source_session_ref` (required for RESUME/FORK)
- `source_node_id`, `source_attempt_id` (lineage audit)
- `workspace_binding` (default `candidate_isolated`)
- `require_parent_session` (fail closed when parent missing)

`LocalCandidate.session_policy` is retained for checkpoint migration and audit only.
`AgentNodeExecutor` builds `AgentRequest` from directives, not the legacy
candidate-level policy.

Mixed policies inside one candidate are supported (e.g. fresh critic verifier +
fork/resume repairer on the implementer node).

## Workspace binding

Each Fast Loop candidate receives an **isolated git workspace** forked from the
failed attempt base snapshot.

- RESUME/FORK sessions bind to the **candidate workspace**, not the canonical workspace.
- `validate_session_workspace_binding` rejects shared paths across concurrent candidates.
- Optional `require_same_base_revision` binding compares parent lineage base revision
  against the candidate workspace.

Losers cannot pollute winner or canonical workspaces.

## Parent session resolution

`SessionLineageResolver` resolves parents by exact `(subtask_id, node_id, backend_id)`:

1. Durable `session_lineage_records` (preferred)
2. Initial `subtask.backend_sessions` for the failed attempt

Ambiguous multiple matches raise `SessionParentResolutionError` (fail closed).
Sessions from other subtasks are never used as parents.

`missing_parent_policy`:

- `reject` — candidate marked `compatibility_rejected` with explicit reason (no silent FRESH label)
- `fresh_fallback` — audited FRESH candidate with explicit lineage reason

## Production SDK support

The `codex_sdk` backend uses `openai_codex.AsyncCodex`:

| API | Used for |
|-----|----------|
| `thread_start` | FRESH |
| `thread_resume` | RESUME (with cwd rebinding) |
| `thread_fork` | FORK (with cwd rebinding) |

`CodexThreadLifecycleAdapter` never fabricates parent IDs. Unsupported SDK builds
fail at capability negotiation or lifecycle open with `CodexLifecycleError`.

Catalog capabilities (`supports_resume`, `supports_fork`, cross-workspace rebind)
gate candidates **before** backend calls.

## No silent downgrade

AdaMAS rejects unsupported session policies at three layers:

1. **Capability filter** — `validate_candidate_against_capabilities`
2. **Executor** — `AgentNodeExecutor._resolve_session_directive`
3. **Lifecycle adapter** — FRESH must not carry `parent_thread_id`; fake forks forbidden

There is no automatic relabeling of RESUME/FORK candidates as FRESH.

## No native subagents

Hybrid mode does **not** enable Codex native subagents or managed agents. Optional
`AddVerifierNodeEdit` inserts an AdaMAS graph verifier node with its own FRESH
session directive — still orchestrated by AdaMAS harness and commit paths.

## Configuration

```yaml
fast_loop:
  codex_session_mode: hybrid   # fresh_only | resume_only | fork_only | hybrid
  hybrid_codex:
    enable_resume: true
    enable_fork: true
    add_fresh_critic: true
    missing_parent_policy: reject
    parent_preference:
      - failed_initial_attempt
      - last_selected_candidate
```

Default `codex_session_mode: fresh_only` preserves M4-A behavior.

## Smoke and tests

Deterministic coverage (fake AsyncCodex):

```bash
uv sync --extra codex
uv run pytest -q tests/unit/test_hybrid_codex_session.py
uv run pytest -q tests/integration/test_hybrid_codex_fast_loop.py
uv run python -m orchestra.cli.run_hybrid_codex_smoke \
  --config configs/experiments/hybrid_codex_smoke.yaml
```

Fixture repo: `tests/fixtures/codex_tiny_repo`. Graph:
`configs/graphs/codex_single_implementer.yaml`.

## Related docs

- `docs/m4_fast_local_adaptation.md` — Fast Loop controller, budgets, commit
- `docs/m3_5_codex_backend.md` — Codex SDK backend baseline
