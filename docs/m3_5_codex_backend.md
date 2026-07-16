# Milestone 3.5 — Codex Second-Backend Vertical Slice (CLOSED)

## Goal

Enable AdaMAS single-subtask `TaskPlan` execution to call the **Codex Python SDK**
as one Agent node backend. Codex edits an isolated Git workspace; AdaMAS owns
TaskPlan / SubtaskState / artifacts / checkpoints / graph runtime / repository
harness. No M4 fast loop, no multi-subtask scheduler, no Codex source changes.

**Status:** M3.5 engineering gate closed. Next milestone is **M4** (not implemented
in this slice).

## Dependency pin

| Package | Version | Notes |
|---------|---------|-------|
| `openai-codex` | **0.1.0b3** | Python SDK |
| `openai-codex-cli-bin` | **0.137.0a4** | Bundled Codex runtime (transitive) |

Install:

```bash
uv sync --extra codex
```

`pyproject.toml` sets `[tool.uv] prerelease = "allow"` because these packages are
currently published only as prereleases. Do not depend on Codex `main`.

Approval mapping (verified on `openai-codex==0.1.0b3`):

- AdaMAS YAML/config key: `approval_policy: never`
- SDK kwargs on `AsyncCodex.thread_start` / `AsyncThread.run`: **`approval_mode=`**
  (there is no public `approval_policy=` kwarg)
- Mapped value: `ApprovalMode.deny_all` → protocol `AskForApproval.never`

Auth for real smoke: `OPENAI_API_KEY` via `login_api_key`; optional
`OPENAI_BASE_URL` is passed as Codex `openai_base_url` config override (proxy /
OpenAI-compatible Responses endpoints).

## Control-plane ownership

```text
TaskDecomposer → TaskPlan → TaskExecutionState
  → SharedSubtaskGitWorkspaceManager.prepare
  → SingleSubtaskCompatibilityRunner
  → NativeAsyncRuntime
       → CodexSDKBackend (node; full contract prompt)
       → repository_test_harness (AdaMAS; trusted fixtures only)
  → task_execution.json checkpoint
```

Forbidden in M3.5 backend **YAML** config: resume, fork, steer, interrupt, review,
native subagents, danger-full-access / full_access. `AgentSessionPolicy.RESUME` /
`FORK` exist on the schema but Codex still fail-closed to `FRESH` only.

Host note: Codex `workspace_write` uses `bwrap` user namespaces. If the host
blocks that (common in some containers), set
`ADAMAS_CODEX_SANDBOX_OVERRIDE=full_access` for local smoke only. This does not
relax the YAML forbid; it is an explicit runtime escape hatch.

## Session persistence (generic)

Sessions are **not** keyed by `backend_id`. Each record is:

```text
BackendSessionRecord(node_id, backend_id, attempt_id, session_ref, candidate_id=None)
```

Collected from `NodeExecutionResult.backend_metadata["session_ref"]` after each
graph run (agent executor copies `AgentResult.session_ref` into metadata). Legacy
`dict[str, BackendSessionRef]` checkpoints are explicitly migrated via
`migrate_backend_sessions` (best-effort `node_id=backend_id`, `attempt_id=1`).

## Failure semantics

| Outcome | Subtask status | failure_reason |
|---------|----------------|----------------|
| Harness `passed=false` | `HARNESS_FAILED` | `HARNESS` |
| Model / tool / contract / timeout / infra | `FAILED` | mapped reason |
| Success + freeze | `COMMITTED` | cleared |

M3.5 does **not** auto-retry (`RETRY_PENDING` reserved for M4).

## Prompt forwarding

`render_agent_request_messages(request)` joins the full `request.messages` list
(`[SYSTEM]`, `[USER]`, …). Codex does **not** receive only the last user turn /
`rendered_context`.

## Workspace layout

```text
<run_dir>/tasks/<task_id>/workspaces/<subtask_id>/repo/
```

Kind: `SHARED_SUBTASK_WORKSPACE`. Source repo is cloned/copied; user original
repo is never mutated. Absolute paths live in execution state (`workspace_ref`),
not in TaskPlan content hash.

## Artifacts / harness security

- `RepositoryChangeArtifact`: `patch` from `git diff --binary --no-ext-diff`;
  `changed_files` from Git status; `final_response` is explanatory only.
- Empty diff with `require_git_diff=true` → `OUTPUT_CONTRACT_FAILURE`.
- `RepositoryHarnessResultArtifact`: produced only by AdaMAS harness, never by
  trusting Codex self-report.
- `repository_test_harness` supports **trusted fixtures only** (marker
  `.adamas_trusted_harness`). It runs pytest as a normal subprocess under the
  Orchestra process privileges — **not a security sandbox**. Untrusted repos are
  rejected; `ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS=1` is an explicit local override
  only. CI uses only in-repo tiny fixtures.

## Configs / CLI

| Asset | Path |
|-------|------|
| Graph | `configs/graphs/codex_single_implementer.yaml` |
| Contract | `configs/contracts/codex_implementer.yaml` |
| Plan | `configs/plans/codex_tiny_repo_single_subtask.yaml` |
| Experiment | `configs/experiments/m3_5_codex_smoke.yaml` |
| Fixture | `tests/fixtures/codex_tiny_repo/` |
| CLI | `python -m orchestra.cli.run_codex_smoke --config configs/experiments/m3_5_codex_smoke.yaml` |

## Tests / CI

- Unit: fake Codex client; prompt rendering; session schema; trusted harness guard
- Integration: harness-failure → `HARNESS_FAILED`; session persistence + reload;
  LCB official worker (no Codex credentials)
- Real Codex smoke: recorded in `EXPERIMENT_LOG.md` (`EXP-20260716-01`); not a CI gate

## Non-goals (stop after M3.5)

FastLoopController, SlowLoop, LocalEdit, candidate generation, Pareto archive,
multi-subtask scheduler, automatic retry, Codex `thread/resume|fork`,
`turn/steer|interrupt`, `review/start`, native subagents.
