---
title: "Stage 1 Experiment Protocol — LiveCodeBench Baseline Validation"
status: execution-ready
project: codename_Madagascar
benchmark: LiveCodeBench Code Generation
primary_metric: Hidden Pass@1
---

# Stage 1 实验协议：LiveCodeBench 基线验证

## 0. 实验目的

Stage 1 不验证 Pareto 搜索，也不验证动态图编辑。

本阶段只回答三个基础问题：

1. **Direct Agent 本身有多强？**
2. **公开测试与一次修复能带来多少提升？**
3. **固定 Multi-Agent System 是否在 Harness 之外带来额外收益？**

对应三套系统：

| 方法 | 系统结构 | 要隔离验证的因素 |
|---|---|---|
| **B0 Direct Agent** | Problem → Coder → Freeze | 单模型直接解题能力 |
| **B1 Single Agent + Harness** | Coder → Public Harness → Same-Agent Repair | 公开测试反馈与一次修复 |
| **B2 Fixed MAS** | Parallel Analysts → Coder → Public Harness → Repair | 多 Agent 分工与并发协作 |

实验递进关系：

\[
\text{B0}
\rightarrow
\text{B1}
\rightarrow
\text{B2}
\]

其中：

\[
\text{B1}-\text{B0}
\]

衡量 Harness 与 self-repair 的价值；

\[
\text{B2}-\text{B1}
\]

衡量 Multi-Agent decomposition 的额外价值。

---

# 1. 实验范围

## 1.1 本阶段包含

- LiveCodeBench `release_v6`
- Python 3.11
- Code Generation 场景
- B0、B1、B2 三套 graph
- OfficialLCBSandbox
- 公开测试驱动的 Harness
- 最终代码冻结后的 private evaluation
- token、成本、延迟、失败类型和并发统计
- bring-up、smoke、dev、held-out 四级实验

## 1.2 本阶段不包含

- Pareto archive
- 动态 AgentEdit / ContractEdit / EdgeEdit
- Shapley 或 contribution attribution
- 自动图搜索
- outer loop
- diffusion 或其他训练
- 多模型自动路由
- hidden-test-driven repair
- 根据 held-out 逐题结果修改 prompt

---

# 2. 实验公平性要求

所有方法必须共享以下条件：

1. 相同 LiveCodeBench release；
2. 相同任务 manifest；
3. 相同主要 coding model；
4. 相同最大生成 token；
5. 相同 code parser；
6. 相同 public harness；
7. 相同 OfficialLCBSandbox；
8. 相同 private evaluator；
9. 相同 timeout 配置；
10. 相同成本计算方式。

额外 Agent 调用、Repair 调用和 Analyst 调用必须计入总成本。

不得将 B2 的额外调用通过“归一化”隐藏。

---

# 3. 数据划分

使用已经生成的三个固定 manifest。

## 3.1 Smoke Set

```text
15 tasks
5 easy
5 medium
5 hard
```

用途：

- 检查系统端到端正确性；
- 检查三套 baseline 的执行语义；
- 检查成本、延迟和并发统计；
- 不用于正式调参结论。

## 3.2 Development Set

```text
60 tasks
20 easy
20 medium
20 hard
```

用途：

- 调整 prompt；
- 调整 graph contract；
- 调整 timeout；
- 调整 public failure summary；
- 分析 B0/B1/B2 的差异；
- 为 Stage 2 设计候选 graph。

## 3.3 Held-Out Set

```text
60 tasks
20 easy
20 medium
20 hard
```

用途：

- 最终冻结配置后的评价；
- 不用于逐题调参；
- 不允许根据 hidden failure 修改 prompt 或 graph。

## 3.4 Bring-Up Set

从 smoke set 中固定选取：

```text
1 easy
1 medium
1 hard
```

保存为：

```text
configs/manifests/lcb_bringup.json
```

三套方法必须使用完全相同的三道题。

---

# 4. 固定实验配置

## 4.1 模型配置

第一轮正式对比时，所有 Agent 使用同一模型。

例如：

```text
Direct Coder        = MODEL_X
Algorithm Analyst   = MODEL_X
Edge Case Analyst   = MODEL_X
Solution Coder      = MODEL_X
Repair Agent        = MODEL_X
```

不要在第一轮同时改变：

- graph；
- role；
- model size；
- prompt；
- budget。

否则无法判断提升来自 MAS 还是模型差异。

## 4.2 建议推理参数

```yaml
temperature:
  analyst: 0.2
  coder: 0.2
  repair: 0.1

max_tokens:
  analyst: 1800
  coder: 4000
  repair: 4000
```

三种 baseline 的 coder 配置必须一致。

## 4.3 并发配置

### Bring-Up 阶段

```yaml
max_parallel_benchmark_tasks: 1
max_parallel_llm_calls: 2
max_parallel_sandboxes: 1
```

### Smoke 阶段

```yaml
max_parallel_benchmark_tasks: 2
max_parallel_llm_calls: 4
max_parallel_sandboxes: 2
```

### Dev / Held-Out 阶段

只有在 smoke 稳定后才允许进一步提高。

---

# 5. 实验顺序

## Phase A：执行器验收

在调用真实模型前，先运行 fixture 测试。

必须包含：

1. known-correct code；
2. wrong answer；
3. syntax error；
4. runtime exception；
5. infinite loop；
6. no-public-tests；
7. function-call-style task；
8. stdin/stdout-style task。

验收要求：

- correct 被正确判定；
- wrong answer 被正确判定；
- syntax error 被标记为 compile failure；
- runtime exception 不导致主进程崩溃；
- infinite loop 被 timeout 杀死；
- private tests 不进入 public worker；
- final evaluator 只能在 frozen 状态调用；
- infrastructure timeout 与 wrong answer 分开记录。

若任一项失败，停止实验，不运行真实模型。

---

## Phase B：3 题 Bring-Up

按顺序运行：

1. B0
2. B1
3. B2

不要并行运行三种方法。

### B0 检查项

- 每题恰好一次 LLM 调用；
- 不使用 public failure 进行 repair；
- 最终代码先 freeze；
- private evaluation 只在 freeze 后发生；
- token、cost 和 latency 完整记录。

### B1 检查项

- 初始 coder 与 B0 相同；
- public test pass 时不触发 repair；
- public test fail 时最多 repair 一次；
- repair prompt 不包含 private test 信息；
- selector 为确定性规则；
- hidden failure 不触发第二次 repair。

### B2 检查项

- AlgorithmAnalyst 与 EdgeCaseAnalyst 位于同一 wave；
- 两者真实并发；
- PlanMerger 等待两个输入；
- Coder 接收到完整合并 artifact；
- public harness 与 repair 分支正常；
- graph wall time、node latency sum 和 critical path 被分别记录。

### Bring-Up 停止条件

出现以下任意情况则停止：

- private test 泄漏；
- worker 无法退出；
- timeout 被错误计为 wrong answer；
- B0 发生超过一次 LLM 调用；
- B1/B2 出现超过一次 repair；
- B2 join 丢失任一 analyst artifact；
- token/cost 统计缺失；
- checkpoint 恢复导致重复调用。

Bring-up 通过后才进入 15 题 smoke。

---

## Phase C：15 题 Smoke

依次运行：

1. B0 complete smoke
2. B1 complete smoke
3. B2 complete smoke

同一任务 ID 必须在三套系统中一一对应。

### Smoke 阶段目的

Smoke 不追求统计显著性，主要检查：

- 整个 manifest 能否完成；
- 是否存在系统性 crash；
- public/private evaluation 是否一致执行；
- repair 分支是否异常频繁；
- B2 并发是否稳定；
- cost 是否合理；
- 是否存在 infrastructure error。

### Smoke 通过条件

- 三套系统均完成 15 题；
- infrastructure error rate = 0；
- private leakage = 0；
- 所有 task 有完整 artifact 与 telemetry；
- B0 每题一次模型调用；
- B1/B2 repair 次数符合配置；
- B2 analyst concurrency 被实际记录；
- summary 可以生成完整对比表。

如果 smoke 阶段稳定，即可进入 dev。

---

## Phase D：60 题 Development

在 dev set 上运行 B0/B1/B2。

该阶段允许调整：

- prompt；
- contract；
- plan merge 格式；
- failure summary；
- timeout；
- graph 中的固定节点配置。

每次调整必须：

1. 创建新的 experiment config；
2. 记录 git commit；
3. 记录 model 与 prompt hash；
4. 不覆盖旧 run；
5. 使用相同 dev manifest。

不允许只挑选有利任务汇报。

### Dev 阶段优先分析

1. B1 相比 B0 的提升；
2. B2 相比 B1 的提升；
3. 不同难度上的差异；
4. repair 的真实收益；
5. public pass 与 hidden pass 的一致性；
6. B2 的成本是否值得；
7. 哪一类任务适合 B0/B1/B2。

完成 dev 分析后，冻结所有配置。

---

## Phase E：60 题 Held-Out

冻结以下内容：

- graph；
- contracts；
- prompts；
- models；
- timeout；
- repair budget；
- selector；
- manifest；
- evaluator version。

Held-out 阶段禁止：

- 查看单题 hidden failure 后改 prompt；
- 修改 graph；
- 修改 timeout；
- 针对某题重跑不同配置并择优；
- 删除失败任务；
- 更换模型。

最终结果只允许按预先设定的规则聚合。

---

# 6. 运行命令

以下命令以现有项目结构为准。

## 6.1 验证 graph

```bash
uv run python -m orchestra.cli.validate_graph \
  --graph configs/graphs/b0_direct.yaml

uv run python -m orchestra.cli.validate_graph \
  --graph configs/graphs/b1_single_harness.yaml

uv run python -m orchestra.cli.validate_graph \
  --graph configs/graphs/b2_fixed_mas.yaml
```

## 6.2 运行 baseline

```bash
uv run python -m orchestra.cli.run \
  --config configs/experiments/stage1_b0_direct.yaml \
  --force-rerun
```

```bash
uv run python -m orchestra.cli.run \
  --config configs/experiments/stage1_b1_single_harness.yaml \
  --force-rerun
```

```bash
uv run python -m orchestra.cli.run \
  --config configs/experiments/stage1_b2_fixed_mas.yaml \
  --force-rerun
```

Bring-up 时增加：

```bash
--manifest configs/manifests/lcb_bringup.json
```

## 6.3 最终评测

```bash
uv run python -m orchestra.cli.evaluate \
  --run-dir outputs/stage1/<run_id>
```

## 6.4 汇总

```bash
uv run python -m orchestra.cli.summarize \
  --run-dir outputs/stage1/<run_id>
```

---

# 7. 必须记录的实验元数据

每个 run 必须保存：

```text
git commit
LiveCodeBench release
LiveCodeBench evaluator commit
manifest hash
graph hash
contract hash
prompt hash
model names
temperature
max tokens
timeout settings
concurrency settings
repair limit
start/end time
```

缺少其中任一项的 run 不进入正式结果。

---

# 8. 核心指标

## 8.1 准确性

### Hidden Pass@1

主要指标：

\[
\text{Pass@1}
=
\frac{\text{hidden tests passed tasks}}
{\text{total valid tasks}}
\]

Infrastructure error 不应直接等同于 wrong answer，应单独报告。

### Public Pass Rate

分别统计：

- initial public pass；
- post-repair public pass。

## 8.2 成本

- prompt tokens/task；
- completion tokens/task；
- API cost/task；
- total cost；
- cost per hidden-solved task；
- analyst cost；
- coder cost；
- repair cost。

## 8.3 稳定性

- parse failure rate；
- compile failure rate；
- runtime exception rate；
- code timeout rate；
- worker infrastructure error rate；
- repair trigger rate；
- repair success rate。

## 8.4 并发与延迟

B2 必须报告：

- graph wall-clock latency；
- sum of node latency；
- critical-path latency；
- semaphore waiting time；
- analyst parallel speedup。

可定义：

\[
\text{Parallel Speedup}
=
\frac{
L_{\text{algorithm analyst}}+
L_{\text{edge-case analyst}}
}{
L_{\text{parallel analyst wave}}
}
\]

---

# 9. 主结果表

## 9.1 总体结果

| Metric | B0 Direct | B1 Single+Harness | B2 Fixed MAS |
|---|---:|---:|---:|
| Hidden Pass@1 | | | |
| Public pass before repair | N/A | | |
| Public pass after repair | N/A | | |
| Compile success rate | | | |
| Runtime error rate | | | |
| Code timeout rate | | | |
| Infrastructure error rate | | | |
| Repair trigger rate | N/A | | |
| Repair success rate | N/A | | |
| Avg. prompt tokens | | | |
| Avg. completion tokens | | | |
| Avg. total cost | | | |
| Cost per solved task | | | |
| Avg. wall latency | | | |
| B2 parallel speedup | N/A | N/A | |

## 9.2 按难度分层

| Difficulty | B0 Pass@1 | B1 Pass@1 | B2 Pass@1 |
|---|---:|---:|---:|
| Easy | | | |
| Medium | | | |
| Hard | | | |

## 9.3 逐题配置比较

| Task ID | Difficulty | B0 | B1 | B2 | Cheapest Successful |
|---|---|---:|---:|---:|---|
| | | | | | |

这张表直接用于判断 Stage 2 的 Pareto 候选空间。

---

# 10. 分析方法

## 10.1 Harness 增量

计算：

\[
\Delta_{\text{Harness}}
=
\text{Pass@1}_{B1}
-
\text{Pass@1}_{B0}
\]

同时报告成本增量：

\[
\Delta C_{\text{Harness}}
=
C_{B1}-C_{B0}
\]

判断标准：

- 若质量提升明显且成本可接受，说明 harness/self-repair 有价值；
- 若质量不提升但成本显著增加，应检查 repair prompt 和 public test 信号；
- 若 public pass 提升但 hidden pass 不提升，说明 public tests 过弱或 repair 过拟合。

## 10.2 MAS 增量

计算：

\[
\Delta_{\text{MAS}}
=
\text{Pass@1}_{B2}
-
\text{Pass@1}_{B1}
\]

以及：

\[
\Delta C_{\text{MAS}}
=
C_{B2}-C_{B1}
\]

重点看 medium/hard：

- B2 是否主要帮助难题；
- easy 是否只有成本增加；
- analyst 输出是否对 coder 有实际帮助；
- merged plan 是否增加冗余上下文。

## 10.3 Repair 分析

将 repair 任务分成：

```text
initial fail → repaired pass
initial fail → repaired fail
initial pass → no repair
```

计算：

\[
\text{Repair Success Rate}
=
\frac{
\text{initial fail and repaired pass}
}{
\text{repair triggered}
}
\]

还需统计 repair 是否导致退化：

```text
initial result better than repaired result
```

## 10.4 Public/Hidden 一致性

计算：

\[
P(\text{Hidden Pass}\mid\text{Public Pass})
\]

和：

\[
P(\text{Hidden Fail}\mid\text{Public Pass})
\]

如果 public pass 但 hidden fail 比例较高，Stage 2 不能只使用 public pass 作为 quality 指标。

## 10.5 成本效率

计算：

\[
\text{Cost per Solved Task}
=
\frac{\text{Total API Cost}}
{\text{Number of Hidden-Passed Tasks}}
\]

同时绘制或报告：

```text
Pass@1 vs Average Cost
Pass@1 vs Average Tokens
Pass@1 vs Wall Latency
```

---

# 11. 统计要求

## 11.1 Smoke

15 题只用于系统验收，不做显著性结论。

## 11.2 Dev / Held-Out

建议报告：

- bootstrap 95% confidence interval；
- paired bootstrap for B0 vs B1；
- paired bootstrap for B1 vs B2；
- 按相同 task ID 进行配对比较。

不要把任务视为独立随机模型采样来做不配对检验。

## 11.3 Infrastructure Errors

Infrastructure error 必须单独列出。

正式 Pass@1 报告两种口径：

1. strict：infra error 按失败计；
2. diagnostic：排除 infra error 后的模型成功率。

主表使用 strict 口径。

---

# 12. 预期结果与解释

以下不是必须达到的数值，而是合理预期。

## 12.1 B0

预期：

- 成本最低；
- 延迟最低；
- easy 任务可能已经较强；
- 无 repair，因此 compile/runtime failure 较高。

## 12.2 B1

预期：

- public compile/runtime failure 显著下降；
- 一部分初始错误被修复；
- 成本和延迟高于 B0；
- 对有代表性 public tests 的任务提升更明显。

## 12.3 B2

预期：

- medium/hard 任务可能优于 B1；
- edge-case coverage 更好；
- compile/runtime stability 可能提升；
- 成本最高；
- easy 任务可能没有质量收益。

## 12.4 最理想的 Stage 1 结论

不是简单地证明 B2 全面最好，而是发现：

```text
简单任务：B0 已足够
可修复任务：B1 最划算
复杂任务：B2 更有优势
```

这种 task-dependent trade-off 是 Stage 2 Pareto 搜索的直接动机。

---

# 13. 不同结果下的后续决策

## 情况 A：B1 > B0，B2 > B1

进入 Stage 2。

候选包含：

- B0；
- B1；
- B2；
- B2 的轻量变体。

## 情况 B：B1 > B0，但 B2 ≈ B1 且更贵

仍可进入 Stage 2。

研究目标变成：

> 根据任务动态选择 B0/B1/B2，而不是所有任务都使用 MAS。

## 情况 C：B2 在质量与成本上全面优于 B0/B1

需要增加廉价 B2 变体，形成实际 trade-off：

- 单 analyst；
- small analyst；
- compact payload；
- no repair。

## 情况 D：B2 明显差于 B1

暂停 Pareto。

优先检查：

- analyst prompt；
- plan merge；
- context 冗余；
-错误 plan 污染；
- coder 是否过度依赖 analyst；
- MAS 是否只增加噪声。

## 情况 E：B1 与 B0 接近

检查：

- public tests 是否过少；
- repair prompt 是否有效；
- selector 是否正确；
- repair 是否产生退化；
- model 是否已经足够强。

---

# 14. Stage 1 完成标准

Stage 1 只有在以下条件满足后才完成：

- [ ] B0/B1/B2 在 bring-up set 上语义正确；
- [ ] 三套系统完成 15 题 smoke；
- [ ] infrastructure error rate 为 0 或已明确解释；
- [ ] private test 泄漏为 0；
- [ ] 60 题 dev 完成；
- [ ] prompt/graph/model 配置冻结；
- [ ] 60 题 held-out 完成；
- [ ] 主结果表和难度分层表完成；
- [ ] Harness 增量与 MAS 增量分析完成；
- [ ] public/hidden 一致性分析完成；
- [ ] cost per solved task 完成；
- [ ] Stage 2 候选 graph 清单确定。

---

# 15. 实验输出目录建议

```text
outputs/stage1_experiments/
├── bringup/
│   ├── b0/
│   ├── b1/
│   └── b2/
├── smoke/
│   ├── b0/
│   ├── b1/
│   └── b2/
├── dev/
│   ├── b0/
│   ├── b1/
│   └── b2/
├── heldout/
│   ├── b0/
│   ├── b1/
│   └── b2/
└── reports/
    ├── stage1_main_results.csv
    ├── stage1_by_difficulty.csv
    ├── stage1_task_comparison.csv
    ├── stage1_cost_analysis.csv
    ├── stage1_public_hidden_analysis.csv
    └── stage1_summary.md
```

每次实验不得覆盖旧 run。

---

# 16. 最终报告必须回答的问题

Stage 1 报告最后必须明确回答：

1. Direct Agent 的 Hidden Pass@1 和成本是多少？
2. Harness/self-repair 带来多少增益？
3. Fixed MAS 是否提供额外增益？
4. 增益主要出现在什么难度？
5. Repair 是否存在 public overfitting？
6. B2 的并发是否抵消了部分额外延迟？
7. 哪个系统的 cost per solved task 最低？
8. 是否存在明显的 task-dependent 最优配置？
9. 是否有足够证据进入 Pareto 搜索阶段？
10. Stage 2 应保留哪些 graph/config candidates？
