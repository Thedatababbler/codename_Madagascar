# Changelog

## 2026-07-10 — Milestone 2: smolagents CodeAgent vertical slice

### Added

- Optional dependency group `smolagents` pinned to `smolagents[openai]==1.26.0`.
- `SmolagentsCodeBackend` with spawn-isolated worker process, wall timeout, and
  process-group cleanup.
- `SmolagentsModelFactory`, ToolRegistry, and first-batch BBEH tools
  (`python_math`, `calculator`, `final_answer`).
- `BBEHTaskAdapter` + `orchestra.cli.run_bbeh` smoke path.
- Graph/experiment configs: `bbeh_single_codeagent`, `bbeh_codeagent_smoke`.
- `FinalAnswerArtifact` and `final_answer` parser.

### Changed

- Graph compiler validates tool allowlists and includes `smolagents_code` when
  the optional dependency is installed.
- `load_graph` expands `${ENV}` placeholders (for CodeAgent model names).

## 2026-07-10 — Milestone 1.1: backend execution boundaries

### Added

- Backend trace propagation into `NodeExecutionResult` and runtime telemetry
  (`backend_run_started` / `backend_step` / `backend_run_completed|failed`).
- Narrow `BackendExecutionContext` (no RunContext / stores / semaphores).
- Capability-based graph compile validation and pre-run backend healthchecks.
- Typed backend exceptions and `SmolagentsCodeBackendConfig` schema
  (forbids `managed_agents`).

### Changed

- `AgentNodeExecutor` preserves node `max_steps` and `tools` without hardcoding
  single-step structured-only behavior.

## 2026-07-10 — Milestone 1: AgentBackend abstraction

### Added

- `orchestra.backends` package with `AgentBackend` protocol, capabilities,
  registry, and `StructuredLLMBackend`.
- Legacy agent graph nodes without `backend` now default to `structured_llm`.
- Graph content hashes remain stable for default structured_llm backends.

### Changed

- `AgentNodeExecutor` now delegates through `AgentBackendRegistry` instead of
  calling the LLM client directly.
- Graph compiler validates that declared agent backends are registered.

## 2026-07-10 — Private request isolation and evaluation status

### Added

- Worker deletes `request.json` immediately after parsing and runs checker code from
  an `execution/` subdirectory so generated code cannot read hidden tests.
- `FinalEvaluationStatus` with `passed`, `wrong_answer`, `code_timeout`, and
  `infra_error` outcomes.
- One automatic retry for private-final worker wall timeouts before marking
  `infra_error`.
- `repair_eligible` harness field and graph routing so missing public tests skip
  repair and freeze the initial code.
- Tests for request deletion, infra-timeout classification, and no-harness repair
  skipping.

### Changed

- `evaluate` CLI excludes `infra_error` results from pass@1 and reports them
  separately.

## 2026-07-10 — Private-final worker and timeout hardening

### Added

- `FinalLCBWorker` and `PRIVATE_FINAL` worker mode for freeze-gated private evaluation.
- Split sandbox timeout configuration:
  - `per_test_timeout_seconds`
  - `worker_grace_seconds`
  - `max_worker_wall_seconds`
- `compute_worker_wall_timeout()` to align outer process-group timeout with the
  official checker budget.
- `function_name` on `AgentVisibleLCBTask` for consistent functional-task routing.
- `harness_available` on `PublicHarnessResultArtifact`; empty public tests no
  longer auto-pass.
- Dedicated CI integration job with pinned LiveCodeBench checkout via
  `LCB_REPOSITORY_PATH`.
- Tests for private-final isolation, multi-test wall timeout, compile-only
  syntax failures (`return`), and no-public-test harness behavior.

### Changed

- `FinalLCBEvaluator` now delegates to `FinalLCBWorker` instead of calling
  `check_correctness` in the main process.
- Public and private workers share the same low-privilege process runner:
  environment redaction, `nobody` drop, RLIMITs, and process-group wall timeout.
- Syntax checks now use `compile(..., "exec")` instead of `ast.parse()`.
- Environment redaction now matches sensitive suffixes (`_KEY`, `_TOKEN`,
  `_SECRET`, `_PASSWORD`) instead of substring matches such as `TOKEN`.

### Security note

The official LiveCodeBench reliability guard is not a complete security
sandbox. The worker backend is intended for controlled research environments
and relies on process isolation, privilege dropping, environment redaction,
resource limits, and timeout termination. Docker should be preferred when
stronger isolation is required.

## 2026-07-10 — Official LiveCodeBench worker backend

### Added

- `OfficialLCBSandbox`, the default development code-execution backend.
- `lcb_worker`, an independent process that invokes the pinned
  `lcb_runner.evaluation.compute_code_generation_metrics.check_correctness`.
- Strict public-test-only worker request schema using `AgentVisibleLCBTask`.
- Worker environment redaction for names containing `KEY`, `TOKEN`, `SECRET`,
  or `PASSWORD`.
- Linux resource limits for address space, processes, open files, and file size.
- Whole-process-group wall-clock timeout and fail-closed worker startup.
- Root-to-`nobody` UID/GID drop before generated code is evaluated.
- Single-thread OpenBLAS/OMP settings for predictable memory use.
- Structured tests for correct, wrong, syntax-error, runtime-error, and timeout
  outcomes, plus environment, resource-limit, private-isolation, and freeze-gate
  tests.

### Changed

- `SandboxBackend` is now the canonical backend abstraction.
- Supported backend values are `lcb_official`, `docker`, and `mock`.
- B0/B1/B2 experiment configs now default to `lcb_official` with:
  - 10-second wall timeout;
  - one evaluator process;
  - 2 GB memory;
  - 32 processes;
  - 128 open files;
  - 16 MB maximum file size.
- Docker remains available as an optional stronger-isolation backend.
- The runtime refuses automatic fallback to an ordinary subprocess.
- `mock` is restricted to explicit `--mock-llm` test runs.

### Security note

The official LiveCodeBench reliability guard is not a complete security
sandbox. The worker backend is intended for controlled research environments
and relies on process isolation, privilege dropping, environment redaction,
resource limits, and timeout termination. Docker should be preferred when
stronger isolation is required.
