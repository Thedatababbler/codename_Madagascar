# CodeProjectEval × AdaMAS：真实验收测试驱动的 milestone

> 相关：`docs/realbench_dynamic_taskplan_harness.md`（同一套风险优先规划 + subgraph 运行时）

## 为什么接这个数据集

RealBench 的 milestone 闸门有两个先天缺陷：**最该被闸门保护的决策（公开 API 形状）由
`public_design/` 直接给定**，而且**开发期没有可见测试**，所以验收标准只能由我们从设计
文档反推出来——本质是猜的。

CodeProjectEval 同时补上这两块：

- 每个仓库自带 **可见的 `check_tests`**（约 10 个用例、63.4% 覆盖）供开发期使用，
  以及 **独立不重叠的 `unit_tests`**（约 186 个、90.7% 覆盖）用于最终评分；
- 于是 milestone 的验收 harness 跑的是**真实测试**，而隐藏测试能回答一个我们此前
  无法回答的问题：闸门是在真的防风险，还是只让 agent 过拟合了可见信号。

## 数据与环境

- 数据集：`/root/codex-benchmarks/projectgen/datasets/CodeProjectEval/python-subset`，18 个 Python 仓库
- 规模：597–9,308 LOC，3–30 个模块（远大于我们此前用的 RealBench 五题）
- 每仓一个虚拟环境：`/root/codex-benchmarks/cpe_envs/<repo>`，由
  `scripts/probe_codeprojecteval_env.py` provision（`uv venv` + 该仓自己的 `requirements.txt`）

探针结果（参考实现上 check_tests 与 unit_tests 均全绿才算可用）：**11/18 可用** —
bplustree、csvs-to-sqlite、deprecated、imapclient、parsel、portalocker、pyjwt、
python-hl7、simpy、tinydb、voluptuous。其余为环境或参考实现自身的问题
（cookiecutter/simplejwt/rsa 有少量 unit 失败，flask 有 collection error，
trailscraper/xmnlp 缺依赖，zxcvbn 收集不到用例）。

跑测试时统一用 `-o addopts=` 屏蔽仓库自带的 pytest 配置：这些仓库把覆盖率阈值、mypy、
pycodestyle 挂在 pytest 上，判的是风格而不是代码能不能工作（portalocker 就是因为
`--cov-fail-under=100` 被误判失败）。

## 工作区隔离

沿用 RealBench 的不变式，agent 工作区只有数据集给的东西：

| 进工作区 | 不进工作区 |
|---|---|
| `docs/PRD.md`、`docs/UML*.md`、`docs/architecture_design.md`、`docs/directory_tree.txt` | 参考实现（`source_code` 目录） |
| `requirements.txt`、README | 隐藏评分用的 `unit_tests/` |
| 可见的 `check_tests/`（数据集本来就打算给开发者看） | 检查脚本、manifest、契约 JSON、里程碑简报、跨阶段记忆 |

AdaMAS 造的一切仍然是 **runner 持有 + prompt 投递**。

## 验收 harness（`src/orchestra/codeprojecteval/harness.py`）

| level | 检查 |
|-------|------|
| discovery | 声明的顶层包存在 + `compileall` |
| implementation | 上述 + 导入 `directory_tree.txt` 声明的每个模块 + 契约检查 |
| integration | 上述 + **执行数据集的 `check_tests`** |

命令形如
`<repo_venv>/bin/python <harness_dir>/adamas_cpe_check.py --manifest ... --level ... [--contracts ...] [--tests ...]`，
以仓库为 `cwd`、用该仓自己的解释器运行，第三方依赖的解析方式与评分时一致。
`--tests` 允许把闸门收窄到与该 milestone 相关的用例。

## 规划输入

`PlanningBrief` 把规划器与数据集布局解耦（`milestone_planner.render_planner_prompt`）：
RealBench 提供 TASK/REQUIREMENTS/public_design，CodeProjectEval 提供
PRD/architecture_design/directory_tree/UML。CPE 的 brief 还告诉规划器：可见的
`check_tests` 会在每个 integration 闸门执行，评分套件不可见。

## 这个数据集适合分段吗：规划器的回答

对全部 18 个仓库跑风险优先规划器（两次独立采样）：

- 稳定判定需要分段的：**bplustree、pyjwt、simpy**
- 只在其中一次分段的：voluptuous、flask、trailscraper、zxcvbn
- 其余稳定单段

关键在于它给出的闸门理由是**真实的爆炸半径决策**，而不是按目录切：

- `bplustree` — 二进制页格式、元数据契约与序列化行为：定错了，后面所有树操作都建在错的
  磁盘结构上
- `pyjwt` — 算法名到实现的注册表与 JWK 契约：下游每个模块都 import 并信任它
- `simpy` — `Environment`/`Event`/`Process` 协议：resources、stores、containers、realtime
  全部依赖同一套事件生命周期语义
- `voluptuous` — Schema 编译行为、marker 语义与错误传播路径

这与 RealBench 形成对照：那五题里规划器几乎总是收敛成单段，因为 UML 已经把公开契约
交出来了，没有留下值得设闸门的决策。

由此得到一个天然的实验设计：**会分段的那批仓库是分解应当起作用的总体，稳定单段的那批
是对照组**。分段与否由规划器自己决定，不由我们按形状规定。

## 隐藏测试的天花板（重要）

首个真实 run（bplustree，单 milestone，1,132 行）**通过了全部可见 check_tests 并 commit**，
但隐藏 unit_tests 在收集阶段就挂了两个模块：

```
unit_tests/test_node.py: from bplustree.const import TreeConf, ENDIAN
ImportError: cannot import name 'ENDIAN' from 'bplustree.const'
```

`ENDIAN` 在 `PRD.md`、`UML.md`、`UML_pyreverse.md`、`architecture_design.md` 里**出现次数为
0**。隐藏套件是原项目自己的测试，写的是原项目的**内部**符号名，而规格从未声明它们。也就是
说这部分分数对任何系统都是不可达的，与分解好坏无关。

由此定下报告纪律：

- 绝对隐藏通过率不能单独解读，它包含一层"规格未声明的内部命名"噪声；
- 结论必须是**同一仓库上的相对比较**（单段 vs 多段，其余预算对齐）；
- 评分脚本分别记录 `failed` 与 `error`：收集期 ImportError 多半是未声明的内部命名，
  断言失败才更接近真实行为差距。可见 check_tests 的通过情况应与隐藏结果一并汇报。

## 规划器的采样方差

bplustree 连续三次规划分别得到 2、2、1 个 milestone。分段与否本身带随机性，所以对照实验
要么先固定一份 plan 再复用，要么对同一配置多次采样后平均——不能拿单次分段结果下结论。

## 现状与待办

已完成：数据集适配（`dataset.py`）、验收 harness（`harness.py`）、规划 brief
（`planning.py`）、环境 provision 与探针（`scripts/probe_codeprojecteval_env.py`）、
规划器分段探针（`scripts/probe_codeprojecteval_planner.py`）、
`build_plan_from_draft` 的 `harness_binder` 参数化、运行 CLI
（`orchestra.cli.run_codeprojecteval_decomp` + `configs/experiments/codeprojecteval_decomp.yaml`）、
离线评分脚本（`scripts/eval_codeprojecteval.py`）。链路两端都验证过：空仓库判 0，
参考实现判 1.000（bplustree 356 个隐藏用例全过）。

运行方式：

```bash
uv run python -m orchestra.cli.run_codeprojecteval_decomp --task-id bplustree
uv run python scripts/eval_codeprojecteval.py outputs/codeprojecteval_decomp/<batch>
```

评分读的是 `<batch>/<task>/tasks/*/canonical/repo`（milestone 合并后的仓库），不是批次级
那份保持数据集原样的 workspace。隐藏套件与可见套件在评分时都从数据集重新覆盖，agent 改过
的测试进不了评分。

待办：预算对齐的单段 vs 多段对照实验（同仓多次采样），以及把 7 个不可用仓库的环境补齐。
