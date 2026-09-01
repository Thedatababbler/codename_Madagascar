# Playbook Search 阶段进度（2026-08-31）

> 这份文档记录 **fast-loop playbook search 做到哪了、核心代码怎么走、下一步该付哪一笔钱**。设计原稿仍是 `docs/fast_loop_playbook_search.md`；Pareto 选择协议仍是 `docs/fast_loop_pareto_protocol.md`。本文不替代那两份，只写现状。

仓库：`/root/projects/AdaMAS`，工作分支 **`topology`**。选择器（Pareto / epsilon / incumbent / `require_gate_pass`）**不在本阶段范围内**，没改。旧 atomic-edit 生成器（`DesignSearchCandidateGenerator` / `RuleBasedLocalCandidateGenerator`）**没删、没改入口语义**。

---

## 0. 状态更新（2026-09-01）— 本节覆盖 §1、§10、§11 中已过时的表述

**P0 已付费复跑并完成**（EXP-20260831-01，五次 run，均走 cli-proxy / ChatGPT 订阅通道，不是官方 API key）。四个验收点全过。过程中发现并修掉三个缺陷，都带回归测试：

1. 计划层重编译后生成器自己的校验编译解析不到新合同（`_annotate_recompile` 里的 `apply_local_edits` 比控制器的注册早）——单测全绿是因为所有 generator 测试都不接 compiler。
2. 候选 checkpoint 的 `task_id` 没有里程碑成分，第二个搜索的里程碑全部候选 46–151ms 秒死（`CheckpointDriftError`）。
3. `milestone_objectives` 在全部候选里取最高分配给选中者的 id，把被合同判零的设计的分数当成提交结果上报。

**结果层面**：菜谱一次都没赢过锚点。全锚点对照臂（`anchor_search`）证明 k=3 下纯重抽的提升 ≥ 菜谱搜索；同设计噪声 σ ≈ 一道题（0.04），而 `epsilon.quality=0.02` 只有半道题。持久性交集把失败分成「每次都挂」和「翻转」：翻转题是重抽的全部收益来源，持久题重抽永远碰不到。

**已改**：`pb_tf_q_failures_to_builder` 删除（三战三负的受控测量）；两个 improve 形状加 improver 之后的早门 + 可选 repairer；所有规划器可选模板带可选 `test_author` 槽，`solo` 撤出目录；新增 `anchor_search`、`persistence_search` 两个模式；LLM 诊断输出 `recommended_role/recommended_reviewer`（封闭池、规则打底、自洽护栏、默认兜底、账本），并首次在质量路径运行。

**persistence 臂首跑**：M1 交集为空 → 阶段二正确放弃；M2 持久 4/翻转 2，LLM 派 `implementer`（0.89），阶段二候选修掉 **2/4 持久题**——三次重抽从未碰到的题——但重新抽样的 builder 引入 3 道新失败，净 19/24 输给锚点 20/24。改动归因到 builder 的 patch，improver 未触碰。

**下一步（取代 §11）**：续作候选——improver 直接在 incumbent 成品工作区上干、不重跑 builder。数据指向它：诊断能动持久题，收益被 builder 方差随机抵消。其后是把 epsilon 调到噪声量级、按 (class, role) 汇总账本。

---

## 1. 一句话结论

机制已经接通：`playbook_search: true` 时，失败搜索和质量搜索都走 `PlaybookCandidateGenerator`，但读**两张互不共享 `playbook_id` 的表**。质量表按「便宜先上」排好，换模板后会把错题名绑到新图上真正会跑的槽，合同注册也会同步更新 executor。

~~还没在当前代码上付过官方探测。~~ 已于 2026-08-31 起付费复跑五次，见 §0 与 EXP-20260831-01。下文 §4 的两次探测仍只作教训。

---

## 2. 目标与边界

### 要解决的问题

原来的 Pareto 搜索（`design_search: true`）用固定 atomic-edit 草案顺序。预算 `k=2` 时永远只生成两件事：feedback-only，再加一个角色。诊断几乎不影响草案。`behaviour_failures` 算出来了，但从没进过 prompt。

Playbook search 的替换点只有前两步：

1. **诊断**：失败里程碑可选用 LLM 分成 `budget` / `functional` / `design`；质量搜索有 incumbent，不调 LLM。
2. **草案**：按「为何搜索 + 当前模板 +（失败时）失败类」从写死的表里抽有序菜谱。

第三步没动：编辑层仍走 `apply_local_edits`（DAG、终端可达、`max_steps≤64`、墙钟上限）；形状变化走计划层 `recompile_candidate` + `TemplateSwitch`，不走 `apply_local_edits` 拼拓扑。

### 明确不做

| 不做 | 原因 |
|------|------|
| 改 Pareto / epsilon / incumbent 选择 | 另一份协议，本阶段不掺 |
| 新的 `FailureClass.quality` | 质量搜索没有失败类可分 |
| 覆盖旧 atomic-edit 生成器 | 对照臂还要用 |
| `playbook_search` + `design_search` 同时开 | CLI / 控制器两边都报错 |
| 把搜索专用模板交给规划器 | `planner_selectable: false`，避免分解和搜索同时变 |
| improver 走 `runs_if_gate_passed` | 会和现有 early-gate / `probe_pass_to_freeze` 抢路 |
| 推 `main` | 工作在 `topology` |

---

## 3. 完成度总表

| 块 | 状态 | 落点 |
|----|------|------|
| 失败菜谱表 `CATALOG` | 已落地，探测后未再改 | `src/orchestra/control/fast_loop/playbooks.py` |
| 质量菜谱表 `QUALITY_CATALOG` | 已按探测教训重写 | 同上 |
| 生成器 | 已落地；计划层重编译后会贴错题名 | `playbook_generator.py` |
| 计划层重编译 | 已落地 | `plan_candidates.py` |
| 合同注册同步 executor | **代码已修，未付费复跑** | `register_new_contracts` + `controller.py` |
| 搜索专用模板 | 4 个 yaml，规划器不可见 | `configs/subgraph_templates/` |
| 只读角色 `behaviour_critic` | 已落地 | `configs/roles/behaviour_critic.yaml` |
| LLM 诊断 | 已落地；**质量搜索不调用** | `llm_diagnosis.py` |
| CLI / `TuningConfig` | 已接线 | `run_codeprojecteval_decomp.py` 等 |
| 实验 yaml | 已写 | 见第 9 节 |
| 单测 | 控制面相关已绿 | `tests/unit/control/test_playbooks.py` 等 |
| 官方探测 | 两次，均早于当前质量表 | 见第 4 节 |
| 当前代码的官方复跑 | **未做** | 下一步第一项 |

---

## 4. 两次官方探测（代码已变，结果只作教训）

都在 `outputs/cpe_official_playbook/`，任务都是 imapclient，冻结计划 `outputs/cpe_tuning/plans/imapclient.multi.test_first.json`（两里程碑，`test_first`）。质量触发看的是**行为分** `< min_score`（0.9），不是混合 harness（结构阶段常把 harness 钉在 0.92–0.96）。

### 4.1 k=3，gpt-5.4（2026-08-27）

路径：`outputs/cpe_official_playbook/probe-imapclient-playbook-official-k3-20260827T095305Z/`

- 墙钟约 28 分钟（`latency_ms` 1 677 401），花费约 **$7.32**（M1 $1.91 + M2 $5.41）。
- M1 `freeze_shared_api_contracts`：行为 0.95，门过，高于 0.9，**未搜**。
- M2 `implement_client_behaviour_and_public_surface`：首通行为约 0.76，质量搜索开火。
- 当时还没有独立质量表。候选：`incumbent_first_pass`、`cand_feedback`、`cand_pb_tf_failures_to_repairer`、`cand_pb_tf_diagnose_before_repair`。
- **选中 `cand_feedback`**（行为 0.81，harness 0.943）。
- `pb_tf_failures_to_repairer`：分数几乎等于首通。`repairer` 有 `runs_if_gate_failed: true`，门已过，修理工没上场。
- `pb_tf_diagnose_before_repair`：约 0.16s 死。表面 `GraphDeadlockError`，根因是  
  `KeyError: '..._cand_pb_tf_diagnose_before_repair_test_author_test_author'`。  
  新合同写在 `generated/contracts/`，`register_new_contracts` 只更新了 compiler，`AgentNodeExecutor.contracts` 仍是 CLI 启动时的旧表。
- LLM 诊断没跑：质量搜索走 `quality_search_diagnosis`，`failure_class` 空，lookup 因 `spec_tests` 标成 functional，于是抽出**失败**菜谱。

### 4.2 k=2，gpt-5-mini（2026-08-28）

路径：`outputs/cpe_official_playbook/probe-imapclient-playbook-official-k2-mini-20260828T001012Z/`

- 墙钟约 22 分钟。mini 无官方价目，花费显示 $0。
- M1：一次 infra 死锁，`infra_retry_0` 过门。
- M2：已抽**当时的**质量表。候选：incumbent、`cand_feedback`、`cand_pb_tf_q_failures_to_builder`。失败菜谱未出现。
- **选中 incumbent**（行为 0.14 ≈ 6/42，harness 0.74）。
- `cand_feedback`：门没过（缺一批 contract 符号，还动了 `spec_tests`），不能替换 incumbent。
- 质量菜谱：门过，行为仍约 0.14。只把 **3 个错题名**塞给 builder；42 题约 36 道错，harness 的 `failed_tests` 只点了 3 个。

> 结论：机制通了，分数没动。原因是 5-mini 天花板 + `k=2` 只用最弱一招 + 点名残缺 + 当时换模板后错题名还没打到 improver。**不能用这两次结果评价现在的质量表。**

忽略：`...T073654Z`（xiaoai 误跑）、`...T094928Z`（官方 key + xiaoai URL，401）。

---

## 5. 核心代码：一次搜索怎么走

### 5.1 模块地图

```
scheduler / CLI
    │  incumbent?  ─── 有 → 质量搜索
    │                  无 → 失败搜索
    ▼
FastLoopController.run
    ├─ 诊断
    │     质量 → quality_search_diagnosis()     不调 LLM
    │     失败 → diagnose_subtask_failure()
    │             + 可选 refine_diagnosis()     diagnosis.mode == llm
    ├─ generator.generate(search_reason=...)
    │     playbook_search → PlaybookCandidateGenerator
    │     design_search   → DesignSearchCandidateGenerator   丢掉 search_reason
    │     都关            → RuleBasedLocalCandidateGenerator 丢掉 search_reason
    ├─ 每个候选 register_new_contracts(compiler + executor) → compile → execute
    └─ ParetoCandidateSelector（playbook / design 都用；本阶段未改）
```

关键文件：

| 文件 | 职责 |
|------|------|
| `src/orchestra/control/fast_loop/playbooks.py` | 两张表、抽表、绑定到编辑或 `TemplateSwitch` |
| `src/orchestra/control/fast_loop/playbook_generator.py` | 锚点 + 按表生成候选 |
| `src/orchestra/control/fast_loop/plan_candidates.py` | 换模板重编译、合同注册 |
| `src/orchestra/control/fast_loop/controller.py` | 接线：诊断、`search_reason`、跑候选 |
| `src/orchestra/control/fast_loop/llm_diagnosis.py` | 失败里程碑的 LLM 分类 |
| `src/orchestra/control/fast_loop/diagnosis.py` | lookup 诊断 |
| `src/orchestra/control/fast_loop/quality_trigger.py` | 何时搜、incumbent、质量「诊断」 |
| `src/orchestra/control/fast_loop/edit_engine.py` | 编辑层落地（未改语义） |
| `src/orchestra/control/fast_loop/schemas.py` | `FailureDiagnosis` / `LocalCandidate` / `PlanRecompile` |
| `src/orchestra/cli/run_codeprojecteval_decomp.py` | `TuningConfig.playbook_search` + `diagnosis` |

生成器和选择器是两件事。`playbook_search` 与 `design_search` **都用 Pareto 选择器**；差别只在候选从哪来。`search_reason` 只有 playbook 生成器认。

### 5.2 控制器里怎么决定用哪张表

`FastLoopController.__init__`（`controller.py` 约 120–138 行）：

- `playbook_search` → `PlaybookCandidateGenerator` + `ParetoCandidateSelector`
- `elif design_search` → atomic-edit 生成器 + 同一个 Pareto 选择器
- 否则 → 规则生成器 + 确定性选择器
- 两真 → `ValueError`

`run()`（约 258–367 行）：

1. 有 `incumbent`：质量搜索。已 COMMITTED 的里程碑**不会**早退。诊断用 `quality_search_diagnosis`。`fl_state.search_reason = "quality"`，incumbent 进前沿。
2. 无 incumbent：失败搜索。`diagnosis.mode == "llm"` 才调用 `_refine_lookup`。
3. `generate(..., search_reason="quality" if incumbent else "failure")`。

质量搜索不调 LLM 是刻意的：`if incumbent ... elif mode==llm`。质量「诊断」只带 furthest stage、错题名、passed/total，没有失败类。

### 5.3 `Playbook` 是菜谱，不是图

`playbooks.py` 里的 `Playbook` 是冻结 dataclass。生成器按表取出一行，再 **bind** 成编辑或开关。表就是全部可达设计空间，生成器不发明行。

| 字段 | 含义 |
|------|------|
| `playbook_id` | 稳定 id，写进 `LocalCandidate` / `CandidateRecord`，用来算胜率 |
| `reason` | 人读的一句话，也是 `generation_reason` |
| `classes` | 失败表按 `budget` / `functional` / `design` 过滤；质量表为空集 |
| `templates` | 父模板。空 = 通用兜底。有专属行时兜底被藏住 |
| `target` | 编辑层贴到哪个槽；空 = 诊断锚点。计划层也可表示「重编译后贴谁」 |
| `include_failure_list` | 是否把点名错题写进 prompt |
| `feedback_slots` | 计划层重编译后，把名单贴到**新图**的这些槽；空则用 `target` |
| `steps_delta` / `timeout_delta` | 编辑层预算 |
| `extra_prompt` | 额外护栏（质量表：不许删 suite / 公开符号） |
| `switch_template` | 非空 → 计划层，`layer == "plan"` |
| `switch_slots` | 新模板槽 → 角色 |
| `carry` | 父槽角色拷到新槽（如 solo 的 `author` → chain 的 `first`） |
| `stage_slot` | 按 `furthest_stage` 填一个槽 |
| `swap_reviewer` | `contract_critic` / `spec_auditor` / `behaviour_critic` 轮转 |
| `search_reasons` | 默认只有 `failure`；质量行必须显式 opt-in |

反馈锚点**不在表里**。它永远是草案第 0 项，`playbook_id=""`，用来回答「这道菜谱相对 preamble 买到了什么」。不要为了腾出 k 把它丢掉。

### 5.4 抽表：`playbooks_for`

```
catalog_for(search_reason)
    quality → QUALITY_CATALOG
    failure → CATALOG
    测试可传入 stub catalog

过滤 search_reasons 包含当前原因的行

质量：忽略 failure_class
    当前 template 有专属行 → 只返回那些（按表顺序）
    否则 → templates 为空的兜底

失败：必须给 failure_class
    同样「专属藏兜底」
```

`test_first` + 质量 → 三行：builder 名单、换 `test_first_improve`、换 `test_first_quality_diagnosed`。  
`test_first` + 失败 + functional → 三行：名单给 repairer、换 diagnosed、换 double repair。  
`solo` + 质量 → 兜底两行（质量表没有 solo 专属行）。

`infer_failure_class`：诊断已写 `failure_class` 就用它；否则 timeout / `compile` / `imports` → budget，其余 → functional。`design` 只由 LLM 写出，lookup 不猜。

### 5.5 绑定：同一行变成两种东西

**编辑层** `bind_edits`：在**当前图**上解析 `target` 槽 → 节点 id，产出 `PromptFeedbackEdit` / `BudgetAdjustmentEdit`。质量名单用 `_format_quality_failures`（含 6/42、点名不完整），失败名单用 `_format_failures`。名字缩成 `Class::test`。

**计划层** `bind_switch`：产出 `TemplateSwitch(template_id, slots, playbook_id, reason)`。`carry` / `stage_slot` / `swap_reviewer` 在这里填槽。

**计划层贴名单** `bind_recompile_feedback`：必须在**新图**的 `PlaybookContext` 上调用。父图没有 `improver`，名单贴父 builder 等于没贴。槽来自 `feedback_slots` 或 `target`。

`playbook_applies` 会跳过：要名单但诊断没有点名；编辑层目标槽不存在；换 reviewer 却没有下一个；`stage_slot` 已经是该角色（换了也是同一张图）。

### 5.6 生成器：`PlaybookCandidateGenerator.generate`

```
不可重试 / infra → []
找不到锚点 agent → []

context_for(...)                         # 模板、槽图、失败类
k = min(budget.max_candidates, MAX_LOCAL_CANDIDATES)

[0] cand_feedback                        # preamble：concise_feedback + FRESH
[1..] 按 playbooks_for 顺序
        playbook_applies? 否则 skip
        编辑层：preamble + bind_edits → apply_local_edits
        计划层：bind_switch → recompile_candidate
                → _annotate_recompile（名单打到新槽，保留 plan_recompile）

filter_compatible_candidates             # 能力不够的标 REJECTED，仍审计
```

编辑层候选带着 preamble，所以和「只看同一份失败报告、换会话」的锚点可比。计划层不把 preamble 打到父锚点上：新形状自己的 gate 报告才是那些 agent 该看的证据；错题名由 `_annotate_recompile` 打到新槽。

候选 id：`cand_{playbook_id}`。锚点 id 固定 `cand_feedback`。

### 5.7 计划层重编译

`recompile_candidate`（`plan_candidates.py`）：

1. 从 `graph.metadata.milestone_draft_path` 读出当初的 `MilestoneDraft`。没有草稿就不能换形状。
2. `_fill_slots`：同名槽继承角色，开关点名的槽用新角色，可选槽不点名就不填。
3. 模板没变且槽也没变 → `PlanRecompileError`（不能赢也不能输的复制品）。
4. `materialize_milestone_subgraph(..., contract_namespace=candidate_id)`，合同和图谱写在候选自己的命名空间，不覆盖父里程碑。
5. `LocalCandidate.plan_recompile` 记录模板对、全槽赋值、真正动过的槽。光看 `edits` 不够：裸计划层 `edits=[]`；质量开关若贴了名单，`edits` 里会有 prompt，但形状仍以 `plan_recompile` 为准。

`register_new_contracts`：图点名的 `contract_id` 只要 compiler **或** executor 缺，就 `load_contracts(contracts_dir)` 后两份 `update`。只更新 compiler 会编译过、运行时 `KeyError`（4.1 的坑）。控制器在 `compile` 前：

```python
register_new_contracts(
    compiler=self.compiler,
    graph=candidate_graph,
    contracts_dir=self.contracts_dir,
    executor_contracts=getattr(agent_executor, "contracts", None),
)
```

### 5.8 诊断与质量触发

**失败 lookup**（`diagnosis.py`）：`SubtaskFailureReason` → 固定 `recommended_edit_types`，带 `furthest_stage`、`behaviour_failures`。Playbook 生成器不读 `recommended_edit_types`，只读类、锚点、名单。

**失败 LLM**（`llm_diagnosis.py`）：只输出 class / confidence / `target_node_id` / rationale / evidence，**不写编辑**。`temperature=0`，prompt+response 落到 `fast_loop/diagnosis/<subtask>_<attempt>.json`，花费 `accounting_source="fast_loop_diagnosis"`。模型不可用、JSON 坏、类不在枚举、置信度低于 `min_confidence` → 退回 lookup，诊断失败不得弄死里程碑。禁止把 held-out / `proj_with_test` / hidden tests 写进 prompt。

**质量诊断**（`quality_search_diagnosis`）：`reason=HARNESS`，`retryable=True`，无失败节点。带 `behaviour_failures`、`behaviour_total`、`behaviour_passed`（`round(score * total)`）。锚点是喂 gate 的那个 agent。

**何时开火**（`QualityTrigger.fires`）：`enabled` 且门过且 `behaviour_score` 非空且 **严格小于** `min_score`。CPE playbook yaml：`min_score: 0.9`。RealBench playbook yaml：`0.94`。未知行为分不开火。

Incumbent id 固定 `incumbent_first_pass`，以 `VALID`、真实花费进前沿。门没过的候选不能替换已过门的 incumbent。

### 5.9 CLI 开关

`TuningConfig`（`run_codeprojecteval_decomp.py`）：

```yaml
tuning:
  playbook_search: true
  design_search: false          # 两真报错
  fast_loop_candidates: 3       # k = 锚点 + 最多 k-1 道菜
  diagnosis:
    mode: llm                   # llm | deterministic
    min_confidence: 0.5
    model: gpt-5.4
  quality_trigger:
    enabled: true
    min_score: 0.9              # 比的是行为分
```

`k=3`：锚点 + 质量表前两行（builder 名单、换 improver）。第三行 `pb_tf_q_diagnose_then_improve` 要 `k=4`。

---

## 6. 两张表（现在的内容）

两表 `playbook_id` 无交集。质量行 `search_reasons={quality}`；失败行默认 `{failure}`。

### 6.1 失败表 `CATALOG`（门没过）

按模板，便宜/对症的在前。`functional` 与 `design` 暂时同组（`_FUNCTIONAL`）：lookup 几乎从不标 design，专开 design 组会变成死行。

**`test_first` / `test_first_diagnosed`**

| 顺序 | id | 类 | 层 | 动作 |
|------|----|----|----|------|
| 1 | `pb_tf_failures_to_repairer` | functional | 编辑 | 错题名 → `repairer` |
| 2 | `pb_tf_diagnose_before_repair` | functional | 计划 | → `test_first_diagnosed`（critic + repairer 都在失败门后） |
| 3 | `pb_tf_second_repairer` | functional | 计划 | → `test_first_double_repair` |
| — | `pb_tf_builder_budget` | budget | 编辑 | builder +2 steps / +30s |

**`gate_then_repair`**：名单 → repairer；budget → author。  
**`solo`**：→ `gate_then_repair`；→ `review_then_fix`；→ `chain` 且 second 按 stage；budget → author。  
**`review_then_fix`**：换 reviewer 角度；→ `parallel_audit`；budget 时丢掉 reviewer 变 `chain`。  
**`chain`**：填 third；→ `review_then_fix`；budget → second。  
**兜底**（无专属行时）：名单 → 锚点；小预算；大预算 + 「先过门再打磨」。

失败计划层（diagnose / second repairer）**还没有** `include_failure_list`。critic/repairer 只靠新形状自己的 gate 报告，不会像质量表那样在重编译后把点名名单打上去。这是失败表相对质量表落后的一点。

### 6.2 质量表 `QUALITY_CATALOG`（门过了，行为差）

原则：下一美元必须改变「写的人看见什么」或「谁来写」。`runs_if_gate_failed` 的修理工禁止出现。

**父形状 `test_first`（k=3 用前两行）**

| 顺序 | id | 层 | 动作 |
|------|----|----|------|
| ~~1~~ | ~~`pb_tf_q_failures_to_builder`~~ | 编辑 | 已删除（2026-08-31，三战三负，见 §0） |
| 1 | `pb_tf_q_improve_after_gate` | 计划 | → `test_first_improve`；名单 + guard → **improver**，角色由诊断决定 |
| 2 | `pb_tf_q_diagnose_then_improve` | 计划 | → `test_first_quality_diagnosed`；名单 → critic **和** improver，两个角色都由诊断决定 |

**已经是 improve 形状**（不要再切同一形状）

| 顺序 | id | 层 | 动作 |
|------|----|----|------|
| 1 | `pb_tf_q_failures_to_improver` | 编辑 | 名单 → improver（`test_first_improve` 与 `test_first_quality_diagnosed`） |
| 2 | `pb_tf_q_improver_budget` | 编辑 | improver +2 / +30s |
| 3 | `pb_tf_q_diagnose_from_improve` | 计划 | 仅从 `test_first_improve` → `test_first_quality_diagnosed` |

**兜底**：名单 → 锚点；锚点小预算。

`_QUALITY_GUARD`：不许删 `spec_tests` / `check_tests` / 文档化公开符号；不许还原 hidden tests；只改点名的行为。来自 4.2 里 `cand_feedback` 拆 suite、掉符号的教训。

质量 prompt 在有 totals 时会写：「suite 跑了 N 道、过了 P 道；点名了 M / (N-P)，名单不完整」。

---

## 7. 搜索专用模板与角色

都在 `configs/subgraph_templates/`，`planner_selectable: false`。规划器目录 `catalog_lines` 看不到它们。

| 模板 | 形状 | 何时用 |
|------|------|--------|
| `test_first` | test_author → builder → (repairer) | 规划器常选；early gate 在 builder 后 |
| `test_first_diagnosed` | + critic → repairer | 失败：拆诊断/修理；两者 `runs_if_gate_failed` |
| `test_first_double_repair` | 两轮 repairer | 失败：同一扇失败门后再修一轮 |
| `test_first_improve` | test_author → builder → **improver** | 质量：improver **每轮都跑**（门已过，不能藏在失败门后） |
| `test_first_quality_diagnosed` | + critic → improver | 质量：critic/improver 都每轮跑，无 early-gate |

只读槽不能放最后：compiler 冻结最后一个 agent 的 diff，只读尾巴会让 harness 打空 diff。所以 critic 永远在 repairer/improver 前面。角色 `behaviour_critic`：只读行为证据，不改仓库。improver 默认 `edge_case_hardener`。

---

## 8. 测试（证明表是分开的、名单打对了人）

控制面单测（`uv run pytest tests/unit/control`，2026-08-31 全绿）：

- `tests/unit/control/test_playbooks.py`：抽表顺序、两表不相交、improve 形状不再切自己、未知模板走兜底。
- `tests/unit/control/test_playbook_generator.py`：锚点、失败名单在 repairer、质量不抽失败表、improver 上看见名单、点名残缺文案、`k=4` 时 critic+improver 都有名单、atomic 生成器仍是固定草案。
- `tests/unit/control/test_plan_layer_candidates.py`：搜索模板不进规划器目录；`register_new_contracts` 同时更新 executor 字典。
- `tests/unit/control/test_llm_diagnosis.py`、`test_quality_trigger.py`：诊断与触发。

这些是单元证明。**当前质量表还没有一次付费端到端。**

---

## 9. 实验配置与环境

| yaml | 用途 |
|------|------|
| `configs/experiments/codeprojecteval_official_playbook.yaml` | CPE 官方 Codex，`k=3`，`output_root=outputs/cpe_official_playbook` |
| `configs/experiments/realbench_xiaoai_playbook.yaml` | RealBench + `smolagents_code`，`min_score: 0.94` |

结果目录必须和新的 `design_search` 臂分开，候选表不同，不能拼一张总表。

环境纪律（踩过的坑）：

- 仓库 `.env` 长期生效的是 **xiaoai**（`OPENAI_BASE_URL=https://xiaoai.plus/v1`）。xiaoai 是 OpenAI 兼容 chat，**不是** Codex runtime。用官方 playbook yaml + xiaoai env 会打到 `xiaoai.plus/v1/responses`。
- 官方探测不要改 `.env`。用启动器解析注释里的官方 key，确认 `api.openai.com` 再拉起；`load_env_file` 用 `setdefault`，进程先 export 官方变量可压过 xiaoai。
- 不要推 `main`。未明确要求不要 commit/push。

---

## 10. 已知缺口（按伤害排）

1. ~~当前代码未付费复跑。~~ 已复跑（§0）。improver 菜谱的合同判零根因是形状丢了早门，已修；它真正的对手是重抽的方差。
2. **失败计划层仍不贴点名名单。** `pb_tf_diagnose_before_repair` / `pb_tf_second_repairer` 没有 `include_failure_list`。质量侧已经有 `bind_recompile_feedback`，失败侧还没接上。
3. **Harness 点名远少于真实失败数。** 提示里会声明不完整，但模型仍然只看见 3 个名字。根因在 harness 的 `failed_tests`，不在菜谱。
4. **质量搜索不做 LLM 分类。** 若希望质量搜索也按「预算不够 vs 行为错」分菜，要另开一条，且不能复用失败类语义。
5. **`k=3` 到不了第三行质量菜谱。** critic+improver 从未在实跑出现。
6. **其它模板的质量表很薄。** CPE 冻结计划里 `test_first`+`solo` 约占 89%；solo 质量只有兜底。
7. **没有按 `playbook_id` 汇总胜率的报表。** 字段已经在 `CandidateRecord` 上，缺聚合脚本。
8. **INFRA 与资源天花板仍混在一起。** `infrastructure_related=True` 的生成器直接 `[]`，只走 `_infra_retry_once`。`SIGXCPU` 和供应商 401 还是一类。

---

## 11. 下一步（按建议顺序）

### P0 — 用现在的表做一次官方 imapclient `k=3`

目的：复验，不是扫全集。

- 配置：`configs/experiments/codeprojecteval_official_playbook.yaml`，冻结计划仍用 `imapclient.multi.test_first.json`。
- 后端：官方 `api.openai.com` + `gpt-5.4`，不要 xiaoai。
- 预算预期：量级与 4.1 类似（约 25–35 分钟、数美元）。
- 跑完必须对的三件事（pass rate 看不出）：
  1. M2 质量候选是 `incumbent`、`cand_feedback`、`cand_pb_tf_q_failures_to_builder`、`cand_pb_tf_q_improve_after_gate`，**没有** `pb_tf_failures_to_repairer`。
  2. improve 候选的 improver 节点 `prompt_feedback` 里有错题名和 `_QUALITY_GUARD`；`plan_recompile.template_id == test_first_improve`。
  3. 换模板候选跑完，不再出现合同 `KeyError` / 0.16s 死。
- 记录：选中谁、四条行为分、improve 相对 incumbent 是否涨、花费。写入本次 run 的 `summary.json` / `TRACE.md`，并在 `EXPERIMENT_LOG.md` 加一行（若沿用仓库实验日志习惯）。

### P1 — 失败计划层也贴名单

给 `pb_tf_diagnose_before_repair`（以及如有必要 `pb_tf_second_repairer`）加上 `include_failure_list` + `feedback_slots`（critic / repairer）。生成器已经会走 `_annotate_recompile`。补单测：重编译后 critic 或 repairer 的 feedback 含错题名。

### P1 — 失败搜索的官方探测

imapclient M1 很少搜；真正的失败搜索要等门没过的里程碑。可以：等质量复跑里若某候选把门打没；或找历史上 gate fail 更勤的任务。否则失败表仍只有单测证明。

### P2 — 视 P0 结果决定要不要 `k=4`

若 improve 有涨分但仍大量漏测，再付 `k=4` 看 `pb_tf_q_diagnose_then_improve`。不要在 P0 没看完时就把 k 拉到 4。

### P2 — 按 `playbook_id` 出对照表

从 `task_execution.json` / `pareto_frontiers` 抽：playbook、是否过门、行为分、是否被选、花费。先 imapclient，再扫的时候复用。没有这张表，多任务 sweep 无法说「哪道菜在赚钱」。

### P3 — 视需要再动

- harness `failed_tests` 点名不全：先确认是截断还是没收集，再决定是否在质量 prompt 里加 passed/total 之外的信号（不要把 hidden suite 源码塞进 prompt）。
- solo 质量专属行（RealBench 更可能碰到）。
- INFRA vs 资源上限分叉。
- 质量侧 LLM 分类：仅当 P0 显示「名单给错人 / 预算不够」混在一起时再做。

### 不要做的下一步

- 不要为了「看起来在搜」把失败修理工菜谱加回质量表。
- 不要把 `test_first_improve` / `test_first_quality_diagnosed` 设成规划器可选。
- 不要和 `outputs/cpe_official` 拼表。
- 不要在复跑前改 Pareto 规则——否则分不清是菜谱还是选择器在动。

---

## 12. 给接手的人：从哪读代码

按这个顺序读，大约一小时能跟上：

1. 本文第 5.1–5.2（接线）。
2. `playbooks.py`：`Playbook`、`playbooks_for`、`CATALOG`、`QUALITY_CATALOG`。
3. `playbook_generator.py`：`generate` → `_from_playbook` → `_recompile` → `_annotate_recompile`。
4. `plan_candidates.py`：`recompile_candidate`、`register_new_contracts`。
5. `controller.py`：`run()` 里 incumbent / LLM / `search_reason` 三段。
6. 四个 `test_first_*.yaml`。
7. `tests/unit/control/test_playbook_generator.py` 里质量相关四个测试（表、名单、残缺、k=4）。

对照实验：`design_search: true` 的 yaml（如 `configs/experiments/codeprojecteval_official_search.yaml`）仍是 atomic-edit + Pareto，**不用**这两张表。
