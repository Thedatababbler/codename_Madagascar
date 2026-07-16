# Milestone 3.5 — Codex Second-Backend Vertical Slice

## Goal

Enable AdaMAS single-subtask `TaskPlan` execution to call the **Codex Python SDK**
as one Agent node backend. Codex edits an isolated Git workspace; AdaMAS owns
TaskPlan / SubtaskState / artifacts / checkpoints / graph runtime / repository
harness. No M4 fast loop, no multi-subtask scheduler, no Codex source changes.

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
       → CodexSDKBackend (node)
       → repository_test_harness (AdaMAS)
  → task_execution.json checkpoint
```

Forbidden in M3.5 backend **YAML** config: resume, fork, steer, interrupt, review,
native subagents, danger-full-access / full_access.

Host note: Codex `workspace_write` uses `bwrap` user namespaces. If the host
blocks that (common in some containers), set
`ADAMAS_CODEX_SANDBOX_OVERRIDE=full_access` for local smoke only. This does not
relax the YAML forbid; it is an explicit runtime escape hatch.

## Workspace layout

```text
<run_dir>/tasks/<task_id>/workspaces/<subtask_id>/repo/
```

Kind: `SHARED_SUBTASK_WORKSPACE`. Source repo is cloned/copied; user original
repo is never mutated. Absolute paths live in execution state (`workspace_ref`),
not in TaskPlan content hash.

## Artifacts

- `RepositoryChangeArtifact`: `patch` from `git diff --binary --no-ext-diff`;
  `changed_files` from Git status; `final_response` is explanatory only.
- Empty diff with `require_git_diff=true` → `OUTPUT_CONTRACT_FAILURE`.
- `RepositoryHarnessResultArtifact`: produced only by AdaMAS harness, never by
  trusting Codex self-report.
- `repository_test_harness` currently supports **trusted fixtures only** (marker
  file `.adamas_trusted_harness`). It runs pytest as a normal subprocess under
  the Orchestra process privileges — **not** a low-privilege worker. Do not
  point it at untrusted repositories; set `ADAMAS_ALLOW_UNTRUSTED_REPO_HARNESS=1`
  only for explicit local overrides.

## Configs / CLI

| Asset | Path |
|-------|------|
| Graph | `configs/graphs/codex_single_implementer.yaml` |
| Contract | `configs/contracts/codex_implementer.yaml` |
| Plan | `configs/plans/codex_tiny_repo_single_subtask.yaml` |
| Experiment | `configs/experiments/m3_5_codex_smoke.yaml` |
| Fixture | `tests/fixtures/codex_tiny_repo/` |
| CLI | `python -m orchestra.cli.run_codex_smoke --config configs/experiments/m3_5_codex_smoke.yaml` |

## Tests

`tests/unit/test_codex_m3_5_slice.py` covers config bans, workspace requirement,
no silent fallback, git artifact creation (fake Codex client), repository
harness, checkpoint persistence + resume skip, graph compile.

Real Codex auth smoke is optional and must not be a hard CI dependency without
credentials.

## Non-goals (stop after M3.5)

FastLoopController, SlowLoop, LocalEdit, multi-subtask scheduler, SWE-bench.
