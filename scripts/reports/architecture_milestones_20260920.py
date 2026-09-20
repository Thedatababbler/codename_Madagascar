# -*- coding: utf-8 -*-
"""AdaMAS 技术方案 / 架构原理 / 代码架构 总结（milestones 分支，2026-09-20）.

    .venv/bin/python scripts/reports/architecture_milestones_20260920.py docs/reports/adamas-architecture-milestones-20260920.pdf
"""
import sys

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

pdfmetrics.registerFont(TTFont("CJK", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", subfontIndex=0))

INK = colors.HexColor("#1A1A2E")
MUTED = colors.HexColor("#4A5568")
ACCENT = colors.HexColor("#1F497D")
RULE = colors.HexColor("#D5DAE3")
BAND = colors.HexColor("#EEF1F6")
BOX = colors.HexColor("#E8EEF7")
BOX2 = colors.HexColor("#FBEFE6")
BOX3 = colors.HexColor("#E9F4EC")

title = ParagraphStyle("t", fontName="CJK", fontSize=19, leading=25, textColor=INK, wordWrap="CJK")
sub = ParagraphStyle("s", fontName="CJK", fontSize=9.2, leading=13, textColor=MUTED, wordWrap="CJK")
h1 = ParagraphStyle("h1", fontName="CJK", fontSize=14, leading=18, textColor=ACCENT, spaceBefore=12, spaceAfter=5)
h2 = ParagraphStyle("h2", fontName="CJK", fontSize=11.2, leading=15, textColor=ACCENT, spaceBefore=8, spaceAfter=3)
body = ParagraphStyle("b", fontName="CJK", fontSize=9.6, leading=14.4, textColor=INK, wordWrap="CJK", spaceAfter=3)
bullet = ParagraphStyle("bl", parent=body, leftIndent=10, bulletIndent=0, spaceAfter=1.5)
cell = ParagraphStyle("c", fontName="CJK", fontSize=8.3, leading=10.8, textColor=INK, wordWrap="CJK")
cellb = ParagraphStyle("cb", parent=cell, textColor=colors.white)
note = ParagraphStyle("n", fontName="CJK", fontSize=8, leading=11.5, textColor=MUTED, wordWrap="CJK", spaceBefore=2)
cap = ParagraphStyle("cap", fontName="CJK", fontSize=8.4, leading=11.5, textColor=MUTED, wordWrap="CJK", spaceBefore=1, spaceAfter=6)


def P(t, st=body):
    return Paragraph(t, st)


def bl(t):
    return Paragraph(t, bullet, bulletText="•")


def table(rows, widths, header=True, size=8.3, zebra=True):
    data = []
    for i, r in enumerate(rows):
        data.append([Paragraph(str(c), cellb if (header and i == 0) else cell) for c in r])
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    st = [
        ("FONTNAME", (0, 0), (-1, -1), "CJK"),
        ("FONTSIZE", (0, 0), (-1, -1), size),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
    ]
    if header:
        st += [("BACKGROUND", (0, 0), (-1, 0), ACCENT)]
    if zebra:
        st += [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND])]
    t.setStyle(TableStyle(st))
    return t


# --------------------------------------------------------------------------- figures

def _box(d, x, y, w, h, text, fill=BOX, size=7.6):
    d.add(Rect(x, y, w, h, fillColor=fill, strokeColor=ACCENT, strokeWidth=0.6, rx=2, ry=2))
    lines = text.split("\n")
    total = len(lines)
    for i, ln in enumerate(lines):
        yy = y + h / 2 + (total - 1) * size * 0.65 - i * size * 1.3
        d.add(String(x + w / 2, yy - size * 0.35, ln, fontName="CJK", fontSize=size, fillColor=INK, textAnchor="middle"))


def _arrow(d, x1, y1, x2, y2, color=ACCENT):
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=0.8))
    import math
    ang = math.atan2(y2 - y1, x2 - x1)
    L = 4
    p = [x2, y2,
         x2 - L * math.cos(ang - 0.5), y2 - L * math.sin(ang - 0.5),
         x2 - L * math.cos(ang + 0.5), y2 - L * math.sin(ang + 0.5)]
    d.add(Polygon(p, fillColor=color, strokeColor=color, strokeWidth=0.3))


def fig_pipeline():
    W, H = 178 * mm, 62 * mm
    d = Drawing(W, H)
    y0 = H - 22 * mm
    bw, bh = 30 * mm, 14 * mm
    xs = [2 * mm, 36 * mm, 70 * mm, 104 * mm, 138 * mm]
    labels = ["设计文档 + 空仓库\n(PRD / 架构 / 目录树)",
              "规划器\n按功能拆 2–5 个里程碑\n基础先行，整合最后",
              "每个里程碑\n模板 + 角色池\n出题者先写套件并冻结",
              "验收门\n合约检查 + 冻结套件\n(空仓库基线剔除无效用例)",
              "提交到 canonical 工作区\n下一里程碑在其上继续"]
    for x, t in zip(xs, labels):
        _box(d, x, y0, bw, bh, t)
    for a, b in zip(xs, xs[1:]):
        _arrow(d, a + bw, y0 + bh / 2, b, y0 + bh / 2)
    # gate fail loop
    gx = xs[3] + bw / 2
    _arrow(d, gx, y0, gx, y0 - 9 * mm)
    sx = xs[2] + 4 * mm
    _box(d, sx, y0 - 24 * mm, 60 * mm, 15 * mm,
         "门失败 / 分数低 → 快速搜索（见图 2）\n探针 ×2 → 持续/偶发 → 续修候选 /\n节点重采样 / 重出题；只提交不低于现任的候选", fill=BOX2, size=7.2)
    _arrow(d, sx + 10 * mm, y0 - 9 * mm, sx + 10 * mm, y0 - 1)
    # held-out
    _box(d, xs[4], y0 - 24 * mm, bw, 15 * mm, "全部里程碑提交后\nheld-out unit_tests 评分\n(从不进入工作区或提示词)", fill=BOX3)
    _arrow(d, xs[4] + bw / 2, y0, xs[4] + bw / 2, y0 - 9 * mm)
    d.add(String(2 * mm, H - 6 * mm, "图 1  一道题的处理流程（milestones 分支）", fontName="CJK", fontSize=8.5, fillColor=MUTED))
    return d


def fig_routing():
    W, H = 178 * mm, 74 * mm
    d = Drawing(W, H)
    d.add(String(2 * mm, H - 6 * mm, "图 2  验收门失败后的快速搜索：先诊断，再按规则表选一行", fontName="CJK", fontSize=8.5, fillColor=MUTED))
    y1 = H - 24 * mm
    bw, bh = 34 * mm, 13 * mm
    _box(d, 2 * mm, y1, bw, bh, "现任结果\n(首轮图跑完，门失败\n或分数 < 0.9)")
    _box(d, 44 * mm, y1, 40 * mm, bh, "探针：同一设计再跑 2 次\n每个失败用例：\n每次都失败 = 持续；否则 = 偶发")
    _arrow(d, 2 * mm + bw, y1 + bh / 2, 44 * mm, y1 + bh / 2)
    # three routes
    y2 = y1 - 24 * mm
    routes = [
        (2 * mm, "套件一个都收集不到 / 全零\n→ 重出题\n(出题者重写，逐样本冻结)", BOX2),
        (46 * mm, "持续失败集非空\n→ 续修候选\n(一个修复者在现任工作区上\n只拿到持续失败清单与证据)", BOX),
        (90 * mm, "偶发失败 ≥2 且 ≥2/3 归同一节点\n→ 节点重采样\n(固定前缀，重跑该节点 N 次\n带失败清单，样本一致即早停)", BOX),
    ]
    for x, t, f in routes:
        _box(d, x, y2, 42 * mm, 17 * mm, t, fill=f, size=7.2)
        _arrow(d, 64 * mm, y1, x + 21 * mm, y2 + 17 * mm)
    _box(d, 136 * mm, y2, 40 * mm, 17 * mm, "选择\n候选须通过门且不低于现任\n(同设计噪声≈0.04，ε=0.02)\n提交或保留现任", fill=BOX3, size=7.2)
    yb = y2 - 5 * mm
    for x, _, _ in routes:
        d.add(Line(x + 21 * mm, y2, x + 21 * mm, yb, strokeColor=ACCENT, strokeWidth=0.8))
    d.add(Line(23 * mm, yb, 156 * mm, yb, strokeColor=ACCENT, strokeWidth=0.8))
    _arrow(d, 156 * mm, yb, 156 * mm, y2)
    d.add(String(2 * mm, y2 - 11 * mm, "每个里程碑预算：3 个候选（2 个探针 + 1 个候选）；门通过后如内部分 < 0.9 也会搜索（quality trigger）。",
                 fontName="CJK", fontSize=7.4, fillColor=MUTED))
    return d


# --------------------------------------------------------------------------- content

def build(out):
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                            bottomMargin=13 * mm, title="AdaMAS 技术方案与代码架构（milestones 分支）", author="AdaMAS")
    W = A4[0] - 32 * mm
    s = []

    s += [P("AdaMAS：技术方案、架构原理与代码架构", title),
          P("以 <b>milestones</b> 分支当前版本为准（提交 c097a99b，2026-09-20）· 评测集 CodeProjectEval (CPE) 与 NL2Repo-Bench · 模型 gpt-5.5（本地 CLIProxyAPI 通道）", sub),
          Spacer(1, 4 * mm)]

    # ---- 1 一句话
    s += [P("1. 一句话说明", h1),
          P("AdaMAS 接收一套设计文档（PRD、架构设计、目录树）和一个空仓库，输出实现完整的仓库，"
            "由数据集自带、从不进入工作区的 held-out 单元测试打分。做法是三层："
            "<b>按功能把任务拆成 2–5 个可验收的里程碑</b>；<b>每个里程碑由一个小的多智能体拓扑执行，"
            "先由出题者从文档写出测试套件并冻结，再由实现者去做，最后过验收门</b>；"
            "<b>验收门失败时做一次有诊断的快速搜索</b>（区分持续失败与偶发失败，分别走续修候选、节点重采样或重出题），"
            "只提交不低于现任的候选。"),
          fig_pipeline(),
          P("图 1 说明：每个方框对应代码中的一层（规划器、模板/角色、验收门、canonical 工作区、快速搜索），第 4 节按文件列出。", cap)]

    # ---- 2 技术方案
    s += [P("2. 技术方案（现在是怎么做的）", h1),
          P("2.1 任务拆分：按功能模块拆里程碑", h2),
          P("规划器（<b>milestone_planner.py</b>）读设计文档，在 <b>split_policy: feature</b> 下按文档里的功能模块拆成 2–5 个里程碑，"
            "基础合约（异常、数据结构、注册表）先行，公共 API / 打包 / CLI 的整合里程碑最后。每个里程碑带：目标、交付说明、"
            "验收标准、关注路径、模板 id、角色分配、依赖（解析器强制串成一条链，保证 canonical 工作区历史线性）。"
            "计划一次生成后冻结成 JSON 回放（<b>configs/datasets/*_feature_plans/</b>），A/B 时不再引入规划抽样噪声。"),
          P("原来的风险优先策略默认 1 个里程碑（18 道 CPE 题平均 1.33 个），改成按功能拆后 18/18 题被拆开（平均 4.67 个）。"),
          P("2.2 里程碑内部：模板 + 角色池，先出题再做题", h2),
          P("每个里程碑不是让 LLM 自由设计拓扑，而是从固定的<b>模板</b>里选一个形状，再从固定的<b>角色池</b>里给每个槽位选角色。"
            "规划器可选的 5 个模板都以 <b>test_author</b> 槽位开头："),
          table([["模板", "槽位（默认角色）", "适用"],
                 ["test_first", "test_author → builder(implementer) → repairer(gate_repairer)，builder 后提前过门", "默认；先做后修"],
                 ["gate_then_repair", "test_author → author → repairer，author 后提前过门", "同上，修复槽位读门报告"],
                 ["review_then_fix", "test_author → author → reviewer(contract_critic，只读) → fixer", "风险在违反已冻结合约"],
                 ["parallel_audit", "test_author → author → spec_review ‖ contract_review → fixer", "整合里程碑，两路审查"],
                 ["chain", "test_author → contract_author → implementer → integrator", "合约 → 实现 → 接线"],
                 ["author_only / solo / continuation / test_first_*", "非规划器可选：探针专用、历史回放、搜索候选专用形状", "—"]],
                [30 * mm, 100 * mm, W - 130 * mm]),
          Spacer(1, 2 * mm),
          P("<b>出题者（test_author）</b>只从文档出题，写到 spec_tests/，然后套件被移出工作区冻结（custody），后面的智能体看不到也改不了。"
            "验收门用这份冻结套件打分，并先在空仓库上跑一遍，把在空仓库上也能通过的“无效用例”剔除。"
            "出题者提示词迭代到 v10.1：每条断言须引用文档原句；不得测私有表示、未记录的错误路径和默认值；必须到达文档命名的状态转换（如分裂、合并、重开）；"
            "批量场景带 20 秒时限；<b>不得靠初始化调用、垫片或访问内部让测试通过</b>；模块顶层只导入 pytest 和通用标准库。"),
          P("2.3 验收门（harness）", h2),
          P("运行器把一个自包含的检查脚本写进 harness 目录，在任务自己的虚拟环境里执行，阶段为：<b>contracts</b>（由验收标准和关注路径生成的确定性合约检查）、"
            "<b>cross_imports</b>（模块间导入可达）、<b>spec_tests</b>（冻结套件；"
            "每用例 signal 方式超时，未跑完的用例记名）、<b>vacuous</b>（空仓库基线）。门的输出是分数 + 逐用例通过/失败清单，写入 summary 和 fast_loop 记录。"
            "三处守卫：custody 对不可采集的套件直接拒收并要求重出题；<b>包影子</b>让实现不了的项目包不会被环境里的上游包顶替；"
            "运行前后清除任务包在环境里的外来安装（editable 安装、.pth）。"),
          P("2.4 快速搜索（门失败或分数低时）", h2),
          fig_routing(),
          P("图 2 说明：三条路线都是规则表（playbook）里的一行，带前置条件；没有让 LLM 自由改拓扑。续修候选只运行一个修复者，"
            "在现任的工作区上工作，拿到持续失败清单与“修复证据”（失败用例的源码和输出）；节点重采样固定被指认节点之前的前缀，"
            "只重跑该节点 N 次；重出题在逐样本的冻结目录上重新出题，选分数在 (0,1) 之间且可判定用例最多的。", cap),
          P("2.5 里程碑之间", h2),
          bl("每个里程碑在前一个提交的 canonical 工作区上开工；交付说明告诉它“扩展而非重设计”。"),
          bl("已冻结的套件在后续里程碑不再运行（回归盲区），因此整合里程碑的出题者被要求覆盖广度：文档列出的每个公共符号至少调用一次、错误路径、对象协议、跨模块状态。"),
          bl("0 分里程碑不得提交到已有基础之上（<b>refuse_zero_commit_over_base</b>）；支持从断点续跑（--resume / --resume-from，回滚 canonical、清检查点）。"),
          bl("held-out 评分由独立脚本在全部里程碑提交后进行，运行器本身从不接触 unit_tests。")]

    s += [PageBreak(), P("3. 架构原理（为什么这样设计）", h1),
          bl("<b>验收标准来自文档而不是实现者。</b>没有冻结套件时，“做完了”由实现者自己说了算；出题者先写、冻结、后续看不见，是把“什么算对”从实现者手里拿出来。它也是搜索的目标函数，所以套件质量决定搜索优化的方向，出题者提示词是迭代最多的部分（v1–v10.1）。"),
          bl("<b>拆分的目的是可验收，不是并行。</b>里程碑串成一条链，换取线性的工作区历史和每个里程碑自己的验收门；前面里程碑的缺陷在它自己的门上暴露（tinydb 的 LRUCache、bplustree 的树层挂住），而不是留到最后。"),
          bl("<b>搜索先诊断再行动。</b>早期实验证明剧本表盲改拓扑打不过同设计重采样（best-of-n 解释了全部收益），同设计噪声约一个用例（0.04）。于是把预算用在“这个失败是不是每次都失败”上：持续失败给续修候选（模型无关的配对记录：6/8 修好、0 回归），偶发失败才值得重采样。"),
          bl("<b>规则而不是 LLM 决定形状。</b>模板 + 角色池 + 规则表限制了搜索空间，让每次决策可记录、可回放；LLM 只用于诊断分类（可切换为确定性规则），并有置信度门槛。"),
          bl("<b>不信任提示词能守住的，用机制守住。</b>包影子、环境清理、custody 拒收、0 分守卫、held-out 隔离，都是在发现智能体绕过验收后加的机械守卫；出题探针（第 5 节）也是同一思路：不跑整题，只审套件。"),
          bl("<b>历史包袱有意保留。</b>M5 慢环（跨里程碑改计划）和 M6 Pareto 搜索在代码里存在但关闭；solo 等模板留着是为了回放旧的冻结计划。"),
          ]

    # ---- 4 code architecture
    s += [P("4. 代码架构：哪个文件做什么", h1),
          P("仓库根 <b>/root/projects/AdaMAS</b>，Python 包 <b>src/orchestra/</b>（约 160 个模块）。下表按主线数据流分组；标 ✱ 的是当前实验主线每次运行都会经过的文件，其余是历史或关闭的能力。", body),
          P("4.1 入口与数据集", h2),
          table([["文件", "职责"],
                 ["✱ cli/run_codeprojecteval_decomp.py", "主运行器：加载任务 → 物化验收门 → 规划或回放计划 → 为每个里程碑绑定合约与门命令 → 编译子图 → 交给调度器 → 写 summary.json / TRACE.md；含 --resume、--resume-from、包清理"],
                 ["✱ codeprojecteval/dataset.py", "读取任务目录（config.json、docs、src、requirements），构造只含文档和空仓库的工作区；CPE_DATASET_ROOT / CPE_ENV_ROOT 切换 NL2Repo"],
                 ["✱ codeprojecteval/harness.py", "验收门：生成自包含的 adamas_cpe_check.py（三引号脚本，转义需双写），阶段 contracts / cross_imports / spec_tests / vacuous；custody 冻结与拒收；包影子；逐用例超时与记名"],
                 ["✱ codeprojecteval/planning.py", "给规划器的简报（文档 + 模块列表）"],
                 ["codeprojecteval/ab.py", "从一份规划草案派生 solo / single / multi 等对照臂；load_draft 回放冻结计划"],
                 ["codeprojecteval/ceiling.py, suite_sizes.py", "文档能覆盖 held-out 的上限分析；固定各题 held-out 用例数，防止分母漂移"],
                 ["scripts/eval_codeprojecteval.py", "held-out 评分（独立进程，--memory-mb 默认 32 GB）；scripts/nl2repo_to_cpe.py 把 NL2Repo 转成 CPE 格式"]],
                [52 * mm, W - 52 * mm]),
          P("4.2 规划与子图（realbench/、decomposition/、roles/、configs/）", h2),
          table([["文件", "职责"],
                 ["✱ realbench/milestone_planner.py", "里程碑规划器：提示词（risk / feature 两种策略）、解析与规范化（补依赖链、最后一个设为整合、折叠无理由的拆分）、模板解析"],
                 ["✱ realbench/subgraph_builder.py", "把一个里程碑 + 模板 + 角色编译成运行时子图：每个槽位一个智能体节点、系统提示词（角色 + 里程碑简报 + 验收标准 + 出题者附加段）、custody 节点、门节点与提前过门"],
                 ["✱ realbench/milestone_contracts.py, public_check.py, public_harness.py", "由验收标准生成确定性合约检查并物化成可执行检查"],
                 ["✱ decomposition/realbench_plan.py", "TaskPlan 构造：里程碑简报文本（交付说明 / 独立子系统说明）、子任务预算、依赖"],
                 ["decomposition/schemas.py, validator.py, decomposer.py", "任务/子任务 IR、DAG 校验、非法计划回退单子任务"],
                 ["✱ roles/pool.py, roles/templates.py", "角色池与模板加载（ORCHESTRA_ROLE_POOL_DIR 可指向冻结副本做对照）"],
                 ["✱ configs/roles/*.yaml（12 个）", "每个角色的能力说明、是否改仓库、预算与提示词；test_author.yaml 为 v10.1"],
                 ["✱ configs/subgraph_templates/*.yaml（12 个）", "模板形状：槽位、默认/允许角色、边、提前过门槽位、是否规划器可选"],
                 ["✱ configs/experiments/*.yaml", "实验配置：搜索开关、候选数、诊断模式、质量触发、输出目录；主线为 codeprojecteval_official_milestones.yaml，探针为 author_probe.yaml"],
                 ["✱ configs/datasets/", "冻结的功能计划（cpe_feature_plans 18 题、nl2repo_feature_plans 9 题）"]],
                [52 * mm, W - 52 * mm]),
          ]
    s += [P("4.3 控制面：调度、工作区、快速搜索（control/）", h2),
          table([["文件", "职责"],
                 ["✱ control/ready_scheduler.py", "就绪子任务调度器：按依赖顺序跑每个里程碑，收门结果，决定提交 / 搜索 / 保留现任 / 跳过被阻塞的依赖者；读取 custody 拒收边车；质量触发"],
                 ["✱ control/canonical_workspace.py, runtime/committer.py", "任务级 canonical 仓库：里程碑从它分叉，胜者原子提交回去；断点续跑时回滚"],
                 ["✱ control/task_state.py, runtime/task_checkpoint.py", "任务与子任务执行状态、尝试记录、检查点"],
                 ["✱ control/fast_loop/controller.py", "快速搜索控制器：探针、持续/偶发分流、续修候选、节点重采样、重出题、选择与提交、恢复后的持续搜索、拒收套件分支"],
                 ["✱ control/fast_loop/persistence.py, diagnosis.py, llm_diagnosis.py", "持续失败诊断（多样本交集）、确定性 / LLM 失败分类（类别 + 被指认节点 + 推荐形状）"],
                 ["✱ control/fast_loop/playbooks.py, playbook_generator.py, plan_candidates.py", "规则表（按搜索原因、失败类别、模板形状索引的行，含前置条件）；从表行生成候选；候选通过重新编译里程碑而不是改图"],
                 ["✱ control/fast_loop/node_resample.py", "节点级 best-of-N：固定前缀，带失败清单重跑被指认节点，样本一致早停，符号归属"],
                 ["✱ control/fast_loop/repair_evidence.py", "把失败用例的源码与输出放到修复者工作区旁（打分仍用冻结副本）"],
                 ["✱ control/fast_loop/workspace.py, budget.py, selector.py, objectives.py, quality_trigger.py", "候选工作区隔离与原子提交、预算（候选数 / 调用数 / 时钟）、选择规则（质量优先，ε）、目标计算、门通过后的搜索触发"],
                 ["control/fast_loop/candidate_generator.py, edit_engine.py, pareto.py, schemas.py", "早期的原子图编辑与 Pareto 候选（保留，主线不用）"],
                 ["control/slow_loop/*, control/pareto/*", "M5 慢环（跨里程碑改计划）与 M6 Pareto 全局搜索：存在但在主线配置里关闭"],
                 ["control/backend_usage.py, failure.py, input_assembler.py", "用量遥测归一、失败原因映射、子任务输入装配"]],
                [52 * mm, W - 52 * mm]),
          P("4.4 运行时与后端（ir/、runtime/、backends/、harness/、executors/）", h2),
          table([["文件", "职责"],
                 ["✱ ir/graph.py, nodes.py, edges.py, compiler.py, graph_invariants.py", "子图 IR 与编译；结构不变式（套件槽位在前、门在后、custody 只在有下游消费者时接入）"],
                 ["✱ runtime/native_async.py, scheduler.py, state.py, checkpoint.py", "按拓扑序异步执行节点，图级检查点"],
                 ["✱ executors/agent.py, harness.py, transform.py", "三类节点的执行器：智能体节点走后端；门节点跑验收脚本；转换节点做工件搬运"],
                 ["✱ backends/codex_sdk.py（+ codex_types, exception_mapping, health）", "Codex SDK 后端：把系统提示词 + 工作区交给模型，收回仓库变更与用量；健康检查；OPENAI_BASE_URL 指向本地代理"],
                 ["backends/smolagents_code.py, workers/, tools/repository_tools.py", "smolagents 代码智能体后端（隔离工作进程，受限仓库工具）"],
                 ["✱ harness/repository_test.py, progress.py, command_runner.py, env_redaction.py", "在工作区执行门命令、解析 pytest 进度、脱敏环境变量"],
                 ["✱ workspaces/git_workspace.py", "每个子任务的 git 工作区（分叉、diff、变更集）"],
                 ["communication/*", "子任务间投递计划与账本（M5 能力，主线只用默认投递）"],
                 ["schemas/, storage/, telemetry/, settings.py, config.py", "工件类型、事件与工件存储、summary 生成、运行设置解析"],
                 ["adapters/livecodebench, sandbox/, tasks/bbeh.py, experiments/stage*", "更早阶段（LiveCodeBench、BBEH、Stage 1/2）的适配与实验，主线不用"]],
                [52 * mm, W - 52 * mm]),
          P("4.5 脚本、测试与文档", h2),
          table([["位置", "内容"],
                 ["scripts/", "eval_codeprojecteval.py（held-out 评分）、probe_codeprojecteval_planner.py（只跑规划器）、author_probe.py + author_probe_compare.py（只跑出题者并审计）、audit_authored_suites.py（套件深度审计）、cpe_full_table.py / nl2_full_table.py（汇总表）、probe_codeprojecteval_env.py（建环境）、reports/（PDF 生成脚本）"],
                 ["tests/unit/（约 150 个文件）", "规划解析、模板/角色、子图不变式、快速搜索各路线、验收门脚本可解析性、断点续跑等；.venv/bin/python -m pytest tests/unit -q"],
                 ["docs/", "设计文档：codeprojecteval_integration.md、fast_loop_playbook_search.md、node_resample_design.md、topology_and_edit.md、m4/m5/m6 各环；reports/ 为历次 PDF"],
                 ["EXPERIMENT_LOG.md", "实验日志，每次完成的实验一条（最新 EXP-20260917-01）；比较历史成绩前必读"],
                 ["outputs/", "运行产物（gitignore）：cpe_milestones/<batch>/<task>/{plan.yaml, milestone_plan_draft.json, harness/, tasks/rb_<task>/{canonical, subtasks, commit_staging}, fast_loop/, summary.json, TRACE.md, logs/}；hidden_eval.json 在 batch 根；author_probe/ 为探针"]],
                [40 * mm, W - 40 * mm])]

    # ---- 5 results
    cpe = [("bplustree", 5, 0.902, 0.890, 0.890), ("tinydb", 4, 0.868, 0.750, 0.877), ("pyjwt", 5, 0.779, 0.813, 0.748),
           ("simpy", 4, 0.805, 0.765, 0.765), ("imapclient", 5, 0.412, 0.356, 0.390), ("python-hl7", 5, 0.530, 0.530, 0.480),
           ("drf-simplejwt", 5, 0.759, 0.565, 0.031), ("flask", 5, 0.763, 0.639, 0.000)]
    mf = sum(r[2] for r in cpe) / 8; mo = sum(r[3] for r in cpe) / 8; mb = sum(r[4] for r in cpe) / 8
    rows = [["题目", "里程碑数", "按功能拆分", "原 2 里程碑", "最好基线"]] + [[t, str(n), f"{a:.3f}", f"{b:.3f}", f"{c:.3f}"] for t, n, a, b, c in cpe] + \
           [["平均（8 题）", "", f"{mf:.3f}", f"{mo:.3f}", f"{mb:.3f}"]]
    s += [P("5. 当前分支的结果", h1),
          P("5.1 CodeProjectEval 多阶段题（held-out 通过率，每格一次运行，噪声约 0.04）", h2),
          table(rows, [40 * mm, 22 * mm, 30 * mm, 30 * mm, 30 * mm]),
          P("8 题中 7 题不低于原 2 里程碑版，平均 +0.064；未计入的 trailscraper / portalocker 因严格评测拒绝计分。基线 = 直接调用 LLM 的五种方法（solo、best_of_3、self_refine、writer_reviewer、debate）里最好的一个。", note),
          P("5.2 NL2Repo 抽样（held-out，环境清理后）", h2),
          table([["题目", "按功能拆分 + 全部修复", "原 2 里程碑 (v9)", "备注"],
                 ["tablib", "0.491", "0.526", "剩余差距是覆盖广度；早期套件曾靠 register_builtins() 初始化绕过"],
                 ["tenacity", "0.903", "0.847", ""],
                 ["python-jose", "0.445", "0.106", "旧版受诊断路由缺陷影响"],
                 ["python-pathspec", "0.723", "0.756", "实现者曾造 tomllib.py 垫片（未处理，见第 6 节）"]],
                [30 * mm, 40 * mm, 34 * mm, W - 104 * mm]),
          P("5.3 出题质量探针（不跑整题，只审冻结套件；EXP-20260917-01）", h2),
          table([["来源", "套件数", "用例数", "初始化绕过用例", "垫片行数", "私有访问", "采集报错"],
                 ["v9 真实运行（09-15、09-16 早）", "14", "243", "30", "7", "0", "0"],
                 ["v9 真实运行（每题最新一次）", "18", "221", "0", "4", "0", "0"],
                 ["v9 探针（对照，同里程碑）", "6", "104", "0", "4", "0", "1"],
                 ["v10 探针", "10", "185", "0", "0", "0", "1"],
                 ["v10.1 探针（pathspec 整合，复测）", "1", "14", "0", "0", "0", "0"]],
                [52 * mm, 16 * mm, 16 * mm, 26 * mm, 20 * mm, 18 * mm, W - 148 * mm]),
          P("v9 的初始化绕过是阵发的（同一提示词三次运行：30 / 0 / 0），单次探针只能说明 v10 的 10 个套件里一个都没有。v10 的一次采集报错是模块顶层 import tomllib 遇到 3.10 环境，v10.1 已封住。", note)]

    # ---- 6 open
    s += [P("6. 已知问题与下一步", h1),
          bl("<b>实现者绕过验收门</b>：pathspec 的实现者在仓库根造了 tomllib.py 转发 tomli。建议在提交前加“工作区卫生”检查（新增顶层模块撞标准库/已装包名、新增 conftest.py / sitecustomize.py / .pth、改 pytest 配置即判门失败并命名原因），先以只记录模式跑历史产物看误报。"),
          bl("<b>v10.1 对题目得分的影响未测</b>：本轮只审了套件质量；规则会让套件比参考实现更严，需要在 NL2Repo 抽样和 CPE 上看 held-out 后再决定全面替换。"),
          bl("<b>合约阶段的私有名</b>：规划器把 PRD 里的 _formats 之类写进验收标准，合约检查据此判分（python-jose 曾因此 0.106 vs 0.693）。"),
          bl("<b>预算</b>：每里程碑 3 个候选时，节点重采样常被续修候选挤掉，无法比较两者；可试 3→4。"),
          bl("<b>回归盲区</b>：已冻结套件不在后续里程碑重跑，整合里程碑的广度要求只是缓解。"),
          bl("<b>配额</b>：两个 ChatGPT 账户按周限额；一次并发 11 个探针 10 分钟打满 5 小时窗口，并发保持 2–3。"),
          P("附：常用命令", h2),
          P("运行一题（NL2Repo 需设 CPE_DATASET_ROOT / CPE_ENV_ROOT）：<br/>"
            "<font face='CJK' size='8'>.venv/bin/python -m orchestra.cli.run_codeprojecteval_decomp --config configs/experiments/codeprojecteval_official_milestones.yaml --task-id tinydb --plan-file configs/datasets/cpe_feature_plans/tinydb.plan.json --arm multi --run-id &lt;id&gt;</font><br/>"
            "held-out 评分：<font face='CJK' size='8'>.venv/bin/python scripts/eval_codeprojecteval.py outputs/cpe_milestones/&lt;id&gt;</font><br/>"
            "只跑规划器：<font face='CJK' size='8'>scripts/probe_codeprojecteval_planner.py --split-policy feature --max-milestones 5</font>；"
            "只跑出题者：<font face='CJK' size='8'>scripts/author_probe.py run --tag v10 --select nl2_tablib:text_dataframe_formats</font>", body)]

    def on_page(canvas, doc_):
        canvas.saveState()
        canvas.setFont("CJK", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - 16 * mm, 8 * mm, f"AdaMAS · milestones 分支 · 2026-09-20 · 第 {doc_.page} 页")
        canvas.restoreState()

    doc.build(s, onFirstPage=on_page, onLaterPages=on_page)
    print("wrote", out)


if __name__ == "__main__":
    build(sys.argv[1])
