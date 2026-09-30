# 阶段 0：账本审计（只读）

分支 `rsi`，基于 `milestones` a70a8d56，2026-09-30。范围：`outputs/` 下所有带 `task_execution.json` 的运行（373 个文件，175 个里程碑进入过快速搜索），加上 `outputs/cpe_milestones/*/*/node_resample_records.jsonl`。

## 1. 旧剧本行的实际运行次数

候选种类（`fast_loop_states[*].candidates` 逐条计数）：

| 种类 | 次数 | 说明 |
|---|---|---|
| 锚 / 探针（`cand_feedback*`，无 playbook_id） | 271 | 早期是"锚"，持续搜索启用后同一记录就是探针 |
| 现任（`incumbent_first_pass`） | 125 | 质量搜索的对照 |
| 剧本行（带 playbook_id） | 143 | 明细见下表 |
| 旧原子编辑候选（`cand_add_gate_repairer` / `cand_add_test_driven_impl` / `cand_budget`） | 25 | 剧本表之前的编辑层，已不再生成 |
| 基础设施重试（`infra_retry_0`） | 15 | |

剧本行明细（143 次）：

| 行 | 次数 | 类型 |
|---|---|---|
| pb_q_continue_improve（续修 / 定点补修，即 R0 的前身） | 50 | 不改结构 |
| pb_q_failures_to_agent | 16 | 不改结构 |
| pb_rtf_swap_angle | 13 | 换角色 |
| pb_rtf_second_angle | 13 | 改结构 |
| pb_q_node_resample | 11 | 重采样 |
| pb_chain_fill_third | 8 | 换角色 |
| pb_chain_to_review_fix | 8 | 改结构 |
| pb_tf_q_improve_after_gate | 7 | 改结构 |
| pb_tf_q_failures_to_builder（已删除） | 5 | 不改结构 |
| pb_failures_to_agent | 4 | 不改结构 |
| pb_q_reauthor | 3 | 重出题 |
| pb_tf_diagnose_before_repair | 2 | 改结构 |
| pb_tf_failures_to_repairer | 1 | 不改结构 |
| pb_tf_q_diagnose_then_improve | 1 | 改结构 |
| pb_tf_second_repairer | 1 | 改结构 |
| 其余 11 行（pb_solo_*、pb_gtr_*、pb_tf_builder_budget、pb_chain_budget、pb_rtf_drop_reviewer、pb_budget_steps_time_*、pb_tf_q_diagnose_from_improve） | 0 | 从未运行 |

节点重采样记录文件 4 个，共 4 条。

结论：R0 的前身（续修）是唯一样本量够看的行；改结构的行合计 45 次、分散在 8 行，与 EXP-20260831-01 的结论一致。

## 2. 逐用例记录与工作区

| 检查项 | 现状 | 缺口 |
|---|---|---|
| 里程碑提交时冻结套件的逐用例结果 | `summary.json` 与 `task_execution.json` 的每个阶段只记 `passed_units / total_units / failed_tests`；**通过用例的名字没有记录** | 统一验收需要"稳定通过集合"，必须知道通过用例的名字。验收门脚本已用 `-v` 运行并解析 `PASSED` 行（只在超时时用），补记 `passed_tests` 即可 |
| 候选工作区 | 完整保留：`tasks/rb_<task>/subtasks/<milestone>/candidates/<cand>/repo`；被放弃的只写一个 `DISCARDED` 标记，不删除 | 无 |
| 冻结套件能否在后续里程碑重跑 | 能：`harness/<milestone>.spec_tests` 与 `<milestone>.contracts.json` 长期保留；验收门脚本接受 `--spec-tests` 与 `--contracts` 任意路径 | 需要一个"在指定工作区上跑指定里程碑套件"的封装（阶段 1 的前序回归诊断） |
| 探针 / 续修的记录 | `metadata.persistence_phase` 1 = 探针，2 = 第二阶段；`persistence_ledger` 记修好的持续失败 | 无 |

## 3. 第 12 节待确认事项的答复

1. **summary 里有没有提交时的逐用例结果？** 有失败用例名（`failed_tests`）和通过数，没有通过用例名。阶段 0 补：验收门脚本记 `passed_tests`，`HarnessStageResult` / `CandidateRecord` 带 `passed_tests` / `behaviour_passed`，提交时在 `harness/<milestone>.committed_cases.json` 落一份。
2. **custody 冻结的套件能否在后续里程碑取出重跑？** 能，见上表。
3. **候选工作区保留到什么程度？** 完整保留（repo 目录），提交后不删；放弃的只加标记。
4. **修复者"只在门失败时运行"的逻辑在哪？** 模板 YAML 的槽位字段 `runs_if_gate_failed: true`（test_first、gate_then_repair、test_first_diagnosed、test_first_double_repair、test_first_improve、test_first_quality_diagnosed）+ `early_gate_after`；`realbench/subgraph_builder.py` 第 469 行判定提前过门、第 527 行给修复者接一条条件边 `probe.result.passed is_false`；运行时 `runtime/native_async.py` 按条件边决定节点是否运行；`plan_candidates.py` 第 326 行按该标记找修复槽位。改法：条件边改成"门报告里有失败用例"，需要在门结果工件上加一个字段。
5. **规划器有没有输出备选模板？** 没有（计划 JSON 只有一个 `template_id`）。按第 12 节第 5 条兜底：E9-T1 换到 `gate_then_repair` / `review_then_fix` 中本里程碑未用过的一个。

## 4. 阶段 1 之前要先补的记录能力

- 门脚本：`spec_tests` 阶段记录 `passed_tests`（`-v` 输出已有）。
- 门结果工件：新增 `behaviour_failed_count`（供修复者槽位的条件边使用）。
- 候选记录：`behaviour_passed`。
- 提交：`harness/<milestone>.committed_cases.json`（`passed` / `failed` 两个名单 + 套件版本），供后续里程碑做回归诊断。
