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

## Security blocker

The v2 specification requires Docker and explicitly says to stop when Docker is
unavailable. This machine currently has no `docker` executable. Therefore:

- IR/compiler/mock/concurrency/evaluator-wrapper tests are implemented;
- `--mock-llm` runs are supported without executing untrusted code;
- a real run fails closed with a clear Docker blocker;
- no subprocess fallback is silently substituted.

Install Docker, pull/build the configured Python 3.11 image, and complete the
functional-style official-checker image validation before a real-model smoke run.

## Development

```bash
uv run ruff check .
uv run pytest -q
```
