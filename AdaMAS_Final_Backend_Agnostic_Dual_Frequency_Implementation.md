# AdaMAS 最终架构落地方案
## Backend-Agnostic Dual-Frequency Runtime with Subtask Decomposition

**目标仓库：** `Thedatababbler/codename_Madagascar`  
**执行对象：** Coding Agent  
**状态：** 最终技术选型与分阶段实施规范  
**核心决策：** 保留现有 Orchestra runtime，不迁移到 EvoMAS runtime；将 smolagents CodeAgent、SWE-agent 和现有 Structured LLM 接成可插拔执行后端。

---

# 1. 最终技术决策

## 1.1 保留的核心

以下现有能力必须保留并继续作为系统的 control plane：

- typed `OrchestraGraph`
- YAML graph configuration
- dependency-ready scheduling
- `asyncio.TaskGroup` wave concurrency
- atomic wave commit
- immutable artifact 与 parent lineage
- checkpoint/resume
- conditional edges
- 独立 task / LLM / sandbox semaphore
- append-only telemetry
- benchmark-specific harness adapter

不要迁移到 EvoMAS 的 `MasRuntime`，不要用 EvoMAS 的完整 query-level evolution loop 替代现有 runtime。

## 1.2 新增的核心

现有 runtime 需要扩展为：

```text
AdaMAS Control Plane
├── Task decomposition
├── Subtask DAG and lifecycle
├── Local subgraph execution
├── Fast-frequency local update
├── Slow-frequency global update
├── Artifact / checkpoint / telemetry
├── Multi-objective evaluation
└── Agent backend registry
    ├── Structured LLM
    ├── smolagents CodeAgent
    ├── ToolCallingAgent（可选）
    ├── SWE-agent / mini-SWE-agent
    └── Deterministic executor
```

## 1.3 框架职责边界

AdaMAS 负责：

- 任务如何拆解；
- 子任务依赖；
- 每个子任务使用什么 local graph；
- Agent 角色、模型、工具和 contract；
- 节点调度和并发；
- artifact communication；
- keystone harness；
- 局部快速修改；
- 全局慢速修改；
- Pareto objective 与候选选择；
- checkpoint 和实验记录。

外部 Agent backend 负责：

- 单个 Agent 内部的 action loop；
- 工具调用；
- action parse；
- execution observation；
- action-level error recovery；
- repo/file interaction；
- final answer 或 patch submission。

禁止让外部 backend 自己管理顶层 MAS。尤其不要使用 smolagents 的 `managed_agents` 在节点内部再建立一个不可见的嵌套 MAS。AdaMAS 必须是唯一的多 Agent 调度器。

---

# 2. 目标架构

```text
TaskAdapter
    │
    ▼
TaskDecomposer
    │
    ▼
TaskPlan / Subtask DAG
    │
    ▼
DualFrequencyController
    ├── FastLoopController
    │     ├── select ready subtasks
    │     ├── instantiate local OrchestraGraph
    │     ├── execute through NativeAsyncRuntime
    │     ├── run KeystoneHarness
    │     └── retry or apply LocalEdit
    │
    └── SlowLoopController
          ├── inspect committed subtask artifacts
          ├── run TaskProgressHarness
          ├── update future communication plan
          ├── revise pending local graphs
          └── reallocate future budget/backend/model
                    │
                    ▼
            NativeAsyncRuntime
                    │
                    ▼
             AgentBackendRegistry
     ┌──────────────┼──────────────────────┐
     ▼              ▼                      ▼
StructuredLLM   SmolagentsCodeAgent    SWEAgentProcess
```

---

# 3. 实施原则

1. **不要推倒重写。** 通过 adapter 和 compatibility layer 渐进重构。
2. **现有 LiveCodeBench 路径必须始终可运行。**
3. **所有 LLM 生成的系统配置必须经过 Pydantic schema 验证。**
4. **禁止生成并执行自由形式的 orchestration Python code。**
5. **所有 backend-specific object 必须在 adapter 内终止，不能泄漏到 runtime。**
6. **所有 fallback 必须显式配置并进入 telemetry，禁止静默 fallback。**
7. **execution success 与 task accuracy 必须分开统计。**
8. **快频更新只能作用于当前 subtask 的 local graph。**
9. **慢频更新默认只能修改尚未执行的 subtask 和未来通信。**
10. **已经 commit 的 artifact 不得原地修改。**

---

# 4. 新目录结构

在现有 `src/orchestra` 下新增：

```text
src/orchestra/
├── backends/
│   ├── __init__.py
│   ├── base.py
│   ├── capabilities.py
│   ├── registry.py
│   ├── structured_llm.py
│   ├── smolagents_code.py
│   ├── smolagents_model.py
│   ├── sweagent_process.py
│   ├── deterministic.py
│   └── workers/
│       ├── __init__.py
│       └── smolagents_worker.py
│
├── tools/
│   ├── __init__.py
│   ├── base.py
│   ├── registry.py
│   ├── bbeh_tools.py
│   └── coding_tools.py
│
├── tasks/
│   ├── __init__.py
│   ├── base.py
│   ├── registry.py
│   ├── livecodebench.py
│   ├── bbeh.py
│   └── swebench.py
│
├── decomposition/
│   ├── __init__.py
│   ├── schemas.py
│   ├── decomposer.py
│   ├── validator.py
│   └── fallback.py
│
├── control/
│   ├── __init__.py
│   ├── task_state.py
│   ├── subtask_scheduler.py
│   ├── fast_loop.py
│   ├── slow_loop.py
│   ├── triggers.py
│   └── dual_frequency.py
│
├── communication/
│   ├── __init__.py
│   ├── plan.py
│   ├── payload.py
│   └── aggregation.py
│
├── edits/
│   ├── __init__.py
│   ├── schemas.py
│   ├── validator.py
│   ├── apply.py
│   └── diff.py
│
└── objectives/
    ├── __init__.py
    ├── schemas.py
    ├── metrics.py
    └── pareto.py
```

不要立即移动或删除现有：

```text
src/orchestra/adapters/livecodebench
src/orchestra/executors
src/orchestra/runtime
src/orchestra/harness
```

第一轮只允许通过 wrapper 接入新接口。旧接口在新接口稳定后再标记 deprecated。

---

# 5. Agent Backend 抽象

## 5.1 Backend protocol

新增 `src/orchestra/backends/base.py`：

```python
from __future__ import annotations

from typing import Protocol

class AgentBackend(Protocol):
    @property
    def backend_id(self) -> str:
        ...

    @property
    def capabilities(self) -> BackendCapabilities:
        ...

    async def run(
        self,
        request: AgentRequest,
        context: BackendExecutionContext,
    ) -> AgentResult:
        ...

    async def healthcheck(self) -> BackendHealth:
        ...
```

## 5.2 标准请求

```python
class AgentRequest(BaseModel):
    request_id: str
    task_id: str
    subtask_id: str | None = None
    node_id: str
    role: str
    instruction: str
    input_artifacts: list[ArtifactRef]
    rendered_context: str
    model: ModelSpec
    tools: list[str] = []
    max_steps: int = 1
    timeout_seconds: float
    output_contract: OutputContract
    backend_config: dict[str, Any] = {}
```

## 5.3 标准结果

```python
class AgentResult(BaseModel):
    request_id: str
    backend_id: str
    status: AgentRunStatus
    final_output: str | None
    output_artifacts: list[ArtifactEnvelope] = []
    trace_events: list[AgentTraceEvent] = []
    usage: UsageRecord
    latency_ms: int
    step_count: int
    error: AgentError | None = None
    backend_metadata: dict[str, Any] = {}
```

`AgentRunStatus` 至少包含：

```text
SUCCESS
INVALID_REQUEST
BACKEND_UNAVAILABLE
BACKEND_INIT_FAILURE
MODEL_FAILURE
ACTION_PARSE_FAILURE
TOOL_FAILURE
MAX_STEPS_EXCEEDED
OUTPUT_CONTRACT_FAILURE
TIMEOUT
CANCELLED
INFRA_ERROR
```

## 5.4 Backend capability

```python
class BackendCapabilities(BaseModel):
    multi_step: bool
    code_actions: bool
    structured_tools: bool
    repository_editing: bool
    supports_remote_executor: bool
    supports_step_trace: bool
    supports_resume: bool
```

Runtime 不得根据 backend 名称写硬编码条件，应根据 capability 和 task requirement 验证兼容性。

## 5.5 Registry

新增：

```python
class AgentBackendRegistry:
    def register(self, backend: AgentBackend) -> None:
        ...

    def get(self, backend_id: str) -> AgentBackend:
        ...

    def validate_request(self, request: AgentRequest) -> None:
        ...
```

要求：

- backend 名称唯一；
-启动时完成 registry validation；
- graph compile 时验证 backend 存在；
-禁止在执行过程中动态 import 任意字符串路径；
-缺失 backend 必须 fail closed。

---

# 6. 修改现有 AgentNodeSpec

扩展现有 graph node schema，使用 discriminated backend config：

```yaml
id: solver
kind: agent
role: numerical_reasoner

backend:
  type: smolagents_code
  max_steps: 8
  executor_type: local
  use_structured_outputs_internally: true
  additional_authorized_imports: []

model:
  provider: openai_compatible
  name: MODEL_NAME
  temperature: 0.2
  max_tokens: 4096

tools:
  - python_math
  - final_answer

output_contract:
  type: final_answer
  answer_format: single_line
```

兼容旧配置：

```yaml
model: ...
prompt: ...
```

Graph loader 在读取旧配置时自动转换为：

```text
backend.type = structured_llm
```

旧配置转换必须确定性完成并记录 warning，不得改变现有 B0/B1/B2 行为。

---

# 7. Structured LLM backend

新增 `src/orchestra/backends/structured_llm.py`。

它包装当前 `llm` client 和现有 Agent executor。目标是先完成无行为变化迁移。

要求：

- 现有 model client、pricing 和 token telemetry 保持不变；
- 现有 coder/analyst/repair prompt 行为保持不变；
-原 `AgentNodeExecutor` 改为调用 `AgentBackendRegistry`；
-不得在 executor 中直接判断 smolagents 或 SWE-agent；
-现有测试必须全部通过。

第一阶段完成后，所有现有 graph 都应通过：

```text
AgentNodeExecutor
→ BackendRegistry
→ StructuredLLMBackend
```

---

# 8. smolagents CodeAgent backend

## 8.1 依赖策略

在 `pyproject.toml` 增加 optional dependency：

```toml
[project.optional-dependencies]
smolagents = [
  "smolagents==1.26.0"
]
```

必须 pin 精确版本。smolagents Agent API 属于实验性接口，不允许使用浮动版本。

更新：

```text
uv.lock
third_party/versions.yaml
CHANGELOG.md
```

## 8.2 使用范围

CodeAgent 用于：

- BBEH；
-复杂数学和逻辑推理；
-需要 Python 中间计算的任务；
-多步骤工具组合；
-WorkBench 一类工具任务。

CodeAgent 不负责：

-顶层 MAS 调度；
-任务拆解；
-子任务依赖；
-跨 Agent 通信；
-快频/慢频更新；
-Pareto 选择。

## 8.3 禁止 nested managed agents

初始化 CodeAgent 时：

```python
managed_agents = None
```

AdaMAS graph 中的每一个 agent node 对应一个独立 CodeAgent run。不要允许 CodeAgent 内部再调用另一个 agent。

## 8.4 独立 worker

不要直接在主 async event loop 中执行 `agent.run()`。

实现：

```text
SmolagentsCodeBackend
    ↓
spawn smolagents_worker process
    ↓
worker constructs model/tools/CodeAgent
    ↓
agent.run(task)
    ↓
normalized JSON result
```

文件：

```text
src/orchestra/backends/smolagents_code.py
src/orchestra/backends/workers/smolagents_worker.py
```

父进程要求：

- `multiprocessing` 使用 `spawn`；
-每个 worker 独立 process group；
-外层 wall timeout；
-超时杀死整个 process group；
-返回 JSON-compatible result；
-异常不能传播并终止 Orchestra runtime；
-受现有 LLM semaphore 和 backend semaphore 控制。

## 8.5 Worker request

请求中只传：

```text
model spec
task instruction
rendered context
tool names
max steps
executor type
authorized imports
output contract
trace settings
```

工具通过 worker 内部 `ToolRegistry` 根据名称构建。不要 pickle 任意函数、闭包或 Agent object。

## 8.6 CodeAgent 构造

逻辑等价于：

```python
agent = CodeAgent(
    tools=resolved_tools,
    model=model,
    max_steps=config.max_steps,
    planning_interval=config.planning_interval,
    additional_authorized_imports=config.additional_authorized_imports,
    executor_type=config.executor_type,
    use_structured_outputs_internally=True,
    return_full_result=True,
    step_callbacks=[trace_callback],
)
```

所有参数必须由 typed config 控制。

默认值：

```yaml
max_steps: 8
planning_interval: null
executor_type: local
additional_authorized_imports: []
use_structured_outputs_internally: true
```

不得依赖 smolagents 自己的默认 `max_steps`。

## 8.7 安全策略

`executor_type: local` 仅允许用于受控研究环境。

正式配置支持：

```text
local
e2b
modal
blaxel
docker
```

安全规则：

- 默认 authorized imports 为空；
-网络只能通过显式 tool 暴露；
-不要授权 `os`、`subprocess`、`sys`、`pathlib`、`shutil`；
-远程 executor 使用 context manager 并确保 cleanup；
-executor 类型和 cleanup status 写入 telemetry；
-local executor 失败时不得静默切换远程或反向切换；
-正式论文 run 必须记录 executor 类型。

## 8.8 Model adapter

新增 `smolagents_model.py`。

优先实现一个显式 model factory，不要让节点直接创建任意 smolagents model：

```python
class SmolagentsModelFactory:
    def create(self, spec: ModelSpec):
        ...
```

第一版允许使用 smolagents 官方 OpenAI-compatible 或 LiteLLM model wrapper，但必须：

-复用现有 endpoint、model name 和 temperature；
-记录 prompt/completion tokens；
-记录每个 step 的模型调用；
-将总 usage 汇总到 `AgentResult.usage`；
-禁止通过 model wrapper 绕过 pricing telemetry。

## 8.9 Trace

每个 CodeAgent step 归一化成：

```python
class AgentTraceEvent(BaseModel):
    index: int
    event_type: Literal[
        "model_call",
        "action",
        "observation",
        "tool_error",
        "planning",
        "final_answer",
    ]
    timestamp: datetime
    summary: str
    payload_ref: str | None
    token_usage: UsageRecord | None
```

完整 raw trace 保存到 run 目录，主 artifact 只保存引用和摘要，避免 context 膨胀。

---

# 9. Tool Registry

新增：

```python
class ToolRegistry:
    def register(self, tool_id: str, factory: ToolFactory) -> None:
        ...

    def build(
        self,
        tool_ids: list[str],
        context: ToolBuildContext,
    ) -> list[Any]:
        ...
```

规则：

- graph 只能引用 allowlist 中的 tool ID；
-工具 schema 在 graph compile 时验证；
-工具调用和异常进入 trace；
-工具不得直接访问 private benchmark labels；
-工具必须声明 side-effect level；
-写工具必须声明 workspace scope；
-同一 tool ID 在不同 backend 下可有 adapter，但语义必须一致。

BBEH 第一批工具：

```text
python_math
calculator
final_answer
```

搜索或网络工具不在第一批实现范围。

---

# 10. SWE-agent backend

## 10.1 集成方式

不要把 SWE-agent 内部 Python API直接 import 到 core runtime。

使用稳定的 process adapter：

```text
SWEAgentProcessBackend
    ↓
temporary clean workspace
    ↓
pinned SWE-agent / mini-SWE-agent CLI or runner
    ↓
trajectory + git diff / patch
    ↓
normalized AgentResult
```

原因：

- SWE-agent 内部 API 更新较快；
-依赖重；
-环境和 repo state 复杂；
-process boundary 更容易 timeout、清理和复现。

## 10.2 版本固定

新增：

```yaml
# third_party/versions.yaml
sweagent:
  repository: SWE-agent/SWE-agent
  commit: "<PINNED_COMMIT>"
```

不得跟随 `main`。

## 10.3 输出来源

优先级：

```text
valid submitted patch
→ git diff
→ staged git diff
→ explicit failure
```

不要使用自然语言 final response 当作代码 patch。

## 10.4 Workspace 生命周期

每个 task：

```text
create clean workspace
checkout exact task commit
run agent
collect patch and trajectory
evaluate
destroy or archive workspace
```

禁止不同任务共享未清理的 repo。

## 10.5 实施顺序

SWE-agent adapter 不属于第一批强制实现。

先完成：

```text
Backend Protocol
StructuredLLM backend
smolagents CodeAgent backend
BBEH vertical slice
Subtask and dual-frequency control
```

之后再实现 SWE-agent adapter。

---

# 11. Task Adapter 抽象

新增 `src/orchestra/tasks/base.py`：

```python
class TaskAdapter(Protocol):
    @property
    def task_type(self) -> str:
        ...

    def load_instance(self, raw: Any) -> TaskInstance:
        ...

    def build_root_artifacts(
        self,
        instance: TaskInstance,
    ) -> list[ArtifactEnvelope]:
        ...

    def build_decomposition_context(
        self,
        instance: TaskInstance,
    ) -> str:
        ...

    async def run_keystone_harness(
        self,
        subtask: SubtaskSpec,
        result: SubtaskResult,
    ) -> HarnessResult:
        ...

    async def run_task_progress_harness(
        self,
        state: TaskExecutionState,
    ) -> HarnessResult:
        ...

    async def run_final_evaluation(
        self,
        state: TaskExecutionState,
    ) -> FinalEvaluationResult:
        ...
```

Adapter 负责 benchmark 语义，不负责 graph scheduling。

## 11.1 BBEH adapter

实现：

```text
src/orchestra/tasks/bbeh.py
```

职责：

-加载题目；
-构建 root artifact；
-定义 final answer contract；
-使用 BBEH evaluator/extractor；
-不把 reference answer 暴露给 Agent；
-支持 single-task fallback；
-输出 accuracy 和 answer-extraction status。

第一版 BBEH 可以不拆解，先将每个题目包装成单一 subtask，以验证 CodeAgent backend。

随后再开启 decomposition。

## 11.2 LiveCodeBench adapter

现有：

```text
src/orchestra/adapters/livecodebench
```

暂时保留。

新增 `tasks/livecodebench.py` 作为 facade，内部调用现有 loader、public harness 和 final evaluator。不要在第一轮搬迁旧代码。

## 11.3 SWE-bench adapter

只创建 interface stub 和 typed schemas，等 SWE-agent backend 完成后再实现。

---

# 12. Subtask IR

新增 `src/orchestra/decomposition/schemas.py`。

```python
class SubtaskSpec(BaseModel):
    subtask_id: str
    title: str
    objective: str
    dependencies: list[str]
    input_artifacts: list[ArtifactRef]
    expected_outputs: list[OutputContract]
    keystone_harness_id: str
    local_graph_template: str
    budget: BudgetSpec
    priority: int = 0
    metadata: dict[str, Any] = {}
```

```python
class TaskPlan(BaseModel):
    task_id: str
    subtasks: list[SubtaskSpec]
    final_aggregation: AggregationSpec
    communication_plan: CommunicationPlan
    decomposition_rationale: str
    plan_version: int
```

验证要求：

- `subtask_id` 唯一；
-依赖引用存在；
-DAG 无环；
-至少一个 source subtask；
-至少一个 terminal subtask；
-每个 subtask 都有 harness；
-local graph template 存在；
-budget 为正；
-最多允许固定数量 subtask；
-禁止 LLM 生成 executable Python。

默认限制：

```yaml
min_subtasks: 1
max_subtasks: 8
max_dependency_depth: 6
```

## 12.1 Fallback

如果 decomposition：

-解析失败；
-schema 不合法；
-DAG 有环；
-超出限制；
-缺少 harness；

则确定性 fallback 为：

```text
一个 subtask
objective = original task
local_graph_template = configured default graph
```

Fallback 必须记录：

```text
decomposition_status = FALLBACK_SINGLE_SUBTASK
```

不得让 decomposition failure 导致整个任务无法执行。

---

# 13. Task execution state

新增 `src/orchestra/control/task_state.py`：

```python
class SubtaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    HARNESS_FAILED = "harness_failed"
    RETRY_PENDING = "retry_pending"
    COMMITTED = "committed"
    FAILED = "failed"
    SKIPPED = "skipped"
```

```python
class SubtaskState(BaseModel):
    spec: SubtaskSpec
    status: SubtaskStatus
    attempts: list[SubtaskAttempt]
    committed_artifacts: list[ArtifactRef]
    current_graph_hash: str
    local_revision: int
```

```python
class TaskExecutionState(BaseModel):
    task_id: str
    task_plan: TaskPlan
    subtasks: dict[str, SubtaskState]
    artifact_store_ref: str
    communication_plan: CommunicationPlan
    fast_loop_history: list[LocalUpdateRecord]
    slow_loop_history: list[GlobalUpdateRecord]
    global_revision: int
    frozen: bool = False
```

状态必须可 JSON 序列化并进入 checkpoint。

---

# 14. Communication Plan

新增：

```python
class CommunicationPlan(BaseModel):
    payload_contracts: list[PayloadContract]
    context_budgets: dict[str, int]
    delivery_schedule: list[DeliveryRule]
    aggregation_rules: list[AggregationRule]
    version: int
```

通信内容不是“把所有历史拼接给下游”。

每条 payload 必须声明：

```text
source subtask/node
target subtask/node
artifact type
required fields
max tokens
delivery condition
aggregation rule
```

默认只允许 forward communication。

慢频更新不得修改已经被消费并 commit 的历史 payload。修改只对未来 delivery 生效。

---

# 15. 快频局部更新

新增：

```text
FastLoopController
```

触发条件：

- keystone harness fail；
-output contract fail；
-backend max-steps exceeded；
-tool failure；
-local quality below threshold；
-local execution stability failure。

默认不因基础设施错误直接修改 graph。基础设施错误先按照 infra retry policy 处理。

## 15.1 Fast loop 生命周期

```text
select ready subtask
→ instantiate local graph
→ execute local graph
→ run keystone harness
→ pass: atomic commit
→ fail: diagnose
→ choose bounded LocalEdit
→ execute revised local graph
→ pass or exhaust budget
```

## 15.2 LocalEdit

```python
class LocalEdit(BaseModel):
    edit_id: str
    subtask_id: str
    edit_type: Literal[
        "prompt",
        "model",
        "tool",
        "local_edge",
        "local_agent",
        "budget",
    ]
    target_id: str
    parameters: dict[str, Any]
    reason: str
    parent_graph_hash: str
```

要求：

-一次只允许一个 atomic edit；
-edit 必须通过 schema 和 graph validation；
-只修改当前 subtask 的 graph；
-不修改其他 subtask；
-不修改已 commit artifact；
-失败后可以回退 parent graph；
-每次 edit 生成新 graph hash。

默认限制：

```yaml
fast_loop:
  max_attempts_per_subtask: 2
  max_edits_per_subtask: 1
  allow_edit_types:
    - prompt
    - model
    - tool
```

第一版不要开放任意 topology mutation。先实现 prompt/model/tool 三类。

---

# 16. 慢频全局更新

新增：

```text
SlowLoopController
```

触发方式：

```yaml
slow_loop:
  trigger:
    every_n_committed_subtasks: 2
    on_phase_boundary: true
    on_global_progress_failure: true
```

## 16.1 输入

Slow loop 读取：

-已 commit artifact 摘要；
-各 subtask harness result；
-cost / latency / stability；
-communication trace；
-pending subtasks；
-current global plan。

默认不读取 private final labels。

## 16.2 可修改对象

慢频更新第一版仅允许：

-未来 payload 内容；
-context budget；
-future delivery schedule；
-aggregation rule；
-pending subtask 的 local graph template；
-pending subtask 的 model/backend/tool allocation；
-pending subtask priority。

禁止：

-原地修改 committed artifact；
-删除已经完成的 subtask；
-回滚到任意历史状态；
-重新运行全部已完成 subtask；
-改变 hidden/private evaluation result；
-根据 final hidden answer 调参。

## 16.3 GlobalEdit

```python
class GlobalEdit(BaseModel):
    edit_id: str
    edit_type: Literal[
        "payload_contract",
        "context_budget",
        "schedule",
        "aggregation",
        "pending_graph",
        "future_backend",
        "future_model",
        "future_tool",
    ]
    targets: list[str]
    parameters: dict[str, Any]
    reason: str
    parent_plan_version: int
```

每次 slow update 生成：

```text
new CommunicationPlan.version
new TaskPlan.plan_version
new TaskExecutionState.global_revision
```

---

# 17. 双频控制器

新增 `src/orchestra/control/dual_frequency.py`：

```python
class DualFrequencyController:
    async def run(
        self,
        instance: TaskInstance,
        task_plan: TaskPlan,
    ) -> TaskExecutionState:
        ...
```

核心逻辑：

```python
while not state.is_terminal():
    ready = scheduler.ready_subtasks(state)

    batch = scheduler.select_batch(
        ready,
        max_parallel=config.max_parallel_subtasks,
    )

    results = await run_subtasks(batch)

    for result in results:
        harness = await task_adapter.run_keystone_harness(...)

        if harness.passed:
            commit_subtask(result)
        else:
            await fast_loop.handle_failure(...)

    if slow_loop_trigger.should_run(state):
        global_harness = await task_adapter.run_task_progress_harness(state)
        await slow_loop.maybe_update(state, global_harness)

state.frozen = True
await task_adapter.run_final_evaluation(state)
```

所有 commit 必须复用现有 atomic commit/checkpoint 机制。

---

# 18. Edit application 和 diff

现有 `OrchestraGraph` 增加：

```python
def apply_atomic_edit(self, edit: LocalEdit) -> OrchestraGraph:
    ...

def diff(self, other: OrchestraGraph) -> GraphDiff:
    ...
```

要求：

-原 graph 不可变；
-返回 deep-cloned new graph；
-edit 后重新运行 graph validation；
-保存 parent hash；
-diff 可序列化；
-无效 edit 抛出 typed error；
-不允许部分应用。

`GraphDiff` 至少包含：

```text
added nodes
removed nodes
modified nodes
added edges
removed edges
modified contracts
model/tool changes
```

---

# 19. Objective 与 Pareto 接口

本次重构必须定义接口，但 Pareto 搜索可以最后实现。

```python
class ObjectiveVector(BaseModel):
    quality: float
    cost: float
    stability: float
```

约定：

- quality 越大越好；
- cost 越小越好；
- stability 越大越好。

Stability 不等于 task accuracy，至少考虑：

```text
config validation
backend initialization
action parse
tool execution
output contract
timeout
infra retry
```

新增：

```python
class CandidateEvaluation(BaseModel):
    candidate_id: str
    task_id: str
    subtask_id: str | None
    objectives: ObjectiveVector
    graph_hash: str
    plan_version: int
    execution_status: str
```

Pareto archive 第一版只需：

- dominance check；
-nondominated insert；
-deduplicate；
-max archive size；
-deterministic tie-break。

---

# 20. Execution success 指标

必须明确区分：

## 20.1 System execution success

一次运行满足以下全部条件：

```text
configuration valid
task plan valid
all required backends initialized
runtime completed
final output contract valid
evaluator completed
no infrastructure failure
```

定义：

```python
execution_success: bool
```

## 20.2 Task success

由 benchmark evaluator 决定：

```text
BBEH answer correct
LiveCodeBench hidden tests passed
SWE-bench issue resolved
```

定义：

```python
task_success: bool
```

禁止将二者混为一个 success rate。

## 20.3 分层错误统计

必须报告：

```text
configuration_failure
decomposition_failure
backend_init_failure
model_failure
action_parse_failure
tool_failure
output_contract_failure
code_compile_failure
task_timeout
worker_timeout
infra_error
wrong_answer
```

---

# 21. 配置示例

## 21.1 BBEH + CodeAgent

新增：

```text
configs/tasks/bbeh.yaml
configs/graphs/bbeh_single_codeagent.yaml
configs/experiments/bbeh_codeagent_smoke.yaml
```

示例：

```yaml
task:
  type: bbeh
  split: mini

decomposition:
  enabled: false
  fallback_graph: bbeh_single_codeagent

graph:
  path: configs/graphs/bbeh_single_codeagent.yaml

runtime:
  max_parallel_tasks: 2
  max_parallel_subtasks: 2
  max_parallel_backends: 2

backend_defaults:
  smolagents_code:
    max_steps: 8
    executor_type: local
    use_structured_outputs_internally: true
    additional_authorized_imports: []

dual_frequency:
  fast_loop:
    enabled: false
  slow_loop:
    enabled: false
```

Graph：

```yaml
graph_id: bbeh_single_codeagent

nodes:
  - id: solver
    kind: agent
    role: problem_solver
    backend:
      type: smolagents_code
      max_steps: 8
      executor_type: local
    model:
      provider: openai_compatible
      name: ${CODEAGENT_MODEL}
      temperature: 0.2
    tools:
      - python_math
      - final_answer
    output_contract:
      type: final_answer
      answer_format: single_line
```

## 21.2 双频配置

```yaml
decomposition:
  enabled: true
  min_subtasks: 1
  max_subtasks: 6
  max_dependency_depth: 5
  invalid_plan_policy: fallback_single_subtask

dual_frequency:
  fast_loop:
    enabled: true
    max_attempts_per_subtask: 2
    max_edits_per_subtask: 1
    allow_edit_types:
      - prompt
      - model
      - tool

  slow_loop:
    enabled: true
    every_n_committed_subtasks: 2
    on_phase_boundary: true
    allow_edit_types:
      - payload_contract
      - context_budget
      - schedule
      - aggregation
      - pending_graph
```

---

# 22. CLI

新增或扩展：

```bash
uv run python -m orchestra.cli.validate_backend \
  --backend smolagents_code
```

```bash
uv run python -m orchestra.cli.validate_task_plan \
  --plan configs/plans/example.yaml
```

```bash
uv run python -m orchestra.cli.run_task \
  --config configs/experiments/bbeh_codeagent_smoke.yaml
```

```bash
uv run python -m orchestra.cli.inspect_task \
  --run-dir outputs/<run_id> \
  --show-subtasks \
  --show-fast-updates \
  --show-slow-updates
```

原 LiveCodeBench CLI 必须继续工作。

---

# 23. Telemetry

在现有 telemetry 基础上新增事件：

```text
backend_registered
backend_healthcheck
backend_run_started
backend_step
backend_run_completed
backend_run_failed

task_decomposition_started
task_decomposition_completed
task_decomposition_fallback

subtask_ready
subtask_started
subtask_harness_completed
subtask_committed
subtask_failed

fast_loop_triggered
local_edit_proposed
local_edit_applied
local_edit_rejected

slow_loop_triggered
global_edit_proposed
global_edit_applied
global_edit_rejected

communication_payload_created
communication_payload_delivered
context_budget_exceeded
```

每个事件必须包含：

```text
run_id
task_id
subtask_id
node_id
graph_hash
plan_version
global_revision
timestamp
```

---

# 24. 分阶段实施顺序

必须按以下顺序执行。不要把所有重构放进一个 commit。

## Milestone 0：建立安全分支

-创建新 branch；
-记录当前 test baseline；
-运行 `uv run pytest -q`；
-运行 `uv run ruff check .`；
-保存当前 LiveCodeBench mock run；
-禁止删除旧接口。

验收：

```text
existing tests pass
existing graph validation passes
existing mock B0/B1/B2 run passes
```

## Milestone 1：Backend abstraction，无行为变化

实现：

- `backends/base.py`
- `capabilities.py`
- `registry.py`
- `structured_llm.py`
-旧 Agent executor 经 registry 调用 structured backend
-旧 graph 自动兼容

验收：

-现有所有测试不变；
-B0/B1/B2 mock trace 与重构前语义一致；
-graph hash 只因 schema version 明确变化；
-无 backend-specific branch 留在 runtime scheduler。

## Milestone 2：CodeAgent vertical slice

实现：

- optional dependency pin；
-smolagents model factory；
-tool registry；
-smolagents worker；
-smolagents backend；
-BBEH task adapter；
-单一 BBEH graph；
-3 个离线 fixture；
-3 个真实 BBEH smoke instance。

验收：

-错误 Python action 后 Agent 可继续下一 step；
-max steps 被正确识别；
-final answer 可抽取；
-worker timeout 可终止；
-token、step、latency trace 完整；
-主 runtime 不被 Agent 异常终止；
-CodeAgent 不使用 managed agents。

## Milestone 3：Task/Subtask IR

实现：

- `TaskPlan`
- `SubtaskSpec`
- validation
- deterministic fallback
- `TaskExecutionState`
- checkpoint serialization
-单 subtask compatibility mode

验收：

-非法 DAG 拒绝；
-环依赖拒绝；
-非法 plan fallback；
-resume 后不重复已 commit subtask；
-单 subtask plan 与旧任务执行结果一致。

## Milestone 4：Fast loop

实现：

- ready-subtask scheduler；
-keystone harness interface；
-FastLoopController；
-LocalEdit schema；
-`apply_atomic_edit()`；
-graph diff；
-bounded retry。

验收：

-失败 subtask 只修改自身 graph；
-其他 subtask graph hash 不变；
-一次 edit 后可重试；
-超过 budget 正确失败；
-commit artifact 不可变；
-checkpoint resume 不重复付费调用。

## Milestone 5：Slow loop

实现：

- CommunicationPlan；
-TaskProgressHarness；
-trigger；
-GlobalEdit；
-future-only update；
-plan versioning。

验收：

-已 commit artifact 不变；
-已完成 subtask 不重跑；
-只有 pending graph/communication 被修改；
-global revision 单调增加；
-旧 payload version 可追溯；
-resume 后 plan version 一致。

## Milestone 6：Pareto interface

实现：

- objective schemas；
-metrics aggregation；
-nondominated archive；
-fixed candidate evaluation；
-search cost 与 deployment cost 分开记录。

验收：

-标准 dominance tests；
-相同候选去重；
-archive size 限制；
-结果确定性；
-private labels 不进入 candidate selection。

## Milestone 7：SWE-agent process backend

实现：

- pinned external checkout；
-process adapter；
-workspace lifecycle；
-patch collection；
-trajectory normalization；
-SWE-bench task adapter。

验收：

-工作区清理；
-non-empty valid patch；
-agent 未 submit 时可从 git diff 恢复；
-timeout 能终止；
-不同任务无 patch 污染；
-runtime 不依赖 SWE-agent 内部 Python API。

---

# 25. 测试要求

新增：

```text
tests/unit/backends/
tests/unit/decomposition/
tests/unit/control/
tests/unit/communication/
tests/unit/edits/
tests/unit/objectives/
tests/integration/bbeh/
tests/integration/dual_frequency/
tests/integration/sweagent/
```

关键测试：

```text
test_legacy_graph_uses_structured_backend
test_missing_backend_fails_closed
test_backend_capability_validation
test_codeagent_action_error_recovery
test_codeagent_max_steps
test_codeagent_worker_timeout
test_codeagent_trace_normalization
test_invalid_decomposition_falls_back
test_cyclic_subtasks_rejected
test_subtask_resume_no_duplicate_execution
test_fast_edit_is_subtask_local
test_fast_loop_budget_enforced
test_slow_update_future_only
test_committed_artifacts_immutable
test_plan_revision_monotonic
test_execution_success_separate_from_accuracy
test_pareto_dominance
test_private_labels_not_in_search_state
```

CI 分成：

```text
unit
existing-livecodebench-integration
smolagents-integration
dual-frequency-integration
sweagent-integration（后续，可选）
```

smolagents integration 必须 pin dependency，并允许在无 API key 时运行 mock model fixture。

---

# 26. 非目标

本次实现不要做：

-重写 EvoMAS；
-迁移到 EvoMAS `MasRuntime`；
-让 LLM 生成完整 Python MAS；
-在 CodeAgent 内部使用 managed agents；
-一开始实现开放式任意 topology mutation；
-一开始实现 Shapley attribution；
-一开始实现 RL；
-允许 slow loop 回滚所有历史 subtask；
-根据 hidden/private result 修改 runtime；
-删除现有 LiveCodeBench runtime。

---

# 27. 最终交付物

Coding Agent 完成后必须提交：

```text
1. 新 backend abstraction
2. StructuredLLM compatibility backend
3. smolagents CodeAgent backend
4. BBEH task adapter 与 smoke config
5. Task/Subtask IR
6. DualFrequencyController
7. FastLoopController
8. SlowLoopController
9. CommunicationPlan
10. LocalEdit / GlobalEdit schema
11. Graph apply_atomic_edit / diff
12. Objective/Pareto interface
13. 单元与 integration tests
14. README architecture section
15. docs/final_runtime_architecture.md
16. CHANGELOG
17. migration report
```

Migration report 必须明确：

```text
哪些旧文件保留
哪些旧接口被 wrapper
哪些接口 deprecated
哪些功能尚未实现
当前测试数量
ruff 结果
BBEH smoke 结果
LiveCodeBench regression 结果
```

---

# 28. 完成定义

最终方案第一版只有在以下条件同时满足时才算完成：

- [ ] 现有 LiveCodeBench 路径无回归；
- [ ] runtime 不直接依赖任何具体 Agent framework；
- [ ] StructuredLLM 与 CodeAgent 均通过统一 backend protocol；
- [ ] BBEH 可通过 CodeAgent backend 端到端执行；
- [ ] 任务可表示为 Subtask DAG；
- [ ] subtask 可独立执行、验收、commit 和 resume；
- [ ] 快频更新只影响当前 local graph；
- [ ] 慢频更新只影响未来 subtask 和未来通信；
- [ ] artifact 与 revision 全部可追踪；
- [ ] execution success 与 task accuracy 分开；
- [ ] 所有更新均受 schema 和 budget 限制；
- [ ] Pareto interface 可接收固定候选结果；
- [ ] 不依赖 EvoMAS runtime；
- [ ] CodeAgent/SWE-agent 均被视为可替换 backend，而非 control plane。

---

# 29. 一句话实现准则

> 保留 AdaMAS 作为持久化、可分解、双频更新的 Orchestra control plane；复用 CodeAgent 和 SWE-agent 作为可靠的单 Agent execution harness；任何外部框架都不得接管顶层任务状态、子任务生命周期和全局优化循环。
