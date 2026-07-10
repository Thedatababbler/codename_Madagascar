# AdaMAS Stage 1 — Unified LiveCodeBench Runtime

Native Python 3.11/`asyncio` implementation of
`Stage1_LiveCodeBench_Unified_Runtime_Implementation.md`.

The source of truth is a typed `OrchestraGraph` loaded from YAML. The same
runtime executes all three comparable baselines:

- `B0_DIRECT`: one coding-model call, no pre-freeze public feedback;
- `B1_SINGLE_HARNESS`: the same coder plus public harness and at most one self-repair;
- `B2_FIXED_MAS`: parallel Algorithm/Edge-Case analysts, deterministic merge,
  coder, public harness, and conditional repair.

No smolagents, LangGraph, AutoGen, or hard-coded baseline pipeline is used.

## Setup

```bash
uv sync
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
- API keys and all environment variables containing `KEY`, `TOKEN`, `SECRET`,
  or `PASSWORD` are removed;
- the worker runs in a temporary directory and drops to the `nobody` UID/GID
  before generated code is evaluated;
- memory, process, file-descriptor, and file-size limits are applied;
- a wall timeout kills the complete worker process group;
- only public tests are serialized into the worker request.

`docker` remains an optional stronger backend. It is currently unavailable on
this machine, but Stage 1 development can proceed with `lcb_official`.
`sandbox.backend: mock` is accepted only together with `--mock-llm`.

> The LiveCodeBench reliability guard is not a complete security sandbox.
> `lcb_official` is intended only for a controlled research environment and
> therefore must remain paired with the independent low-privilege worker,
> environment redaction, resource limits, and process-group timeout described
> above. Use Docker for stronger isolation when it becomes available.

## Development

```bash
uv run ruff check .
uv run pytest -q
```
