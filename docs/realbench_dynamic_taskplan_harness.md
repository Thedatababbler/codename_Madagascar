# RealBench：动态 TaskPlan + 公开 Harness + Scheduler

## 目标

把原先固定的 `analyze → implement → verify`（且 `keystone_harness_id: none`）
升级为：

1. **动态 TaskPlan**：按 `public_design/`（tree / package UML）生成 milestone DAG  
2. **每阶段可运行公开 harness**：图内 `repository_test_harness` gate freeze  
3. **ReadySubtaskScheduler**：按依赖调度并做 canonical commit 校验  

Hidden RealBench `proj_with_test` **不进入** agent / plan / 在线 harness；仍用离线脚本评测。

## 边界

| 层 | 内容 | 可见性 |
|----|------|--------|
| 公开 harness | `scripts/adamas_public_check.py`（compileall / import / UML export） | agent 可见 |
| Milestone 目标 | 调度 fork 后写入 `MILESTONE.md` | agent 可见 |
| Hidden eval | `scripts/eval_realbench_codex_decomp_baseline.py` | 仅离线 |

## 组件

- Plan builder：`src/orchestra/decomposition/realbench_plan.py`  
  - `build_realbench_candidate_plan(...)`  
  - 产出任务相关 subtask id（如 `map_public_api`、`implement_nodeflow`、`public_contract_harden`）  
- Public harness：`src/orchestra/realbench/public_harness.py`  
  - `materialize_public_harness(workspace)`  
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
- Scheduler：按 `keystone_harness_id` 选择 commit harness；fork 后写 `MILESTONE.md`

## 公开 harness 等级

| level | 检查 |
|-------|------|
| discovery | `compileall` |
| implementation | compileall + import 期望模块 |
| integration | 上述 + UML `package.json` exports 符号存在 |

这些检查只由 `public_design/tree.txt` 与 `package.json` 生成，**不是** hidden tests 的代理全集。

## 运行

```bash
# dry-run：只产出 plan / subgraphs / public harness
uv run python -m orchestra.cli.run_realbench_codex_decomp_baseline \
  --config configs/experiments/realbench_codex_decomp_baseline.yaml \
  --agent-backend codex_sdk \
  --task-id encore-ecosystem_NodeFlow \
  --dry-run

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

| | 旧固定 A/I/V | 本实现 |
|--|--|--|
| Plan | 手写三阶段 | 按 public tree 动态 milestone |
| keystone | `none` | `repository_test_harness` |
| 图内验收 | 无 harness 节点 | harness gate freeze |
| 公开信号 | 恒真 smoke | graded public check |
| Hidden | 离线 | 仍离线 |

## 未做（有意）

- 开放式 LLM 任意 TaskPlan / 任意 shell harness（仍走白名单 + catalog）  
- Codex 跨阶段 resume thread（仍 `thread_policy: fresh`）  
- fast/slow loop 适应  
- 把 hidden tests 接入在线 verifier  
- 付费 API / held-out 线上实验（需单独启动）  
