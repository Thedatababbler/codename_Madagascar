---
title: "Stage 1 Implementation Plan v2 — LiveCodeBench Unified Agent & MAS Runtime"
status: execution-ready
target_benchmark: LiveCodeBench Code Generation
python_version: "3.11"
primary_goal: "Build one unified runtime that executes Direct Agent, Single-Agent-with-Harness, and Fixed MAS baselines fairly."
---

# Stage 1 Implementation Plan v2  
## LiveCodeBench Unified Agent & MAS Runtime

## 0. Document Purpose

This document is an implementation specification for a coding agent.

Stage 1 must build the shared execution infrastructure for all later experiments. It must not hard-code one agent pipeline. Instead, it must implement:

1. a typed **Orchestra Intermediate Representation (Orchestra IR)**;
2. a dependency-aware asynchronous graph runtime;
3. task-bound LiveCodeBench harnesses;
4. three directly comparable baselines:
   - Direct Single Agent;
   - Single Agent + Harness;
   - Fixed Multi-Agent System;
5. safe execution, checkpointing, telemetry, and official evaluation.

The same runtime, model client, benchmark adapter, artifact store, sandbox, and budget accounting must be used for all three baselines.

Stage 2 will add local Pareto search by generating and evaluating multiple `OrchestraGraph` configurations. Therefore, Stage 1 must treat the graph configuration as data rather than encode the workflow directly in Python control flow.

---

# 1. Stage 1 Research Questions

Stage 1 should make it possible to answer three increasingly specific questions.

## Q1. How strong is a direct coding agent?

The model receives the problem and produces one final solution in one LLM call.

This is the mandatory base baseline.

## Q2. How much improvement comes from harness feedback and one repair attempt?

The same coding agent is allowed to inspect visible public-test feedback and revise its solution once.

This isolates the contribution of execution feedback from the contribution of multi-agent collaboration.

## Q3. Does fixed multi-agent role decomposition improve over a single agent under transparent cost accounting?

Two analysis agents work in parallel, their artifacts are merged and passed to a coder, and a separate repair agent is invoked conditionally.

This isolates the value of multi-agent decomposition before any Pareto search is introduced.

The experimental progression is:

```text
B0 Direct Agent
      ↓
B1 Single Agent + Harness
      ↓
B2 Fixed MAS
      ↓
Stage 2: Pareto-Optimized MAS
```

---

# 2. Stage 1 Scope

## 2.1 In Scope

Stage 1 implements:

### Benchmark integration

- LiveCodeBench `codegeneration` scenario;
- the official `code_generation_lite` dataset;
- a pinned release version;
- deterministic task manifests;
- public/private test isolation;
- one final code output per task;
- official-compatible custom evaluation output;
- pass@1 reporting.

### Core runtime

- custom Orchestra IR;
- graph validation;
- typed artifacts;
- artifact dependency routing;
- conditional edges;
- branch and join;
- asynchronous ready-node scheduling;
- task-level and node-level concurrency;
- LLM and sandbox semaphores;
- node-level timeout and failure handling;
- checkpoint and resume;
- deterministic event traces.

### Three baselines

- `B0_DIRECT`;
- `B1_SINGLE_HARNESS`;
- `B2_FIXED_MAS`.

### Fixed task harnesses

- code extraction;
- syntax checking;
- compile checking;
- public-test execution;
- failure summarization;
- final private-test evaluation only after output freeze.

### Observability

- prompt/completion tokens;
- estimated API cost;
- latency;
- parse failures;
- compile failures;
- runtime exceptions;
- timeouts;
- public-test pass ratio;
- repair trigger and repair delta;
- graph node start/end events;
- graph-level critical-path latency;
- total and per-agent cost.

---

## 2.2 Explicitly Out of Scope

Do not implement:

- Pareto archive;
- epsilon-dominance;
- hypervolume;
- Shapley or marginal attribution;
- dynamic graph editing;
- free-form agent creation;
- candidate graph search;
- suffix replay optimization;
- cross-keystone outer loop;
- dynamic keystone decomposition;
- graph diffusion;
- DPO, PPO, or other training;
- smolagents as the orchestration backend;
- LangGraph as the canonical graph representation;
- AutoGen group chat;
- repository-level SWE tasks;
- multi-language support;
- more than one repair iteration;
- hidden-test-driven repair.

The code must expose extension points for later stages, but no Stage 2 algorithm should be implemented.

---

# 3. Technical Selection

## 3.1 Do Not Use an Agent Framework as the Core Runtime

Do not use `smolagents`, AutoGen, CrewAI, or another conversation-first framework as the canonical runtime.

Reasons:

- the graph itself will become the optimization/search object;
- later stages need graph cloning and graph diffs;
- later stages need atomic Agent/Contract/Edge edits;
- artifacts must be typed and cached;
- suffix replay requires explicit dependency information;
- exact per-node cost and latency must be available;
- public/private harness separation must be under project control.

A framework-specific graph object must not become the source of truth.

---

## 3.2 Required Runtime Stack

Use:

```text
Python 3.11
asyncio
asyncio.TaskGroup
asyncio.Semaphore
Pydantic v2
JSON / JSONL artifact storage
Docker sandbox
official pinned LiveCodeBench evaluator
```

`asyncio.TaskGroup` is used for structured concurrency inside one ready-node wave.

Node tasks must not mutate shared graph state directly. Each task returns an immutable `NodeExecutionResult`; the scheduler commits all results after the wave completes.

---

## 3.3 Backend Abstraction

Define a backend interface even though Stage 1 implements only the native backend.

```python
class RuntimeBackend(ABC):
    @abstractmethod
    async def execute(
        self,
        *,
        graph: OrchestraGraph,
        initial_artifacts: ArtifactBundle,
        context: RunContext,
    ) -> GraphExecutionResult:
        ...
```

Implement:

```text
NativeAsyncRuntime
```

Do not implement:

```text
LangGraphRuntime
SmolagentsRuntime
```

They may be added later as optional backends.

---

# 4. LiveCodeBench Constraints

## 4.1 Dataset

Use:

```text
dataset = livecodebench/code_generation_lite
scenario = codegeneration
release_version = release_v6
language = Python 3.11
final samples per task = 1
primary metric = pass@1
```

Pin both:

- the LiveCodeBench release version;
- the exact LiveCodeBench Git commit used for evaluation.

Do not use `release_latest` in committed experiment configs.

---

## 4.2 Test Visibility

The official problem object contains separate:

```text
public_test_cases
private_test_cases
```

The adapter must produce two separate data types.

```python
class AgentVisibleLCBTask(BaseModel):
    question_id: str
    question_title: str
    question_content: str
    platform: str
    contest_date: datetime
    starter_code: str
    difficulty: str
    public_test_cases: list[VisibleTestCase]
    metadata_public: dict


class HiddenLCBEvaluationRecord(BaseModel):
    question_id: str
    private_test_cases_ref: str
```

`AgentVisibleLCBTask` must not contain any private test data.

### Allowed use of public tests

Public tests may be used by:

- B1 runtime harness;
- B2 runtime harness;
- the repair prompt;
- offline analysis of B0 after the final code is frozen.

### Allowed use of private tests

Private tests may be used only by:

```text
FinalLCBEvaluator
```

after the graph run reaches:

```text
FINAL_OUTPUT_FROZEN
```

Hidden test failure details must not be returned to:

- any agent;
- the graph scheduler;
- the repair branch;
- Stage 2 search;
- normal agent-visible telemetry.

The final evaluator returns only the evaluation record needed for metrics.

---

## 4.3 Baseline Fairness Rule

All three baselines must use:

- the same problem statement representation;
- the same primary coding model unless an experiment explicitly changes it;
- the same maximum code-generation token limit;
- the same code parser;
- the same final private evaluator;
- the same sandbox;
- the same cost model;
- the same task manifest.

The additional calls made by B1 and B2 must be counted, not normalized away.

---

# 5. Baseline Definitions

## 5.1 B0 — Direct Single Agent

### Graph

```text
ProblemArtifact
      │
      ▼
DirectCoder
      │
      ▼
CodeParser
      │
      ▼
Freeze Final Code
      │
      ▼
Final Private Evaluation
```

### Behavior

- exactly one LLM call;
- no analyst;
- no public-test feedback before freeze;
- no repair;
- no role decomposition;
- one final solution.

The system may run deterministic parsing and syntax checks to store infrastructure diagnostics, but these results must not trigger another LLM call.

Public tests may be executed only after final freeze for analysis. They must not influence B0 output.

### Purpose

Measure the direct model capability and establish the lowest-cost baseline.

---

## 5.2 B1 — Single Agent + Harness

### Graph

```text
ProblemArtifact
      │
      ▼
DirectCoder
      │
      ▼
PublicCodeHarness
      │
      ├── PASS ──────────────► Freeze Final Code
      │
      └── FAIL
             │
             ▼
       SameCoderRepair
             │
             ▼
       PublicCodeHarness
             │
             ▼
     Deterministic Selector
             │
             ▼
       Freeze Final Code
```

### Behavior

- initial code uses the same `DirectCoder` contract as B0;
- public harness may trigger one repair;
- repair is performed by the same model family and same logical single-agent identity;
- maximum repair attempts: 1;
- deterministic selection between initial and repaired code;
- no analyst or independent specialist agent.

### Purpose

Isolate the value of execution feedback and one self-repair step.

---

## 5.3 B2 — Fixed MAS

B2 must exercise actual graph concurrency.

### Graph

```text
                           ┌──► AlgorithmAnalyst ───┐
ProblemArtifact ───────────┤                        │
                           └──► EdgeCaseAnalyst ────┤
                                                    ▼
                                               PlanMerger
                                                    │
                                                    ▼
                                              SolutionCoder
                                                    │
                                                    ▼
                                            PublicCodeHarness
                                                    │
                                  ┌─────────────────┴───────────────┐
                                  │ PASS                            │ FAIL
                                  ▼                                 ▼
                         Freeze Final Code                    RepairAgent
                                                                    │
                                                                    ▼
                                                           PublicCodeHarness
                                                                    │
                                                                    ▼
                                                        Deterministic Selector
                                                                    │
                                                                    ▼
                                                           Freeze Final Code
```

### Parallel nodes

The following nodes must run concurrently because they depend only on the problem:

```text
AlgorithmAnalyst
EdgeCaseAnalyst
```

`PlanMerger` waits for both outputs.

### Agent responsibilities

#### AlgorithmAnalyst

Produces:

- problem summary;
- candidate algorithm;
- correctness argument;
- complexity;
- required data structures.

#### EdgeCaseAnalyst

Produces:

- input/output interpretation;
- corner cases;
- overflow and indexing risks;
- functional-vs-stdin interface concerns;
- likely failure modes.

#### PlanMerger

May be implemented as either:

- an LLM node; or
- a deterministic structured merge.

Stage 1 default: deterministic structured merge.

It concatenates validated fields into one typed `CombinedPlanArtifact`.

#### SolutionCoder

Receives:

- visible problem;
- combined algorithm plan;
- edge-case report;
- starter code.

Produces complete Python code.

#### RepairAgent

Receives:

- visible problem;
- combined plan;
- previous code;
- visible public-test failure summary.

Produces revised code.

### Purpose

Measure whether explicit parallel specialist decomposition improves accuracy or stability relative to B1, with all additional cost reported.

---

# 6. Orchestra Intermediate Representation

## 6.1 Design Requirement

The three baselines must be three serialized graph configurations interpreted by the same runtime.

Do not create three independent Python pipelines.

---

## 6.2 Node Kinds

Support four node kinds in Stage 1:

```python
class NodeKind(str, Enum):
    AGENT = "agent"
    HARNESS = "harness"
    TRANSFORM = "transform"
    SELECTOR = "selector"
```

### AGENT

Performs one LLM call and produces a typed artifact.

### HARNESS

Runs deterministic or benchmark-bound evaluation.

### TRANSFORM

Performs deterministic artifact conversion or merge.

### SELECTOR

Chooses one artifact from multiple candidates according to a deterministic rule.

---

## 6.3 Node Specifications

Use a discriminated Pydantic union.

```python
class BaseNodeSpec(BaseModel):
    node_id: str
    node_kind: NodeKind
    input_slots: dict[str, str]
    output_slots: dict[str, str]
    timeout_seconds: float | None = None


class AgentNodeSpec(BaseNodeSpec):
    node_kind: Literal[NodeKind.AGENT]
    contract_id: str


class HarnessNodeSpec(BaseNodeSpec):
    node_kind: Literal[NodeKind.HARNESS]
    harness_id: str
    visibility: Literal["public"]


class TransformNodeSpec(BaseNodeSpec):
    node_kind: Literal[NodeKind.TRANSFORM]
    transform_id: str


class SelectorNodeSpec(BaseNodeSpec):
    node_kind: Literal[NodeKind.SELECTOR]
    selector_id: str
```

---

## 6.4 Agent Contract

```python
class AgentContract(BaseModel):
    contract_id: str
    role: str
    system_prompt_template: str
    user_prompt_template: str
    model: str
    temperature: float
    max_tokens: int
    allowed_tools: list[str] = []
    input_schema: str
    output_schema: str
    parser_id: str
    memory_scope: Literal["local", "task"] = "local"
```

Stage 1 agents do not call arbitrary tools. Their only inputs are structured artifacts.

The code harness and sandbox are runtime nodes, not tools available for free agent invocation.

---

## 6.5 Edges

```python
class EdgeCondition(BaseModel):
    source_field: str
    operator: Literal[
        "equals",
        "not_equals",
        "greater_than",
        "less_than",
        "is_true",
        "is_false",
    ]
    value: Any | None = None


class EdgeSpec(BaseModel):
    edge_id: str
    source_node: str
    source_output: str
    destination_node: str
    destination_input: str
    payload_policy: Literal["full"] = "full"
    condition: EdgeCondition | None = None
```

Examples:

```text
PublicCodeHarness.passed == true
    → Freeze transform

PublicCodeHarness.passed == false
    → RepairAgent
```

---

## 6.6 Orchestra Graph

```python
class OrchestraGraph(BaseModel):
    graph_id: str
    version: str
    nodes: list[
        AgentNodeSpec
        | HarnessNodeSpec
        | TransformNodeSpec
        | SelectorNodeSpec
    ]
    edges: list[EdgeSpec]
    initial_artifact_slots: dict[str, str]
    final_output_slot: str
    metadata: dict[str, Any] = {}
```

---

## 6.7 Graph Configuration Files

Create:

```text
configs/graphs/b0_direct.yaml
configs/graphs/b1_single_harness.yaml
configs/graphs/b2_fixed_mas.yaml
```

Graph changes must be possible by editing YAML, not Python source.

---

# 7. Artifact Model

## 7.1 Artifact Envelope

```python
class ArtifactEnvelope(BaseModel):
    artifact_id: str
    artifact_type: str
    schema_version: str
    producer_node_id: str
    task_id: str
    created_at: datetime
    payload: dict[str, Any]
    parent_artifact_ids: list[str]
    content_hash: str
```

Artifacts are immutable after commit.

---

## 7.2 Required Artifact Types

Implement:

```text
ProblemArtifact
AlgorithmPlanArtifact
EdgeCaseArtifact
CombinedPlanArtifact
CodeArtifact
PublicHarnessResultArtifact
VisibleFailureSummaryArtifact
RepairArtifact
FinalCodeArtifact
```

### ProblemArtifact

Must contain only agent-visible LiveCodeBench data.

### CombinedPlanArtifact

```python
class CombinedPlanArtifact(BaseModel):
    problem_summary: str
    algorithm: str
    correctness_argument: str
    time_complexity: str
    space_complexity: str
    data_structures: list[str]
    edge_cases: list[str]
    implementation_risks: list[str]
```

### CodeArtifact

```python
class CodeArtifact(BaseModel):
    language: Literal["python"]
    code: str
    source_node: str
```

---

## 7.3 Artifact Store

Interface:

```python
class ArtifactStore(ABC):
    @abstractmethod
    async def put(self, artifact: ArtifactEnvelope) -> None:
        ...

    @abstractmethod
    async def get(self, artifact_id: str) -> ArtifactEnvelope:
        ...

    @abstractmethod
    async def list_for_task(self, task_id: str) -> list[ArtifactEnvelope]:
        ...
```

Implement:

```text
FileArtifactStore
```

Storage layout:

```text
outputs/<experiment>/<run_id>/tasks/<task_id>/artifacts/<artifact_id>.json
```

Stage 1 does not require a database.

---

# 8. Graph Validation

Implement `GraphCompiler`.

It must reject a graph when:

- node IDs are duplicated;
- edge IDs are duplicated;
- an edge references an unknown node;
- source output slot does not exist;
- destination input slot does not exist;
- artifact schema types are incompatible;
- an unconditional cycle exists;
- no node can consume the initial problem artifact;
- the declared final output slot is unreachable;
- a conditional branch can never activate;
- an agent contract is missing;
- a harness ID or transform ID is missing.

Stage 1 graphs should be acyclic when conditional repair branches are expanded.

Do not implement general cyclic agent loops in Stage 1.

---

# 9. Native Async Runtime

## 9.1 Runtime State

```python
class NodeStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RuntimeState(BaseModel):
    run_id: str
    task_id: str
    graph_id: str
    node_status: dict[str, NodeStatus]
    node_outputs: dict[str, dict[str, str]]
    active_edges: set[str]
    inactive_edges: set[str]
    final_output_artifact_id: str | None
    frozen: bool = False
```

`node_outputs` maps:

```text
node_id → output_slot → artifact_id
```

---

## 9.2 Ready-Node Rule

A node is ready when:

1. it is `PENDING`;
2. every required input slot has an artifact;
3. every required incoming conditional edge has been resolved;
4. at least one valid path to the node remains active;
5. runtime budget has not been exceeded.

---

## 9.3 Wave Execution

Pseudo-code:

```python
async def execute_graph(
    graph: OrchestraGraph,
    initial_artifacts: ArtifactBundle,
    context: RunContext,
) -> GraphExecutionResult:
    state = initialize_state(graph, initial_artifacts)

    while not state.frozen:
        ready = scheduler.find_ready_nodes(graph, state)

        if not ready:
            if scheduler.can_finalize(graph, state):
                state = scheduler.finalize(graph, state)
                break
            raise GraphDeadlockError(build_deadlock_report(graph, state))

        results = await execute_wave(ready, state, context)

        state = await committer.commit_wave(
            graph=graph,
            previous_state=state,
            results=results,
        )

        await checkpoint_store.save(state)

    return build_graph_result(state)
```

---

## 9.4 Structured Node Concurrency

```python
async def execute_wave(
    node_ids: list[str],
    state: RuntimeState,
    context: RunContext,
) -> list[NodeExecutionResult]:
    task_map: dict[str, asyncio.Task[NodeExecutionResult]] = {}

    async with asyncio.TaskGroup() as group:
        for node_id in node_ids:
            task_map[node_id] = group.create_task(
                execute_node_safely(node_id, state, context),
                name=f"{context.task_id}:{node_id}",
            )

    return [task_map[node_id].result() for node_id in node_ids]
```

`execute_node_safely` must convert expected node failures into a returned result rather than raising them out of the `TaskGroup`.

```python
async def execute_node_safely(...) -> NodeExecutionResult:
    try:
        return await execute_node(...)
    except ExpectedNodeError as exc:
        return NodeExecutionResult.failed_from(exc)
```

Only infrastructure-corrupting errors should escape and cancel sibling tasks.

---

## 9.5 Atomic Wave Commit

Node tasks must not write shared runtime state.

After all ready nodes complete:

1. validate each returned artifact;
2. save artifacts;
3. update node status;
4. evaluate conditional outgoing edges;
5. mark impossible nodes as `SKIPPED`;
6. append telemetry events;
7. write one checkpoint.

This prevents race conditions and non-deterministic state mutation.

---

# 10. Concurrency Architecture

## 10.1 Four Independent Limits

```python
class RuntimeLimits(BaseModel):
    max_parallel_benchmark_tasks: int = 4
    max_parallel_nodes_per_task: int = 4
    max_parallel_llm_calls: int = 8
    max_parallel_sandboxes: int = 2
```

Create separate semaphores:

```python
task_semaphore = asyncio.Semaphore(max_parallel_benchmark_tasks)
llm_semaphore = asyncio.Semaphore(max_parallel_llm_calls)
sandbox_semaphore = asyncio.Semaphore(max_parallel_sandboxes)
```

`max_parallel_nodes_per_task` is enforced by the scheduler.

---

## 10.2 Benchmark Task Concurrency

Different LiveCodeBench tasks may execute concurrently.

```python
async def run_manifest(task_specs: list[TaskSpec]) -> list[TaskRunResult]:
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(run_task_limited(spec))
            for spec in task_specs
        ]
    return [task.result() for task in tasks]
```

`run_task_limited` acquires `task_semaphore`.

---

## 10.3 Graph Node Concurrency

Within one task, only dependency-ready nodes run concurrently.

For B2:

```text
Wave 1:
  AlgorithmAnalyst
  EdgeCaseAnalyst

Wave 2:
  PlanMerger

Wave 3:
  SolutionCoder

Wave 4:
  PublicCodeHarness

Wave 5:
  Freeze
  or RepairAgent

Wave 6 if repair:
  PublicCodeHarness

Wave 7:
  DeterministicSelector / Freeze
```

The runtime must record:

- total wall-clock graph latency;
- sum of node latencies;
- critical-path latency;
- concurrency savings.

---

## 10.4 LLM Concurrency

All agent nodes acquire the shared LLM semaphore.

The LLM client must support async calls.

```python
class AsyncLLMClient(ABC):
    @abstractmethod
    async def generate(...) -> LLMResponse:
        ...
```

For providers with synchronous SDKs, wrap the call using:

```python
await asyncio.to_thread(...)
```

only as a fallback.

Prefer direct async HTTP via `httpx.AsyncClient`.

---

## 10.5 Sandbox Concurrency

Harness nodes that execute generated code acquire the sandbox semaphore.

Do not run Docker operations while holding the LLM semaphore.

---

# 11. LLM Layer

## 11.1 Client Interface

```python
class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    estimated_cost_usd: float | None = None


class LLMResponse(BaseModel):
    text: str
    model: str
    latency_ms: int
    usage: LLMUsage
    provider_request_id: str | None = None


class AsyncLLMClient(ABC):
    @abstractmethod
    async def generate(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        timeout_seconds: float,
        metadata: dict[str, str],
    ) -> LLMResponse:
        ...
```

Implement:

```text
OpenAICompatibleAsyncClient
MockAsyncLLMClient
```

Do not implement multiple provider-specific abstractions in Stage 1.

---

## 11.2 Retries

Retry only:

- connection errors;
- server 5xx;
- explicit rate limit responses;
- transport timeout before any response.

Do not automatically retry:

- malformed model output;
- agent parse failure;
- low-quality solution;
- code failure.

These are agent/runtime outcomes and must be logged.

Use exponential backoff with bounded attempts.

---

# 12. Node Executors

## 12.1 Node Executor Registry

```python
class NodeExecutorRegistry:
    agent_executor: AgentNodeExecutor
    harness_executors: dict[str, HarnessExecutor]
    transforms: dict[str, TransformExecutor]
    selectors: dict[str, SelectorExecutor]
```

---

## 12.2 Agent Node Executor

Steps:

1. resolve input artifact IDs;
2. load typed artifacts;
3. render contract prompts;
4. call async LLM client;
5. parse output;
6. validate output schema;
7. create immutable artifact envelope;
8. return `NodeExecutionResult`.

---

## 12.3 Harness Node Executor

For `PublicCodeHarness`:

1. load `CodeArtifact`;
2. extract source code;
3. run `ast.parse`;
4. run compile check;
5. run public tests in sandbox;
6. compute public pass ratio;
7. create visible failure summary;
8. return `PublicHarnessResultArtifact`.

The harness is bound to the task, not to a specific LLM agent.

---

## 12.4 Transform Executor

Implement:

```text
merge_analysis_artifacts
freeze_code
build_repair_input
```

`merge_analysis_artifacts` must be deterministic in Stage 1.

---

## 12.5 Selector Executor

Implement deterministic solution selection.

Comparison order:

1. higher public-test pass ratio;
2. compile success;
3. fewer runtime errors;
4. fewer timeouts;
5. lower execution time;
6. if tied, initial solution.

Do not use an LLM judge.

---

# 13. Safe Code Execution

Generated code is untrusted.

## 13.1 Docker Sandbox

Use:

```text
Python 3.11 slim image
network disabled
read-only root filesystem
tmpfs for /tmp
CPU quota
memory limit
PID limit
wall-clock timeout
sanitized environment
no mounted credentials
```

Example policy:

```text
CPU: 1 core
memory: 512 MB
PIDs: 64
network: none
default public-test timeout: 10 seconds
```

The official LiveCodeBench checker should be invoked inside or through the isolated evaluator wrapper.

---

## 13.2 Sandbox Result

```python
class SandboxExecutionResult(BaseModel):
    compiled: bool
    passed_count: int
    total_count: int
    runtime_errors: int
    timeouts: int
    stderr_summary: str | None
    per_test_visible_results: list[VisibleTestResult]
    duration_ms: int
```

Private test results must use a separate non-agent-visible result type.

---

# 14. Final Evaluator Isolation

Implement:

```python
class FinalLCBEvaluator:
    async def evaluate_frozen_run(
        self,
        *,
        task_id: str,
        final_code: str,
        lcb_problem_ref: str,
    ) -> FinalEvaluationRecord:
        ...
```

Preconditions:

```text
runtime_state.frozen == true
final code artifact exists
no further graph nodes can execute
```

The wrapper must export official-compatible records:

```json
[
  {
    "question_id": "id1",
    "code_list": ["final code"]
  }
]
```

Record:

- evaluator commit;
- release version;
- timeout configuration;
- pass/fail;
- pass@1.

Do not save private input/output examples into normal task artifacts.

---

# 15. Repository Structure

```text
.
├── pyproject.toml
├── uv.lock
├── README.md
├── .env.example
│
├── configs/
│   ├── experiments/
│   │   ├── stage1_b0_direct.yaml
│   │   ├── stage1_b1_single_harness.yaml
│   │   └── stage1_b2_fixed_mas.yaml
│   ├── graphs/
│   │   ├── b0_direct.yaml
│   │   ├── b1_single_harness.yaml
│   │   └── b2_fixed_mas.yaml
│   ├── contracts/
│   │   ├── direct_coder.yaml
│   │   ├── algorithm_analyst.yaml
│   │   ├── edge_case_analyst.yaml
│   │   ├── solution_coder.yaml
│   │   ├── single_agent_repair.yaml
│   │   └── repair_agent.yaml
│   ├── manifests/
│   │   ├── lcb_smoke.json
│   │   ├── lcb_dev.json
│   │   └── lcb_heldout.json
│   └── pricing.yaml
│
├── third_party/
│   ├── LiveCodeBench/
│   └── LOCK.md
│
├── src/orchestra/
│   ├── __init__.py
│   ├── cli/
│   │   ├── prepare_lcb.py
│   │   ├── validate_graph.py
│   │   ├── run.py
│   │   ├── evaluate.py
│   │   └── summarize.py
│   │
│   ├── ir/
│   │   ├── graph.py
│   │   ├── nodes.py
│   │   ├── edges.py
│   │   ├── contracts.py
│   │   ├── artifacts.py
│   │   └── compiler.py
│   │
│   ├── runtime/
│   │   ├── backend.py
│   │   ├── native_async.py
│   │   ├── scheduler.py
│   │   ├── committer.py
│   │   ├── state.py
│   │   ├── limits.py
│   │   ├── checkpoint.py
│   │   └── errors.py
│   │
│   ├── executors/
│   │   ├── registry.py
│   │   ├── agent.py
│   │   ├── harness.py
│   │   ├── transform.py
│   │   └── selector.py
│   │
│   ├── llm/
│   │   ├── base.py
│   │   ├── openai_compatible_async.py
│   │   ├── mock_async.py
│   │   └── usage.py
│   │
│   ├── prompts/
│   │   ├── render.py
│   │   └── parsers.py
│   │
│   ├── adapters/
│   │   └── livecodebench/
│   │       ├── loader.py
│   │       ├── mapper.py
│   │       ├── manifest.py
│   │       ├── public_harness.py
│   │       ├── final_evaluator.py
│   │       └── exporter.py
│   │
│   ├── sandbox/
│   │   ├── base.py
│   │   ├── docker.py
│   │   └── result.py
│   │
│   ├── storage/
│   │   ├── artifacts.py
│   │   ├── events.py
│   │   └── runs.py
│   │
│   ├── telemetry/
│   │   ├── events.py
│   │   ├── collector.py
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
│   ├── e2e/
│   └── fixtures/
│
└── outputs/
    └── .gitkeep
```

---

# 16. Experiment Configuration

Example:

```yaml
experiment:
  name: stage1_b2_fixed_mas
  seed: 42
  graph_config: configs/graphs/b2_fixed_mas.yaml
  output_root: outputs/stage1

benchmark:
  name: livecodebench
  release_version: release_v6
  scenario: codegeneration
  language: python
  manifest: configs/manifests/lcb_smoke.json

runtime:
  backend: native_async
  max_parallel_benchmark_tasks: 4
  max_parallel_nodes_per_task: 4
  max_parallel_llm_calls: 8
  max_parallel_sandboxes: 2
  checkpoint_after_each_wave: true

evaluation:
  max_repair_attempts: 1
  final_samples_per_task: 1

logging:
  save_prompts: true
  save_raw_responses: true
  save_artifacts: true
  redact_environment: true
```

---

# 17. CLI Contract

## 17.1 Prepare Tasks

```bash
python -m orchestra.cli.prepare_lcb \
  --release-version release_v6 \
  --output configs/manifests/lcb_smoke.json \
  --num-easy 5 \
  --num-medium 5 \
  --num-hard 5 \
  --seed 42
```

---

## 17.2 Validate Graph

```bash
python -m orchestra.cli.validate_graph \
  --graph configs/graphs/b2_fixed_mas.yaml
```

Output:

```text
node count
edge count
entry nodes
parallel waves
conditional branches
reachable final output
schema validation result
```

---

## 17.3 Run One Baseline

```bash
python -m orchestra.cli.run \
  --config configs/experiments/stage1_b0_direct.yaml \
  --resume
```

Equivalent commands must work for B1 and B2.

Options:

```text
--task-id
--limit
--mock-llm
--dry-run
--force-rerun
```

---

## 17.4 Evaluate Frozen Outputs

```bash
python -m orchestra.cli.evaluate \
  --run-dir outputs/stage1/<run_id>
```

---

## 17.5 Summarize

```bash
python -m orchestra.cli.summarize \
  --run-dir outputs/stage1/<run_id>
```

---

# 18. Required Metrics

## 18.1 Accuracy

- hidden pass@1;
- public-test pass rate before repair;
- public-test pass rate after repair;
- compile success rate.

## 18.2 Cost

- prompt tokens;
- completion tokens;
- total model cost;
- sandbox execution count;
- total cost per task;
- cost per hidden-passed task.

## 18.3 Stability

- parse failure rate;
- syntax failure rate;
- runtime exception rate;
- timeout rate;
- repair trigger rate;
- repair success rate.

## 18.4 MAS Runtime

- graph wall-clock latency;
- sum of node latencies;
- critical-path latency;
- parallel node count;
- concurrency speedup;
- failed/skipped node count;
- checkpoint count.

## 18.5 Comparative Table

Produce:

| Metric | B0 Direct | B1 Single+Harness | B2 Fixed MAS |
|---|---:|---:|---:|
| Hidden pass@1 | | | |
| Avg. total tokens | | | |
| Avg. cost | | | |
| Cost per solved task | | | |
| Avg. wall time | | | |
| Compile failure | | | |
| Runtime failure | | | |
| Repair success | N/A | | |
| Parallel speedup | N/A | N/A | |

---

# 19. Telemetry Events

Required event types:

```text
RUN_STARTED
TASK_STARTED
GRAPH_COMPILED
WAVE_STARTED
NODE_READY
NODE_STARTED
NODE_COMPLETED
NODE_FAILED
NODE_SKIPPED
ARTIFACT_COMMITTED
CONDITIONAL_EDGE_ACTIVATED
CONDITIONAL_EDGE_DISABLED
WAVE_COMMITTED
CHECKPOINT_SAVED
FINAL_OUTPUT_FROZEN
PRIVATE_EVALUATION_STARTED
PRIVATE_EVALUATION_COMPLETED
TASK_COMPLETED
TASK_FAILED
RUN_COMPLETED
```

Event fields:

```python
class TelemetryEvent(BaseModel):
    timestamp: datetime
    run_id: str
    task_id: str | None
    graph_id: str
    event_type: str
    wave_id: int | None
    node_id: str | None
    artifact_ids: list[str] = []
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_usd: float | None = None
    status: str | None = None
    metadata: dict[str, Any] = {}
```

---

# 20. Checkpoint and Resume

Checkpoint after every committed wave.

Checkpoint contains:

- runtime state;
- node statuses;
- artifact references;
- active/inactive conditional edges;
- completed telemetry offset;
- budget usage;
- graph/config hashes.

Resume requirements:

- never repeat a successful LLM node by default;
- never repeat a successful sandbox node by default;
- verify artifact hashes;
- verify graph and contract config hashes;
- refuse resume when graph definition changed unless `--allow-config-drift` is explicitly set;
- never rerun private evaluation if a valid result exists.

---

# 21. Implementation Work Breakdown

## Milestone 1 — Project and Benchmark Adapter

Implement:

- project bootstrap;
- pinned LiveCodeBench dependency;
- release_v6 loader;
- public/private type separation;
- deterministic manifests;
- custom output exporter.

Acceptance:

- one official task maps to `AgentVisibleLCBTask`;
- no private test appears in the visible object;
- release and commit are recorded.

---

## Milestone 2 — Orchestra IR and Graph Compiler

Implement:

- node specs;
- edge specs;
- contracts;
- graph config loader;
- schema compatibility;
- reachability;
- cycle detection;
- conditional-edge validation.

Acceptance:

- all three baseline YAML graphs compile;
- intentionally invalid fixtures fail with clear messages;
- no baseline is hard-coded in runtime Python.

---

## Milestone 3 — Artifact and Run Storage

Implement:

- artifact envelope;
- typed payload registry;
- file artifact store;
- append-only event writer;
- run directory;
- content hashing.

Acceptance:

- artifacts round-trip;
- immutability is enforced;
- each output includes producer and parent lineage.

---

## Milestone 4 — Async LLM Layer

Implement:

- async client;
- mock client;
- contract prompt rendering;
- output parsing;
- token/cost extraction;
- semaphore integration.

Acceptance:

- parallel mock calls actually overlap;
- LLM semaphore limit is respected;
- one malformed output fails only its node.

---

## Milestone 5 — Native Async Graph Runtime

Implement:

- scheduler;
- ready-node computation;
- TaskGroup wave execution;
- safe exception wrapping;
- atomic commit;
- conditional edge resolution;
- skipped-node propagation;
- deadlock reporting.

Acceptance:

- B2 analysis nodes run in the same wave;
- coder starts only after both artifacts are committed;
- failed public harness activates repair branch;
- passed public harness disables repair branch;
- final output freezes correctly.

---

## Milestone 6 — Sandbox and Harness

Implement:

- Docker sandbox;
- code extraction;
- syntax/compile;
- public-test runner;
- visible failure summary;
- sandbox semaphore.

Acceptance fixtures:

- correct program;
- syntax error;
- wrong answer;
- runtime exception;
- timeout;
- functional-style problem;
- stdin-style problem.

---

## Milestone 7 — Baseline B0

Implement:

- direct coder contract;
- one-node graph;
- final freeze;
- private evaluation.

Acceptance:

- exactly one LLM call per task;
- public harness cannot affect output;
- one final code per task.

---

## Milestone 8 — Baseline B1

Implement:

- public harness branch;
- same-agent repair;
- deterministic selector.

Acceptance:

- repair is triggered only by visible failure;
- maximum one repair;
- private result cannot trigger repair.

---

## Milestone 9 — Baseline B2

Implement:

- parallel analyst contracts;
- deterministic plan merger;
- coder;
- repair agent;
- graph configuration.

Acceptance:

- two analyst calls execute concurrently;
- join is deterministic;
- all node-level costs are reported;
- graph can be changed through YAML.

---

## Milestone 10 — Evaluation and Comparison

Implement:

- official final evaluator wrapper;
- summary aggregation;
- baseline comparison table;
- cost-per-solved-task;
- concurrency metrics.

Acceptance:

- same manifest can be run under B0/B1/B2;
- final report aligns tasks by question ID;
- missing/failed tasks are explicit.

---

# 22. Test Plan

## Unit Tests

```text
test_lcb_visible_mapping.py
test_private_test_isolation.py
test_graph_schema.py
test_graph_compiler_cycles.py
test_graph_compiler_reachability.py
test_graph_conditional_edges.py
test_contract_loading.py
test_artifact_immutability.py
test_scheduler_ready_nodes.py
test_scheduler_join.py
test_scheduler_skip_branch.py
test_llm_semaphore.py
test_sandbox_semaphore.py
test_code_parser.py
test_public_harness.py
test_selector.py
test_checkpoint_hashes.py
```

## Integration Tests

```text
test_b0_mock_e2e.py
test_b1_public_pass_no_repair.py
test_b1_public_fail_repair.py
test_b2_parallel_analysis.py
test_b2_join_and_code.py
test_b2_repair_branch.py
test_resume_after_wave.py
test_private_eval_only_after_freeze.py
```

## Concurrency Tests

1. Mock both analyst nodes with 0.5-second delays.
2. Verify B2 wave completes in approximately 0.5 seconds rather than 1.0 second.
3. Verify `max_parallel_llm_calls=1` forces sequential execution.
4. Verify task-level semaphore limits active task count.
5. Verify sandbox semaphore limits public-test jobs.

## Security Tests

- no network from generated code;
- no environment secrets;
- timeout kills child processes;
- memory/PID limits work;
- no writes outside sandbox temp directory.

---

# 23. Smoke and Development Runs

## Smoke Manifest

```text
15 tasks
5 easy
5 medium
5 hard
seed = 42
release = release_v6
```

Run all three baselines.

## Initial Real-Model Bring-Up

Before the full smoke set:

```text
2 easy
2 medium
2 hard
```

Run B0 first, then B1, then B2.

Do not run all baselines at high concurrency until:

- telemetry is verified;
- sandbox isolation is verified;
- cost estimation is verified;
- private leakage audit passes.

---

# 24. Definition of Done

## Core System

- [ ] Orchestra IR is implemented.
- [ ] All baseline graphs are YAML-defined.
- [ ] Native async runtime executes branch/join graphs.
- [ ] B2 runs two analysis nodes concurrently.
- [ ] Conditional repair branches work.
- [ ] Artifacts are typed, immutable, and traceable.
- [ ] Checkpoint/resume works.

## Baselines

- [ ] B0 uses exactly one LLM call.
- [ ] B1 isolates harness/self-repair value.
- [ ] B2 is a genuine multi-agent DAG.
- [ ] Same primary coding model can be used across all modes.
- [ ] All costs are reported.

## LiveCodeBench

- [ ] release_v6 and evaluator commit are pinned.
- [ ] public/private tests are isolated.
- [ ] final output is frozen before private evaluation.
- [ ] one final code is exported per task.
- [ ] pass@1 is computed through official-compatible evaluation.

## Concurrency

- [ ] task concurrency works.
- [ ] ready-node concurrency works.
- [ ] LLM semaphore works.
- [ ] sandbox semaphore works.
- [ ] runtime reports critical path and parallel speedup.

## Testing

- [ ] unit tests pass;
- [ ] integration tests pass;
- [ ] security tests pass;
- [ ] six-task real-model bring-up completes;
- [ ] all three 15-task smoke runs complete.

---

# 25. Stage 2 Extension Contract

Stage 2 must be able to use Stage 1 without changing the runtime.

It should add:

```text
OrchestraCandidateGenerator
ParetoArchive
CandidateEvaluationBudget
PreferenceSelector
```

A Stage 2 candidate is simply another `OrchestraGraph` plus contract references.

Required Stage 1 APIs:

```python
graph.clone()
graph.apply_atomic_edit(edit)
graph.diff(other_graph)
runtime.execute(graph, initial_artifacts, context)
metrics.compute_graph_objectives(result)
```

Stage 1 must implement `clone()` and deterministic graph serialization, but must not implement dynamic edit logic.

---

# 26. Coding Agent Stop Conditions

Stop and report a blocker rather than silently expanding scope if:

- the official LiveCodeBench schema differs materially from the adapter;
- private tests cannot be isolated;
- official evaluation cannot be reproduced;
- Docker is unavailable;
- conditional graph execution requires introducing general cycles;
- an agent framework appears necessary;
- a requested change introduces Pareto search;
- a requested change introduces dynamic graph edits;
- async provider support is unavailable.

Blocker report format:

```text
Module:
Observed behavior:
Reproduction command:
Error output:
Minimal proposed fix:
Does the fix alter Stage 1 scope? yes/no
```

Do not solve a Stage 1 blocker by introducing smolagents, LangGraph, AutoGen, or Stage 2 search logic.
