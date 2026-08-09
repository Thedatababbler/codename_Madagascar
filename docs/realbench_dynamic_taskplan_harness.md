# RealBench：动态 TaskPlan + 公开 Harness + Scheduler

## 目标

把原先固定的 `analyze → implement → verify`（且 `keystone_harness_id: none`）
升级为：

1. **动态 TaskPlan**：按 `public_design/`（tree / package UML）生成 milestone DAG  
2. **每阶段可运行公开 harness**：图内 `repository_test_harness` gate freeze  
3. **ReadySubtaskScheduler**：按依赖调度并做 canonical commit 校验  

Hidden RealBench `proj_with_test` **不进入** agent / plan / 在线 harness；仍用离线脚本评测。

## 工作区隔离不变量

**AdaMAS 造出来的任何东西都不落在 agent 工作区**，只经由 prompt 抵达 agent。
工作区里只有数据集内容：`TASK.md`、`REQUIREMENTS.md`、`README.md`、
`public_design/`、由 `tree.txt` 生成的空脚手架。

理由：工作区文件会被 hidden eval 的 overlay 一起带进私有测试环境，也会诱导
agent 围着我们的脚手架写代码（例如把包塞进 `proj_clean/` 再 `from proj_clean.X import *`，
线上绿、离线全崩）。

| 内容 | 位置 | 抵达 agent 的方式 |
|------|------|------------------|
| 公开检查脚本 + manifest | `<run_dir>/harness/adamas_public_check.py`、`adamas_public_harness.json` | 不可见（agent 不能读也不能改） |
| Milestone 合约 checks | `<run_dir>/harness/<milestone_id>.contracts.json` | 以自然语言写进 prompt |
| Milestone 目标 / criteria / corner cases | plan metadata + 生成的 AgentContract | system prompt + prompt prelude |
| 跨 milestone 记忆 | `<run_dir>/adamas_memory/ADAMAS_CHANGELOG.md` | prompt prelude |
| Hidden eval | `scripts/eval_realbench_codex_decomp_baseline.py` | 仅离线 |

harness 以 `cwd = 待测仓库`、脚本/manifest/合约全用绝对路径的方式运行，因此
fork 出的 subtask workspace 与 commit staging 副本都能复用同一份 runner 资产。
`AgentNodeSpec.prompt_prelude`（未设置时不参与 graph content hash）承载运行期
才知道的上下文：milestone brief + 之前 milestone 的提交记录。

## 分解来源（两条路径）

| 来源 | 触发 | 形态 |
|------|------|------|
| **风险优先动态规划**（默认，唯一可用于实验的模式） | 默认开启；`ADAMAS_REALBENCH_DYNAMIC_PLAN=0` 才关闭 | LLM 按任务性质产出 milestone DAG，每段自带验收 harness 与 agent 名单 |
| **public_design 模板**（fail-closed 回退） | LLM 不可用 / 输出不合法 | 简单仓单段；复杂树 discovery / implementation* / integration |

两条路径产出同一 `TaskPlan` 结构，都经 `validate_task_plan` + keystone harness 校验。

> **模板分段不是一种实验模式。** 它按公开树的形状（模块数量）切分，不构成任何值得
> 度量的分解假设。触发回退的 run 会在 `plan.yaml` 旁写下 `PLANNER_FALLBACK` 并打
> error 日志，该任务的结果**作废**，不得用于分解相关的对比。汇报一批结果前先确认
> 每个任务都有 `milestone_plan_draft.json`。

### 风险优先规划（`src/orchestra/realbench/milestone_planner.py`）

Milestone 的唯一存在理由是**风险闸门**：某个决策错了会让后续工作全部作废
（跨模块共享的 key/ID、所有模块都实现的基类或协议、序列化/返回结构约定）。

- 默认答案是**单个 milestone**；无风险不切分
- 明确禁止：按目录/包/文件数切、只读代码或只写文档的 milestone、超过 4 段
- 非终段 milestone 必须给出 `risk_rationale`，否则规划结果会被自动收敛为单段
- **终段 milestone 一律按 `integration` 收口**：否则 freeze gate 不会要求 UML 声明的
  符号能从文档化模块导入，仓库可以带着缺失的包级导出被提交
- 只读公开输入（TASK.md / REQUIREMENTS.md / `public_design/`），不接触 hidden tests
- 依赖只能指向**已声明**的 milestone → DAG 无环由构造保证
- 产物落盘：`<run_dir>/milestone_plan_draft.json`

### 每段验收 harness

规划器为每个 milestone 给出：

- `criteria`：可观察的行为判据
- `corner_cases`：不得回归的边界情形
- `checks`：机器可判定项，仅限 `import` / `export` / `callable_or_class` / `module_file_exists`

这些 checks 与 `public_design` 确定性基线**按目标合并**（同一符号取 level 并集），在
**计划期**冻结到 `<run_dir>/harness/<milestone_id>.contracts.json`；criteria / corner cases
进入 milestone brief 与 agent contract 的 prompt。非白名单类型、路径逃逸（`..`、绝对路径）
一律丢弃。规划器被要求把符号钉在 public design 声明的导出模块上（例如包根），
而不是定义处——只在私有模块可导入、在文档化模块缺失的符号照样会废掉所有下游。

确定性基线还区分**包与单文件模块**：`tree.txt` 里的 `SnoopR.py` 要求
`SnoopR.py` 存在，而不是 `SnoopR/__init__.py`（后者会逼 agent 造出一个
hidden eval 里根本不存在的目录布局）。

### 角色池与模板子图（`src/orchestra/roles/`）

planner 不再自由发明 agent 角色，也不再自己设计拓扑，而是做两次选择：

- **角色池**（`configs/roles/*.yaml`，10 个）：每个角色自带一段 prompt、预算默认值
  和 `edits_repository` 标记。可写角色 8 个（`implementer`、`contract_author`、
  `test_driven_implementer`、`integrator`、`gate_repairer`、`edge_case_hardener`、
  `dependency_resolver`、`scope_pruner`），只读角色 2 个（`spec_auditor`、
  `contract_critic`）。选到池外的名字会退回该槽位的默认角色，不会让整个计划失败。
- **模板目录**（`configs/subgraph_templates/*.yaml`，5 个）：`solo`、`chain`、
  `gate_then_repair`、`review_then_fix`、`parallel_audit`。模板声明槽位、边和验收门
  的位置，builder 把它编译成运行时图。

模板受运行时能力约束，不收录跑不了的形状：

- 同一 milestone 的 agent 共享一个工作区，所以**两个可写 agent 不允许同层并行**
  （加载时校验，违反直接报错）；只有只读角色可以扇出，这也是 `parallel_audit` 安全的原因。
- 编译器拒绝环，因此没有 loop 型模板；"先试再修"要花掉第二个槽位。
- 多条条件边可以指向同一输入槽，先激活且有产物者胜出——`gate_then_repair` 的提前退出
  就建立在这个语义上。

### 每段 runtime subgraph（`src/orchestra/realbench/subgraph_builder.py`）

每个 milestone 按所选模板编译成独立子图，节点即 agent。默认链式：

```text
agent_1 → agent_2 → … → repository_tests(acceptance) → freeze_change
```

`gate_then_repair` 则在首个 agent 之后就跑验收，**通过即冻结，修复 agent 完全不被唤醒**：

```text
agent_1 → probe(acceptance) ─[passed]──────────────→ freeze_change
                            └[failed]→ agent_2(gate_repairer) → acceptance → freeze_change
```

- agent 串行共享同一 workspace；只有末位 agent 的变更进入 harness 与 freeze gate
- 扇入节点的每个上游各占一个输入槽（`upstream_change`、`upstream_change_2`），
  共用一个槽会导致只收到先到的那一份
- 只读角色的节点关闭 `require_git_diff`：它本就不产生 diff，否则正确行为会被判失败
- 每个 agent 生成一份 `AgentContract`（`<run_dir>/generated/contracts/rbdyn_*.yaml`），记录
  **role、system/user prompt、max_tokens、max_steps、timeout**
- 图 metadata 里的 `agent_roster` 另存 `prompt_sha256` / `prompt_chars` 作为证据
- 后端映射：`codex_sdk`（自管回合，`max_steps=1`）/ `smolagents_code`（`max_steps` 生效 + 仓库工具白名单）
- 运行期契约目录切到 `<run_dir>/generated/contracts`（已复制基础合约，基线图仍可用）

### Milestone contracts

合约 JSON 由 `public_design` 确定性生成，叠加规划器验收 checks；可选
`ADAMAS_REALBENCH_LLM_CONTRACTS=1` 用 LLM 再增补（仅白名单 check 类型，失败回退确定性）。
模板 fallback 路径同样在计划期冻结合约，并把静态 role 图复制到
`<run_dir>/generated/graphs/<milestone_id>.yaml`，只改写 harness command 为绝对路径。

## 组件

- Milestone planner：`src/orchestra/realbench/milestone_planner.py`  
  - `plan_milestones(...)` / `parse_plan_payload(...)`（纯函数校验，可单测）  
- Subgraph builder：`src/orchestra/realbench/subgraph_builder.py`  
  - `prepare_generated_root(...)` / `materialize_milestone_subgraph(...)`  
- Plan builder：`src/orchestra/decomposition/realbench_plan.py`  
  - `build_plan_from_draft(...)`（动态）/ `build_realbench_candidate_plan(...)`（模板 fallback）  
- Public harness：`src/orchestra/realbench/public_harness.py`  
  - `materialize_public_harness(workspace, harness_dir=...)` / `public_check_command(...)`  
- Milestone 记忆：`src/orchestra/realbench/workspace_memory.py`（run 目录内，prompt 投递）  
- Graphs（均含 harness→freeze gate）：  
  - Codex：`configs/graphs/codex_realbench_public_{discovery,implementation,integration}.yaml`  
  - Smolagents：`configs/graphs/smolagents_realbench_public_{discovery,implementation,integration}.yaml`  
- Contracts：`codex_realbench_milestone.yaml` / `smolagents_realbench_milestone.yaml`  
- Runner：`src/orchestra/cli/run_realbench_codex_decomp_baseline.py`  
  - `--agent-backend codex_sdk|smolagents_code`（与 `experiment.agent_backend` 一致）  
- Experiments：  
  - `configs/experiments/realbench_codex_decomp_baseline.yaml`  
  - `configs/experiments/realbench_smolagents_decomp_baseline.yaml`  
  - `decomposition.require_public_keystone_harness: true`  
- Validator：`validate_subtask_graph_harness`（计划 keystone 必须出现在图中并 gate freeze）  
- Scheduler：按 `keystone_harness_id` 选择 commit harness；fork 后把 milestone brief +
  记忆注入 `prompt_prelude`（不写工作区）

## 公开 harness 等级

| level | 检查 |
|-------|------|
| discovery | `compileall` |
| implementation | compileall + import 期望模块 |
| integration | 上述 + UML `package.json` exports 符号存在 |

这些检查只由 `public_design/tree.txt` 与 `package.json` 生成，**不是** hidden tests 的代理全集。

UML 的 package 名是**裸 basename**：树里若有两个同名 `abstract.py`，导出符号只需在其中
一个模块可见即可（`export_any` 检查），不会要求每个同名模块都提供。

仓库不拥有的三方依赖（`pyproj`、`torch` 之类）在 harness 环境缺失时按 **skipped** 记录，
不判失败——hidden eval 环境会装这些依赖，而 agent 无权安装它们。仓库自身模块缺失或导入
报错仍然是硬失败。

## 运行

```bash
# dry-run：只产出 plan / subgraphs / public harness
uv run python -m orchestra.cli.run_realbench_codex_decomp_baseline \
  --config configs/experiments/realbench_codex_decomp_baseline.yaml \
  --agent-backend codex_sdk \
  --task-id encore-ecosystem_NodeFlow \
  --dry-run

# 开启风险优先动态分解（会额外调用一次规划 LLM；失败自动回退模板）
ADAMAS_REALBENCH_DYNAMIC_PLAN=1 \
ADAMAS_REALBENCH_PLANNER_MODEL=gpt-5.4 \
bash scripts/run_realbench_codex_decomp_baseline.sh

# Codex 全量（需 Codex 凭证）
bash scripts/run_realbench_codex_decomp_baseline.sh

# Smolagents 全量（需 OpenAI-compatible 凭证；勿与 Codex 混跑）
bash scripts/run_realbench_smolagents_decomp_baseline.sh

# 冻结后离线 hidden 评测
uv run python scripts/eval_realbench_codex_decomp_baseline.py \
  --batch-dir outputs/realbench_codex_decomp_baseline/<RUN_ID>
```

## Codex vs smolagents 公平性

两边共用同一 RealBench 公开输入、动态 milestone TaskPlan 语义、公开 harness、
隔离 workspace、scheduler commit、评测逻辑与预算定义。差异仅在后端交互协议：

| | Codex | Smolagents |
|--|--|--|
| 编辑方式 | Codex SDK 线程内改仓 | 白名单 repository tools |
| step 限制 | Codex 回合语义 | `max_steps` CodeAgent |
| token | Codex usage 字段 | smolagents TokenUsage；缺字段标 `unavailable` |
| 终止 | SDK turn 结束 | `final_answer` / max_steps |
| 变更证据 | Git snapshot（忽略模型文本） | 同左 |

两边都**不**向 agent 暴露 hidden tests / 参考实现。注入的恒真 liveness 测试不计为解题证据。

## 与旧 baseline 的差异

| | 旧固定 A/I/V | public_design 模板 | 风险优先动态 |
|--|--|--|--|
| Plan | 手写三阶段 | 按 public tree 填模板 | 按任务风险生成 milestone DAG |
| 切分依据 | 无 | 模块/包规模 | blast radius（错了会废掉后续） |
| 每段验收 | 无 | role 级公开检查 | 公开检查 + 该段 criteria/corner cases/checks |
| 子图 | 固定三张图 | 固定三张图 | 每段生成，节点即 agent，含 role/prompt/token 预算 |
| keystone | `none` | `repository_test_harness` | 同左 |
| Hidden | 离线 | 离线 | 离线 |

## 未做（有意）

- 任意 shell harness / 任意 check 类型（仍限白名单 + catalog）  
- 规划器改写已 commit 的 milestone（无 replan 回路；一次成图）  
- 同一 milestone 内 agent 并行（当前串行共享 workspace）  
- Codex 跨阶段 resume thread（仍 `thread_policy: fresh`）  
- fast/slow loop 适应  
- 把 hidden tests 接入在线 verifier  
- 付费 API / held-out 线上实验（需单独启动）  
