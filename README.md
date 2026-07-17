# AdaMAS Stage 1 — Unified LiveCodeBench Runtime

Native Python 3.11/`asyncio` implementation of
`Stage1_LiveCodeBench_Unified_Runtime_Implementation.md`.

The source of truth is a typed `OrchestraGraph` loaded from YAML. The same
runtime executes all three comparable baselines:

- `B0_DIRECT`: one coding-model call, no pre-freeze public feedback;
- `B1_SINGLE_HARNESS`: the same coder plus public harness and at most one self-repair;
- `B2_FIXED_MAS`: parallel Algorithm/Edge-Case analysts, deterministic merge,
  coder, public harness, and conditional repair.

No LangGraph, AutoGen, or hard-coded baseline pipeline is used as the
orchestration runtime. Optional `smolagents` is supported only as a pluggable
**CodeAgent execution backend** behind `AgentBackend` (see Milestone 2).

## Setup

```bash
uv sync
# Optional CodeAgent / BBEH vertical slice:
uv sync --extra smolagents
cp .env.example .env
```

The adapter uses the downloaded release-v6 data at
`/root/data/livecodebench/code_generation_lite` and the pinned official checkout
at commit `28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24`.

## Prepare and validate

```bash
uv run python -m orchestra.cli.prepare_lcb \
  --release-version release_v6 \
  --output configs/manifests/lcb_smoke.json \
  --num-easy 5 --num-medium 5 --num-hard 5 --seed 42

uv run python -m orchestra.cli.validate_graph \
  --graph configs/graphs/b2_fixed_mas.yaml
```

## Run

```bash
uv run python -m orchestra.cli.run \
  --config configs/experiments/stage1_b0_direct.yaml --resume

uv run python -m orchestra.cli.run \
  --config configs/experiments/stage1_b1_single_harness.yaml --resume

uv run python -m orchestra.cli.run \
  --config configs/experiments/stage1_b2_fixed_mas.yaml --resume
```

For API-free runtime validation:

```bash
uv run python -m orchestra.cli.run \
  --config configs/experiments/stage1_b2_fixed_mas.yaml \
  --mock-llm --limit 1 --force-rerun
```

Final evaluation and summary:

```bash
uv run python -m orchestra.cli.evaluate --run-dir outputs/stage1/<run_id>
uv run python -m orchestra.cli.summarize --run-dir outputs/stage1/<run_id>
```

## Runtime properties

- immutable, content-hashed artifact envelopes with parent lineage;
- dependency-ready asynchronous waves via `asyncio.TaskGroup`;
- atomic wave commit and checkpoint after every wave;
- conditional branch activation/disable and impossible-node skipping;
- separate task/LLM/sandbox semaphores;
- config-drift detection on resume;
- append-only graph/node/artifact telemetry;
- private tests accessible only to `FinalLCBEvaluator` after final freeze.

## Code-execution backends

The default development backend is `lcb_official`. It runs the pinned
LiveCodeBench `check_correctness` implementation in a separate worker process:

- no generated code is executed in the Orchestra runtime process;
- API keys and environment variables ending in `_KEY`, `_TOKEN`, `_SECRET`, or
  `_PASSWORD` are removed;
- the worker runs in a temporary directory and drops to the `nobody` UID/GID
  before generated code is evaluated;
- memory, process, file-descriptor, and file-size limits are applied;
- a wall timeout kills the complete worker process group;
- per-test timeout and worker wall timeout are configured separately;
- only public tests are serialized into the public worker request;
- private-final evaluation uses the same isolated worker model and returns only
  `passed` / `pass_at_1` after final freeze;
- the worker deletes `request.json` before executing generated code and runs checks
  from an `execution/` subdirectory;
- infra worker timeouts are retried once and reported as `infra_error`, not model
  failures.

`docker` remains an optional stronger backend. It is currently unavailable on
this machine, but Stage 1 development can proceed with `lcb_official`.
`sandbox.backend: mock` is accepted only together with `--mock-llm`.

> The LiveCodeBench reliability guard is not a complete security sandbox.
> `lcb_official` is intended only for a controlled research environment and
> therefore must remain paired with the independent low-privilege worker,
> environment redaction, resource limits, and process-group timeout described
> above. Use Docker for stronger isolation when it becomes available.

## smolagents CodeAgent backend

Install the optional extra, then run the BBEH smoke graph:

```bash
uv sync --extra smolagents
uv run python -m orchestra.cli.run_bbeh \
  --config configs/experiments/bbeh_codeagent_smoke.yaml
```

The CodeAgent runs in a **spawned worker process**. That worker exists for
**crash isolation and wall-timeout cleanup** of the top-level Orchestra
runtime. It is **not** a security sandbox: local `executor_type` still executes
model-generated Python with the worker process privileges. Do not treat it as
equivalent to Docker/E2B isolation.

Desensitized smoke summaries are written as `bbeh_summary_redacted.{json,md}`
under the run directory (reference answers and secrets omitted/redacted).

## Codex SDK backend (Milestone 3.5)

Optional second repository-editing backend. Codex only owns the single-node
coding loop; AdaMAS still owns TaskPlan, workspace, harness, and checkpoints.

```bash
uv sync --extra codex
uv run python -m orchestra.cli.run_codex_smoke \
  --config configs/experiments/m3_5_codex_smoke.yaml
```

Pinned: `openai-codex==0.1.0b3` (bundled CLI `0.137.0a4`). Design:
`docs/m3_5_codex_backend.md`. Experiment history: `EXPERIMENT_LOG.md`.

## Fast local adaptation (Milestone 4)

When a subtask graph fails, AdaMAS runs a **backend-agnostic Fast Loop**:
diagnose (failed-node targeting) → ≤K local edit candidates → capability /
budget gates (rejected candidates audited) → isolated workspaces → AdaMAS
harness → deterministic select → apply `WorkspaceChangeSet` (tracked +
untracked + delete/rename) → **canonical post-apply harness** → commit or
rollback. Cost uses a single source of truth (`CandidateRecord.cost`;
`search_cost` is derived). Downstream subtasks fork from the task canonical
workspace and receive declared dependency artifacts via
`SubtaskInputAssembler`. Concurrent subtasks merge through a locked
`SubtaskExecutionResult` path (`state_version` + atomic checkpoint).
Alternate models come only from configured `BackendModelPool`s.

CodeAgent and Codex both use `SessionPolicy.FRESH` in M4-A; RESUME/FORK stay
capability-gated. M5 slow update and M6 Pareto are **not** implemented.

Design: `docs/m4_fast_local_adaptation.md`.

```bash
# CI / local deterministic coverage (no API keys)
uv sync --extra smolagents --extra codex
uv run ruff check .
uv run pytest -q tests/unit
uv run pytest -q tests/integration

# Optional manual live smoke (not ordinary CI)
uv run --extra codex python -m orchestra.cli.run_m4_smoke \
  --backend codex_sdk \
  --config configs/experiments/m4_codex_fast_loop_smoke.yaml
```

## Stage 1 experiments

The protocol in `Stage1_LiveCodeBench_Experiment_Protocol.md` is executed via:

```bash
# Phase A: executor acceptance (required before real-model runs)
uv run python -m orchestra.cli.stage1_experiments acceptance \
  --lcb-repository-path /root/projects/LiveCodeBench

# Generate per-phase baseline configs (bringup/smoke/dev/heldout × b0/b1/b2)
uv run python -m orchestra.cli.stage1_experiments generate-configs

# Phase B: 3-task bring-up (sequential B0 -> B1 -> B2)
uv run python -m orchestra.cli.stage1_experiments bringup --force-rerun

# Phase C/D/E
uv run python -m orchestra.cli.stage1_experiments smoke --force-rerun
uv run python -m orchestra.cli.stage1_experiments dev --force-rerun
uv run python -m orchestra.cli.stage1_experiments heldout --force-rerun

# Comparison tables
uv run python -m orchestra.cli.stage1_experiments report --phase smoke
```

Outputs land under `outputs/stage1_experiments/<phase>/<baseline>/<run_id>/`.
Cross-baseline CSV reports are written to `outputs/stage1_experiments/reports/`.

Set `OPENAI_API_KEY` and model env vars in `.env` before non-mock runs.
Use `--mock-llm` only for pipeline validation.

## Development

```bash
export LCB_REPOSITORY_PATH=/path/to/LiveCodeBench
uv sync --extra smolagents
uv run ruff check .
uv run pytest -q tests/unit
uv run pytest -q tests/integration
```
