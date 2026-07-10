---
title: "Stage 1 Implementation Plan — LiveCodeBench Runtime Orchestra Foundation"
status: execution-ready
target_benchmark: LiveCodeBench Code Generation
language_scope: Python 3.11
---

# Stage 1 Implementation Plan  
## LiveCodeBench Runtime Orchestra Foundation

## 0. Purpose

This document defines the first implementation stage for the two-level runtime orchestra optimization project.

Stage 1 does **not** implement the final Pareto search algorithm. Its purpose is to build a reliable, benchmark-compatible execution foundation that can later support:

- trace-based component attribution;
- suffix replay;
- constrained Agent/Contract/Edge edits;
- local Pareto archives;
- macro communication optimization;
- learned graph proposal models.

The output of Stage 1 must already be a complete runnable system: it should load LiveCodeBench tasks, run a fixed multi-agent coding workflow, execute visible tests safely, save all intermediate artifacts and telemetry, and evaluate the final submitted code with the official LiveCodeBench evaluator.

---

# 1. Stage 1 Scope

## 1.1 In Scope

Stage 1 implements the following components:

1. **LiveCodeBench adapter**
   - Load the official code-generation dataset.
   - Pin a reproducible release version.
   - Filter by date, difficulty, platform, and task IDs.
   - Build deterministic smoke/dev manifests.
   - Export final predictions in an official-compatible format.

2. **Benchmark-specific keystone template**
   - Use a fixed three-keystone workflow for competitive-programming tasks.
   - Do not implement an open-ended LLM task decomposer yet.
   - Bind a fixed harness to each keystone.

3. **Fixed orchestra baseline**
   - Algorithm Analyst.
   - Solution Coder.
   - Repair Agent, triggered only when visible checks fail.
   - No dynamic agent creation or graph search in this stage.

4. **Task-bound harness**
   - Plan/schema validation.
   - Code extraction and Python syntax validation.
   - Public-test execution.
   - Timeout/runtime-error collection.
   - Private tests reserved exclusively for final benchmark evaluation.

5. **Execution and artifact system**
   - Typed artifact schemas.
   - Run checkpoints.
   - Per-agent inputs and outputs.
   - Artifact provenance.
   - Final solution selection.

6. **Telemetry**
   - Prompt/completion tokens.
   - API cost where available.
   - Latency.
   - Parse failures.
   - Compile failures.
   - Runtime failures.
   - Public-test pass ratio.
   - Retry count.
   - Agent and keystone status.

7. **Reproducible CLI**
   - Prepare task manifests.
   - Run a fixed orchestra.
   - Resume interrupted runs.
   - Evaluate final outputs.
   - Aggregate metrics.

8. **Tests**
   - Unit tests for all schemas and adapters.
   - Harness tests with safe synthetic code.
   - End-to-end tests using mock LLM outputs.
   - A small real-model smoke run.

---

## 1.2 Explicitly Out of Scope

Do not implement the following in Stage 1:

- Pareto archive or epsilon-dominance;
- Shapley or marginal-contribution attribution;
- leave-one-out evaluation;
- suffix replay optimization;
- dynamic AgentEdit, ContractEdit, or EdgeEdit;
- LLM-selected graph topology;
- local candidate search;
- outer-loop communication optimization;
- dynamic keystone decomposition;
- diffusion, DPO, PPO, or other training;
- multi-language support;
- pass@5 or multi-sample benchmark reporting;
- unrestricted tool use;
- repository-level software engineering tasks.

Interfaces should be designed so these functions can be added later, but they must not be implemented prematurely.

---

## 1.3 Stage 1 Success Definition

Stage 1 is complete when the following command sequence works from a clean environment:

```bash
uv sync

python -m orchestra.cli.prepare_lcb \
  --release-version release_v6 \
  --manifest configs/manifests/lcb_smoke.json \
  --num-easy 5 \
  --num-medium 5 \
  --num-hard 5 \
  --seed 42

python -m orchestra.cli.run \
  --config configs/stage1_livecodebench.yaml \
  --manifest configs/manifests/lcb_smoke.json

python -m orchestra.cli.evaluate \
  --run-dir outputs/stage1_lcb/<run_id>

python -m orchestra.cli.summarize \
  --run-dir outputs/stage1_lcb/<run_id>
```

The run must produce:

- one final Python solution per problem;
- an official hidden-test pass/fail result;
- a complete JSONL event trace;
- all intermediate artifacts;
- per-task and aggregate token/cost/latency metrics;
- resumable checkpoints;
- no exposure of private tests to any LLM call.

---

# 2. LiveCodeBench Usage Policy

## 2.1 Scenario

Use only:

```text
scenario = codegeneration
language = Python 3.11
final samples per task = 1
primary metric = pass@1
```

The system may perform internal analysis and one repair attempt, but it must submit exactly one final code solution per task.

---

## 2.2 Dataset Version

Pin:

```text
release_version = release_v6
```

Do not use `release_latest` in committed experiment configurations because it is not reproducible.

At setup time:

1. Add the official LiveCodeBench repository as a pinned submodule or pinned dependency.
2. Record the exact LiveCodeBench Git commit in:
   - `third_party/LOCK.md`;
   - every run manifest;
   - the output metadata.

Recommended setup:

```bash
git submodule add \
  https://github.com/LiveCodeBench/LiveCodeBench.git \
  third_party/LiveCodeBench

git -C third_party/LiveCodeBench checkout <PINNED_COMMIT>
```

Do not directly modify official LiveCodeBench source files. Add all integration logic through adapters in this project.

---

## 2.3 Public and Private Test Separation

This is a hard requirement.

### Public tests

Public tests may be used for:

- runtime harness feedback;
- compile/runtime validation;
- deciding whether to trigger the single repair agent;
- computing public-test pass ratio;
- producing failure summaries.

### Private tests

Private tests may be used only for:

- final offline benchmark evaluation;
- computing official pass@1;
- aggregate reporting after the final answer has been frozen.

Private-test inputs, expected outputs, failure cases, and partial results must never be included in:

- agent prompts;
- repair prompts;
- intermediate artifacts;
- telemetry visible to agents;
- candidate selection;
- any future search loop.

Implement separate types and interfaces so accidental leakage is difficult.

```python
class VisibleTestHarness:
    def evaluate_public(self, task: LCBTask, code: str) -> PublicTestResult:
        ...

class FinalLCBEvaluator:
    def evaluate_hidden(self, task: LCBTask, final_code: str) -> FinalEvaluation:
        ...
```

`FinalLCBEvaluator` must only be called after the task run is marked `FROZEN`.

---

# 3. Development Dataset Manifests

## 3.1 Smoke Manifest

Create a deterministic 15-problem smoke set:

```text
5 easy
5 medium
5 hard
seed = 42
release = release_v6
```

Selection requirements:

- include at least two platforms if possible;
- exclude tasks that fail official evaluation due to known benchmark errata;
- save exact question IDs;
- never resample automatically after the manifest is committed.

File:

```text
configs/manifests/lcb_smoke.json
```

---

## 3.2 Development Manifest

Create a 60-problem development set:

```text
20 easy
20 medium
20 hard
seed = 2026
```

The development set may be used to:

- debug prompts;
- debug orchestration;
- tune non-learned thresholds;
- verify logging and execution behavior.

File:

```text
configs/manifests/lcb_dev.json
```

---

## 3.3 Held-Out Manifest

Create but do not repeatedly inspect a 60-problem held-out set:

```text
20 easy
20 medium
20 hard
seed = 2027
```

Constraints:

- no overlap with smoke or dev;
- prompt changes based on individual held-out failures are prohibited;
- hidden-test results may be aggregated but must not be shown to agents.

File:

```text
configs/manifests/lcb_heldout.json
```

Stage 1 acceptance only requires the smoke manifest. The dev and held-out manifests should be generated and checked for overlap, but a full held-out benchmark run can wait until the infrastructure is stable.

---

# 4. Fixed Keystone Design for LiveCodeBench

LiveCodeBench tasks are standalone competitive-programming problems. For Stage 1, do not use an LLM to invent arbitrary keystones. Use the following benchmark-specific template.

## 4.1 Keystone K1 — Algorithm Analysis

### Goal

Produce a structured algorithm plan from the problem statement.

### Input

```python
class ProblemArtifact(BaseModel):
    question_id: str
    title: str
    statement: str
    starter_code: str
    difficulty: str
    platform: str
    public_examples: list[PublicExample]
```

### Output

```python
class AlgorithmPlanArtifact(BaseModel):
    problem_summary: str
    algorithm: str
    data_structures: list[str]
    correctness_argument: str
    time_complexity: str
    space_complexity: str
    edge_cases: list[str]
    implementation_notes: list[str]
```

### Fixed harness

`PlanHarness` performs only deterministic checks:

- valid JSON/Pydantic schema;
- all required fields are present;
- `algorithm` is non-empty;
- time and space complexity are non-empty;
- edge-case list contains at least one entry;
- no code block is required.

The K1 harness does not claim to prove that the plan is correct.

### Agent

```text
AlgorithmAnalyst
```

The agent may only read the problem artifact. It has no access to private tests.

---

## 4.2 Keystone K2 — Solution Generation

### Goal

Generate one complete Python 3.11 solution using the K1 plan.

### Input

```python
class CodingInputArtifact(BaseModel):
    problem: ProblemArtifact
    plan: AlgorithmPlanArtifact
```

### Output

```python
class CodeArtifact(BaseModel):
    language: Literal["python"]
    code: str
    explanation: str | None = None
```

### Fixed harness

`CodeHarness` executes:

1. code-block extraction;
2. empty-code check;
3. `ast.parse`;
4. `python -m py_compile`;
5. starter-code compatibility check where applicable;
6. public-test execution;
7. timeout and runtime-error collection.

### Agent

```text
SolutionCoder
```

The coder receives only:

- visible problem statement;
- public examples;
- K1 plan;
- starter code.

---

## 4.3 Keystone K3 — Validation and Single Repair

K3 is conditional.

### Trigger

Run the repair agent only if any of the following occurs:

- code extraction fails;
- syntax/compile check fails;
- at least one public test fails;
- runtime exception occurs;
- public-test timeout occurs.

### Repair input

```python
class RepairInputArtifact(BaseModel):
    problem: ProblemArtifact
    plan: AlgorithmPlanArtifact
    previous_code: str
    visible_failure_summary: VisibleFailureSummary
```

`VisibleFailureSummary` may contain:

- compile error;
- exception type;
- timeout;
- public input;
- expected public output;
- actual public output;
- failed public-test index.

It must not contain private-test information.

### Repair output

```python
class RepairArtifact(BaseModel):
    diagnosis: str
    changes: list[str]
    revised_code: str
```

### Fixed harness

Run the same `CodeHarness` used by K2.

### Repair budget

```text
maximum repair attempts = 1
```

If the repaired solution still fails public tests, freeze the better of:

- original solution;
- repaired solution.

Use the deterministic selection rule:

1. higher public-test pass ratio;
2. compile success over compile failure;
3. fewer runtime errors;
4. lower execution time;
5. repaired solution only if all previous criteria tie.

No LLM judge is used for final code selection.

---

# 5. Stage 1 Fixed Orchestra

The initial graph is fixed:

```text
ProblemArtifact
      │
      ▼
AlgorithmAnalyst
      │ AlgorithmPlanArtifact
      ▼
SolutionCoder
      │ CodeArtifact
      ▼
CodeHarness
      │
      ├── PASS ───────────────► Freeze Final Code
      │
      └── FAIL
             │ VisibleFailureSummary
             ▼
         RepairAgent
             │ RepairArtifact
             ▼
         CodeHarness
             │
             ▼
      Deterministic Final Selector
             │
             ▼
        Freeze Final Code
```

No graph edge can be added, removed, or rewired in Stage 1.

This graph serves as:

- the initial baseline;
- the data source for later attribution;
- the initial checkpoint model for suffix replay;
- the reference implementation for future dynamic search.

---

# 6. Repository Structure

Create the following structure:

```text
.
├── pyproject.toml
├── uv.lock
├── README.md
├── .env.example
├── configs/
│   ├── stage1_livecodebench.yaml
│   ├── pricing.yaml
│   └── manifests/
│       ├── lcb_smoke.json
│       ├── lcb_dev.json
│       └── lcb_heldout.json
│
├── third_party/
│   ├── LiveCodeBench/
│   └── LOCK.md
│
├── src/orchestra/
│   ├── __init__.py
│   ├── cli/
│   │   ├── prepare_lcb.py
│   │   ├── run.py
│   │   ├── evaluate.py
│   │   └── summarize.py
│   │
│   ├── adapters/
│   │   └── livecodebench/
│   │       ├── loader.py
│   │       ├── mapper.py
│   │       ├── public_harness.py
│   │       ├── final_evaluator.py
│   │       └── exporter.py
│   │
│   ├── schemas/
│   │   ├── task.py
│   │   ├── keystone.py
│   │   ├── artifacts.py
│   │   ├── agent.py
│   │   ├── harness.py
│   │   ├── telemetry.py
│   │   └── run.py
│   │
│   ├── llm/
│   │   ├── base.py
│   │   ├── openai_compatible.py
│   │   ├── mock.py
│   │   └── usage.py
│   │
│   ├── agents/
│   │   ├── base.py
│   │   ├── analyst.py
│   │   ├── coder.py
│   │   └── repair.py
│   │
│   ├── prompts/
│   │   ├── analyst.py
│   │   ├── coder.py
│   │   └── repair.py
│   │
│   ├── harness/
│   │   ├── plan_harness.py
│   │   ├── code_harness.py
│   │   ├── code_extract.py
│   │   └── sandbox.py
│   │
│   ├── execution/
│   │   ├── stage1_orchestra.py
│   │   ├── checkpoint.py
│   │   ├── artifact_store.py
│   │   └── state_machine.py
│   │
│   ├── telemetry/
│   │   ├── collector.py
│   │   ├── event_writer.py
│   │   ├── pricing.py
│   │   └── summary.py
│   │
│   └── evaluation/
│       ├── metrics.py
│       └── aggregate.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── fixtures/
│   └── e2e/
│
└── outputs/
    └── .gitkeep
```

---

# 7. Core Interfaces

## 7.1 LLM Client

Implement one provider interface first:

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float | None = None


class LLMResponse(BaseModel):
    text: str
    model: str
    latency_ms: int
    usage: LLMUsage
    raw_response_id: str | None = None


class LLMClient(ABC):
    @abstractmethod
    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResponse:
        ...
```

Implement:

```text
OpenAICompatibleClient
MockLLMClient
```

The OpenAI-compatible client must support:

- configurable `base_url`;
- API key from environment;
- timeout;
- retry on transport errors only;
- usage extraction when provided;
- explicit model name;
- request/response metadata logging.

Do not implement provider-specific clients in Stage 1.

---

## 7.2 Agent Interface

```python
class AgentRunContext(BaseModel):
    task_id: str
    run_id: str
    keystone_id: str
    attempt: int


class AgentResult(BaseModel):
    agent_name: str
    raw_text: str
    parsed_artifact: dict | None
    parse_error: str | None
    usage: LLMUsage
    latency_ms: int


class Agent(ABC):
    name: str

    @abstractmethod
    def run(
        self,
        input_artifact: BaseModel,
        context: AgentRunContext,
    ) -> AgentResult:
        ...
```

Each agent must:

1. build a prompt from a typed input artifact;
2. call `LLMClient`;
3. parse the response;
4. return both raw output and parsed artifact;
5. never directly execute code.

---

## 7.3 Harness Interface

```python
class HarnessResult(BaseModel):
    passed: bool
    score: float
    status: str
    errors: list[str]
    telemetry: dict
    visible_feedback: dict


class Harness(ABC):
    @abstractmethod
    def evaluate(self, artifact: BaseModel) -> HarnessResult:
        ...
```

Implement:

```text
PlanHarness
CodeHarness
```

`CodeHarness` must accept a flag:

```python
visibility: Literal["public", "final"]
```

The normal runtime executor must only instantiate `visibility="public"`.

The final evaluator uses a separate class to avoid accidental private-test access.

---

# 8. Safe Code Execution

Generated code is untrusted.

## 8.1 Preferred implementation

Use a Docker-based sandbox:

```text
base image: python:3.11-slim
network: disabled
filesystem: read-only
temporary storage: tmpfs
CPU limit: 1 core
memory limit: 512 MB
PID limit: 64
wall timeout: configurable, default 10 seconds per test batch
```

Example constraints:

```bash
docker run --rm \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cpus 1 \
  --memory 512m \
  --pids-limit 64 \
  <image>
```

Use the official LiveCodeBench testing utilities inside the sandbox when practical.

## 8.2 Fallback for CI

For unit tests only, support a subprocess sandbox with:

- timeout;
- temporary working directory;
- no inherited secrets;
- sanitized environment;
- resource limits where supported.

The subprocess fallback must not be used for large-scale real benchmark execution unless explicitly enabled.

---

# 9. State Machine

Implement an explicit state machine.

```python
class TaskStatus(str, Enum):
    CREATED = "created"
    ANALYSIS_RUNNING = "analysis_running"
    ANALYSIS_DONE = "analysis_done"
    CODING_RUNNING = "coding_running"
    CODE_READY = "code_ready"
    PUBLIC_EVAL_RUNNING = "public_eval_running"
    REPAIR_RUNNING = "repair_running"
    FINAL_CODE_FROZEN = "final_code_frozen"
    FINAL_EVAL_RUNNING = "final_eval_running"
    COMPLETED = "completed"
    FAILED = "failed"
```

Valid transitions must be enforced.

Critical rule:

```text
FinalLCBEvaluator can run only from FINAL_CODE_FROZEN.
```

On resume:

- load the last valid checkpoint;
- do not repeat completed LLM calls unless `--force-rerun` is specified;
- do not rerun final evaluation if a valid result already exists.

---

# 10. Artifact Storage

Each task must have a self-contained directory:

```text
outputs/stage1_lcb/<run_id>/tasks/<question_id>/
├── task.json
├── state.json
├── artifacts/
│   ├── problem.json
│   ├── plan.json
│   ├── code_initial.json
│   ├── public_eval_initial.json
│   ├── repair.json
│   ├── code_repaired.json
│   ├── public_eval_repaired.json
│   └── final_code.py
├── prompts/
│   ├── analyst_system.txt
│   ├── analyst_user.txt
│   ├── coder_system.txt
│   ├── coder_user.txt
│   ├── repair_system.txt
│   └── repair_user.txt
├── responses/
│   ├── analyst_raw.txt
│   ├── coder_raw.txt
│   └── repair_raw.txt
├── telemetry.jsonl
└── final_evaluation.json
```

Do not write private test cases to this directory.

---

# 11. Telemetry Events

Use append-only JSONL events.

Required event types:

```text
TASK_STARTED
AGENT_STARTED
AGENT_COMPLETED
AGENT_PARSE_FAILED
HARNESS_STARTED
HARNESS_COMPLETED
PUBLIC_TEST_FAILED
REPAIR_TRIGGERED
FINAL_CODE_SELECTED
FINAL_CODE_FROZEN
FINAL_EVALUATION_COMPLETED
TASK_COMPLETED
TASK_FAILED
```

Common event fields:

```python
class TelemetryEvent(BaseModel):
    timestamp: str
    run_id: str
    task_id: str
    event_type: str
    keystone_id: str | None = None
    agent_name: str | None = None
    attempt: int | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_usd: float | None = None
    status: str | None = None
    metadata: dict = {}
```

Telemetry must be sufficient for later Stage 2 work to calculate:

- node cost;
- node failure rate;
- downstream artifact use;
- repair effectiveness;
- public-score delta;
- execution stability.

---

# 12. Configuration

Create:

```yaml
# configs/stage1_livecodebench.yaml

experiment:
  name: stage1_lcb_fixed_orchestra
  seed: 42
  output_root: outputs/stage1_lcb

benchmark:
  release_version: release_v6
  scenario: codegeneration
  language: python
  max_tasks: null

models:
  analyst:
    provider: openai_compatible
    model: ${ANALYST_MODEL}
    temperature: 0.2
    max_tokens: 1800

  coder:
    provider: openai_compatible
    model: ${CODER_MODEL}
    temperature: 0.2
    max_tokens: 4000

  repair:
    provider: openai_compatible
    model: ${REPAIR_MODEL}
    temperature: 0.1
    max_tokens: 4000

orchestra:
  max_repair_attempts: 1
  skip_repair_when_public_pass: true

harness:
  python_version: "3.11"
  compile_timeout_seconds: 5
  public_test_timeout_seconds: 10
  sandbox_backend: docker

logging:
  save_prompts: true
  save_raw_responses: true
  save_artifacts: true
  redact_environment: true
```

Validate configuration at startup using Pydantic.

---

# 13. CLI Requirements

## 13.1 Prepare manifest

```bash
python -m orchestra.cli.prepare_lcb \
  --release-version release_v6 \
  --output configs/manifests/lcb_smoke.json \
  --num-easy 5 \
  --num-medium 5 \
  --num-hard 5 \
  --seed 42
```

Must:

- load official metadata;
- exclude known invalid IDs from a local exclusion file;
- select deterministic IDs;
- save task metadata but not private tests;
- validate no duplicate IDs.

---

## 13.2 Run

```bash
python -m orchestra.cli.run \
  --config configs/stage1_livecodebench.yaml \
  --manifest configs/manifests/lcb_smoke.json \
  --resume
```

Optional arguments:

```text
--limit N
--task-id ID
--force-rerun
--dry-run
--mock-llm
```

---

## 13.3 Evaluate

```bash
python -m orchestra.cli.evaluate \
  --run-dir outputs/stage1_lcb/<run_id>
```

Must:

- verify all selected tasks are frozen;
- build official-compatible generation records;
- invoke the pinned official evaluator;
- save per-task pass/fail;
- never expose failure details to the agent pipeline.

---

## 13.4 Summarize

```bash
python -m orchestra.cli.summarize \
  --run-dir outputs/stage1_lcb/<run_id>
```

Output:

```text
number of tasks
pass@1
public-test pass rate before repair
public-test pass rate after repair
repair trigger rate
repair success rate
average prompt tokens
average completion tokens
average total cost
average latency
compile failure rate
runtime failure rate
timeout rate
```

Also write:

```text
summary.json
summary.csv
```

---

# 14. Prompt Requirements

## 14.1 Analyst prompt

The analyst must return JSON only.

Required behavior:

- identify the algorithm;
- explain correctness;
- state complexity;
- list edge cases;
- not generate full code;
- not assume hidden tests.

## 14.2 Coder prompt

The coder must:

- follow the provided plan;
- output one complete solution;
- use Python 3.11;
- respect stdin/stdout or functional interface based on task metadata;
- avoid markdown outside a single code block where possible;
- not include tests in the final submission.

## 14.3 Repair prompt

The repair agent must receive:

- original problem;
- plan;
- previous code;
- visible failure summary.

It must not receive:

- private tests;
- aggregate hidden-test score;
- any text implying the nature of hidden failures.

The repair agent returns diagnosis plus revised code.

---

# 15. Implementation Work Breakdown

## Task 1 — Project Bootstrap

Create:

- `pyproject.toml`;
- `src/` layout;
- `uv` environment;
- linting and formatting;
- test configuration;
- `.env.example`;
- CI test workflow.

Dependencies:

```text
pydantic
pydantic-settings
datasets
pyyaml
httpx
tenacity
orjson
pytest
pytest-cov
ruff
```

Add LiveCodeBench through the pinned submodule.

### Acceptance

```bash
uv sync
ruff check .
pytest -q
```

must succeed.

---

## Task 2 — LiveCodeBench Adapter

Implement:

- official dataset loading;
- task mapping;
- release/date/difficulty filters;
- deterministic manifest generation;
- public/private test separation;
- official output export.

### Acceptance

A unit test must load one fixture task and verify:

- metadata maps correctly;
- public tests are available to runtime harness;
- private tests are absent from `ProblemArtifact`;
- final evaluator can access private tests through its isolated interface.

---

## Task 3 — Typed Schemas

Implement all Pydantic models:

- task;
- keystone;
- problem artifact;
- plan artifact;
- code artifact;
- repair artifact;
- harness result;
- telemetry event;
- run state.

### Acceptance

- JSON round-trip tests;
- invalid artifacts fail clearly;
- schema version is stored in every artifact.

---

## Task 4 — LLM Provider and Mock

Implement:

- abstract `LLMClient`;
- OpenAI-compatible client;
- mock client with fixture-based responses;
- usage and cost extraction;
- transport retry;
- timeout;
- error classification.

### Acceptance

- unit tests use the mock client only;
- no network calls in CI;
- integration test can point to a local OpenAI-compatible server.

---

## Task 5 — Agents and Prompts

Implement:

- `AlgorithmAnalyst`;
- `SolutionCoder`;
- `RepairAgent`;
- prompt builders;
- JSON/code parsing;
- parse-error reporting.

### Acceptance

Given fixture responses:

- analyst produces valid `AlgorithmPlanArtifact`;
- coder extracts valid code;
- repair produces valid revised code;
- malformed output produces a typed parse error rather than a crash.

---

## Task 6 — Harness and Sandbox

Implement:

- plan harness;
- code extraction;
- syntax/compile checks;
- public test evaluator;
- Docker sandbox;
- visible failure summary.

### Acceptance

Synthetic tests must cover:

1. correct solution;
2. syntax error;
3. wrong answer;
4. runtime exception;
5. infinite loop/timeout;
6. malformed code block;
7. functional-style task if present in fixtures.

---

## Task 7 — Fixed Orchestra Executor

Implement the state machine:

```text
analyst → coder → public harness
                   ├── pass → freeze
                   └── fail → repair → public harness → select → freeze
```

Implement:

- checkpointing;
- resume;
- deterministic final selection;
- task failure handling.

### Acceptance

An end-to-end mock run must complete without external API access and produce the full expected output directory.

---

## Task 8 — Telemetry and Summaries

Implement:

- append-only JSONL logging;
- token/cost tracking;
- event aggregation;
- run summary.

### Acceptance

For each agent call and harness call, the event log contains:

- start event;
- completion/failure event;
- latency;
- status;
- token usage where applicable.

---

## Task 9 — Official Final Evaluation

Implement a wrapper around the pinned official LiveCodeBench evaluator.

Requirements:

- evaluate one final code per task;
- compute pass@1;
- save official-compatible outputs;
- keep hidden feedback isolated;
- record evaluator version and timeout settings.

### Acceptance

A known-correct fixture solution passes.  
A known-wrong fixture solution fails.  
No private test appears in prompts, artifacts, or normal telemetry.

---

## Task 10 — Real Smoke Run

Run:

```text
3 easy tasks
2 medium tasks
1 hard task
```

Then run the complete 15-task smoke manifest.

Record:

- pass@1;
- total cost;
- public-pass and hidden-pass discrepancy;
- repair frequency;
- repair improvement;
- failures caused by infrastructure rather than model reasoning.

Infrastructure failures must be fixed before Stage 2 begins.

---

# 16. Test Plan

## Unit tests

At minimum:

```text
test_lcb_task_mapping.py
test_manifest_sampling.py
test_private_test_isolation.py
test_artifact_schemas.py
test_code_extraction.py
test_plan_harness.py
test_code_harness_compile.py
test_public_test_execution.py
test_timeout.py
test_mock_llm.py
test_cost_accounting.py
test_state_transitions.py
test_checkpoint_resume.py
```

## Integration tests

```text
test_fixed_orchestra_mock_e2e.py
test_public_to_repair_flow.py
test_no_repair_on_public_pass.py
test_official_evaluator_wrapper.py
```

## Security tests

Verify:

- generated code cannot access network;
- environment secrets are not inherited;
- process count and memory are limited;
- timeout kills descendants;
- generated files remain inside the sandbox.

---

# 17. Stage 1 Output Contract for Stage 2

Stage 1 must save enough information for future attribution and local editing.

Every completed task must expose:

```python
class Stage1TraceBundle(BaseModel):
    task_id: str
    problem: ProblemArtifact
    plan: AlgorithmPlanArtifact
    initial_code: str
    repaired_code: str | None
    final_code: str
    agent_runs: list[AgentResult]
    public_harness_runs: list[HarnessResult]
    artifact_provenance: list[dict]
    telemetry_events: list[TelemetryEvent]
    final_passed: bool
```

The following fields are especially important:

- which agent produced each artifact;
- which later agent consumed it;
- token/cost per agent;
- parse/compile/runtime failures;
- public-score change after repair;
- exact failure summary given to the repair agent;
- checkpoints before and after each keystone.

Do not optimize or interpret these traces yet. Stage 1 only guarantees they are complete and consistent.

---

# 18. Definition of Done

Stage 1 is done only when all items below are satisfied.

## Functional

- [ ] LiveCodeBench release is pinned.
- [ ] Smoke manifest is deterministic.
- [ ] Fixed three-keystone orchestra runs end to end.
- [ ] One repair attempt is supported.
- [ ] Final solution is frozen before hidden evaluation.
- [ ] Official pass@1 is computed.
- [ ] Resume works after interruption.

## Fairness

- [ ] Private tests never enter agent-visible data.
- [ ] Hidden evaluation cannot trigger repair.
- [ ] One final solution is submitted per task.
- [ ] All model and budget settings are logged.

## Reliability

- [ ] Generated code runs in a sandbox.
- [ ] Timeouts and exceptions do not crash the benchmark runner.
- [ ] All artifacts are schema validated.
- [ ] Every task has a complete event trace.

## Testing

- [ ] Unit test suite passes.
- [ ] Mock end-to-end suite passes.
- [ ] Known-correct/known-wrong evaluator tests pass.
- [ ] Six-task real-model smoke run completes.
- [ ] Fifteen-task smoke manifest completes.

## Documentation

- [ ] README includes setup and execution commands.
- [ ] Configuration fields are documented.
- [ ] Output directory format is documented.
- [ ] LiveCodeBench commit and release are recorded.
- [ ] Known limitations are documented.

---

# 19. Stop Conditions for the Coding Agent

The coding agent should stop Stage 1 and report blockers rather than silently expanding scope when:

- the official LiveCodeBench evaluator cannot be invoked reproducibly;
- public/private tests cannot be isolated safely;
- sandbox execution is unavailable;
- dataset schema differs from the adapter assumptions;
- a functional-style task requires unsupported execution behavior;
- model API usage cannot provide token telemetry;
- a requested change requires implementing Pareto search or dynamic editing.

For each blocker, report:

```text
affected module
observed behavior
minimal reproduction command
error output
proposed minimal fix
whether the fix changes Stage 1 scope
```

Do not implement Stage 2 features as a workaround.
