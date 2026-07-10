# Changelog

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
