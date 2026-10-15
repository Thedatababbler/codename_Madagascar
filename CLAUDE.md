# AdaMAS — standing instructions for the implementing agent

## 沙盒规定（不可违反）

1. 任何会启动智能体或执行工作区代码的运行，都必须在沙盒内进行。沙盒未生效时，不得运行，不得以任何理由降级。
2. 不得为了让运行通过而放宽沙盒规则、关闭网络拦截、扩大可读路径或修改 LD_PRELOAD。确需调整沙盒规则时，先停下并向用户说明原因，得到同意后才能修改。
3. 不得读取、复制或移动 held out、参考实现、验证套件、memory/ 目录的内容到任何智能体可读的位置；不得在 /tmp 或其他共享位置留下题目代码、测试或参考实现的副本。
4. 每次任务结束时，在回报中必须包含"沙盒越界警告"一节（格式见 docs/sandbox_breach_log.md），即使数量为 0 也要写明。
5. 发现任何越界警告时，先在回报开头说明，再报告其他结果；不得自行判定"无害"后省略。

How the rules are enforced in code (2026-10-11): `src/orchestra/sandbox/guard.py` (refuses any agent start or
workspace-code run without the sandbox), `src/orchestra/sandbox/exec.py` (the launcher verifies its own
confinement), `src/orchestra/sandbox/breach.py` (the breach log), `configs/sandbox.yaml` (`enforced: true`, cannot
be turned off). The section of rule 4 is produced by `uv run python scripts/sandbox_breach_report.py <run dirs>`.

## 流水线正对照（2026-10-15 用户决定）

以后每次改动流水线（门、诊断、回归检查、修复证据、候选工作区、沙盒授权），都要先通过整条流水线的正对照，再做任何运行：

    uv run python scripts/pipeline_positive_control.py      # 或 pytest tests/integration/test_pipeline_positive_control.py

它用一个很小的合成题目（埋入已知错误，脚本代替智能体做固定改动），在真实沙盒和真实组件上检查：门能发现埋入的错误；
回归检查在现任代码上运行并发现故意弄坏的前序功能；120 个用例的大考卷结果完整读取；证据目录包含所有考卷的失败并通过
送达核查（存在、不为空、能收集、修复者的沙盒能读到）；修复后门和前序考卷全部通过。不调用模型，不花额度。
