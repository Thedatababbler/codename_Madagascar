"""Build the cross-experiment review PDF (Chinese, plain language).

Every number here is copied from a recorded artefact; the source of each table is
printed under it so a reader can check it. Run:

    uv pip install reportlab        # report-only dependency, not in pyproject
    apt-get install -y fonts-wqy-microhei   # the embedded Chinese face
    .venv/bin/python scripts/make_experiment_review_pdf.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.textlabels import Label
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT = Path("docs/reports/experiment_review_20260811.pdf")

CJK = "WQYMicroHei"
# The CID fonts reportlab ships are not embedded, so a viewer without a Chinese
# system font renders blank pages. This one is embedded, at the cost of ~5 MB.
_FONT = Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")
pdfmetrics.registerFont(TTFont(CJK, str(_FONT), subfontIndex=0))
# Only one weight exists, so <b> keeps the same face; emphasis is carried by
# colour instead (see _emph).
pdfmetrics.registerFontFamily(CJK, normal=CJK, bold=CJK, italic=CJK, boldItalic=CJK)


def _emph(text: str) -> str:
    return text.replace("<b>", "<font color='#0f3557'><b>").replace("</b>", "</b></font>")


INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#5f6b7a")
ACCENT = colors.HexColor("#1f4e79")
RULE = colors.HexColor("#c9d3de")
BAND = colors.HexColor("#eef3f8")

styles = getSampleStyleSheet()


def _s(name: str, size: float, leading: float, **kw) -> ParagraphStyle:
    return ParagraphStyle(
        name, parent=styles["Normal"], fontName=CJK, fontSize=size, leading=leading, **kw
    )


BODY = _s("body", 9.6, 15.4, textColor=INK, alignment=TA_LEFT, spaceAfter=5)
BULLET = _s("bullet", 9.6, 15.0, textColor=INK, leftIndent=13, bulletIndent=3, spaceAfter=3)
H1 = _s("h1", 16, 22, textColor=ACCENT, spaceBefore=20, spaceAfter=9)
H2 = _s("h2", 12, 17, textColor=ACCENT, spaceBefore=12, spaceAfter=5)
H3 = _s("h3", 10.4, 15, textColor=colors.HexColor("#2f4858"), spaceBefore=8, spaceAfter=3)
CAP = _s("cap", 7.8, 11, textColor=MUTED, spaceBefore=2, spaceAfter=10)
CELL = _s("cell", 8.2, 11.4, textColor=INK)
CELLH = _s("cellh", 8.2, 11.4, textColor=colors.white)
TITLE = _s("title", 24, 31, textColor=ACCENT, spaceAfter=4)
SUB = _s("sub", 11, 16, textColor=MUTED, spaceAfter=2)

story: list = []


def h1(t: str) -> None:
    story.append(Paragraph(_emph(t), H1))


def h2(t: str) -> None:
    story.append(Paragraph(_emph(t), H2))


def h3(t: str) -> None:
    story.append(Paragraph(_emph(t), H3))


def p(t: str) -> None:
    story.append(Paragraph(_emph(t), BODY))


def li(items: list[str]) -> None:
    for it in items:
        story.append(Paragraph(_emph(it), BULLET, bulletText="·"))
    story.append(Spacer(1, 4))


def table(rows: list[list[str]], widths: list[float], caption: str = "") -> None:
    data = [
        [Paragraph(_emph(c), CELLH if r == 0 else CELL) for c in row] for r, row in enumerate(rows)
    ]
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, ACCENT),
    ]
    for r in range(1, len(rows)):
        if r % 2 == 0:
            style.append(("BACKGROUND", (0, r), (-1, r), BAND))
    t.setStyle(TableStyle(style))
    block = [t]
    if caption:
        block.append(Paragraph(_emph(caption), CAP))
    story.append(KeepTogether(block))


def bars(
    title: str,
    labels: list[str],
    values: list[float],
    caption: str,
    ymax: float = 1.0,
    bar_colors: list[colors.Color] | None = None,
    width: float = 440,
    height: float = 150,
) -> None:
    d = Drawing(width, height + 28)
    d.add(String(0, height + 14, title, fontName=CJK, fontSize=9.6, fillColor=ACCENT))
    ch = VerticalBarChart()
    ch.x = 34
    ch.y = 20
    ch.width = width - 60
    ch.height = height - 22
    ch.data = [values]
    ch.categoryAxis.categoryNames = labels
    ch.categoryAxis.labels.fontName = CJK
    ch.categoryAxis.labels.fontSize = 8
    ch.categoryAxis.labels.dy = -4
    ch.valueAxis.valueMin = 0
    ch.valueAxis.valueMax = ymax
    ch.valueAxis.valueStep = ymax / 4
    ch.valueAxis.labels.fontName = CJK
    ch.valueAxis.labels.fontSize = 7.5
    ch.barWidth = 6
    ch.groupSpacing = 12
    ch.barSpacing = 2
    ch.barLabels.fontName = CJK
    ch.barLabels.fontSize = 8
    ch.barLabelFormat = "%0.3f"
    ch.barLabels.nudge = 8
    ch.barLabels.dy = 2
    palette = bar_colors or [ACCENT] * len(values)
    for i, c in enumerate(palette):
        ch.bars[(0, i)].fillColor = c
        ch.bars[(0, i)].strokeColor = None
    lab = Label()
    lab.setText("")
    d.add(ch)
    story.append(KeepTogether([d, Paragraph(_emph(caption), CAP)]))


def on_page(canv, doc) -> None:
    canv.saveState()
    canv.setFont(CJK, 7.6)
    canv.setFillColor(MUTED)
    canv.drawString(20 * mm, 12 * mm, "AdaMAS 对比实验汇总 · 2026-08-11")
    canv.drawRightString(A4[0] - 20 * mm, 12 * mm, f"第 {doc.page} 页")
    canv.setStrokeColor(RULE)
    canv.setLineWidth(0.4)
    canv.line(20 * mm, 15 * mm, A4[0] - 20 * mm, 15 * mm)
    canv.restoreState()


# ---------------------------------------------------------------- 封面

story.append(Spacer(1, 60))
story.append(Paragraph("对比实验汇总与数据集分析", TITLE))
story.append(Paragraph("拆里程碑有没有用 · 多智能体有没有用 · 里程碑内部做多方案挑选有没有用", SUB))
story.append(Spacer(1, 14))
p(
    "本报告把 2026-07-13 到 2026-08-11 之间跑过的几组对比实验放在一起讲清楚："
    "每组实验问的是什么问题、用了哪个数据集的多少道题、得到的数字是多少、"
    "以及这些数字能支持什么结论、不能支持什么结论。"
)
p(
    "所有数字都是从磁盘上的运行记录里抄出来的（每张表下面写了来源文件或实验编号），"
    "没有一个是估算的。凡是记录里被判为作废、被扣留、或者两次运行条件不一致的，"
    "报告里都会明确说出来，而不是取个平均糊过去。"
)
story.append(Spacer(1, 10))
table(
    [
        ["项", "内容"],
        [
            "数据来源",
            "EXPERIMENT_LOG.md, CHANGELOG.md, 以及 outpu"
            "ts/ 下各批次的 hidden_eval.json 与 summary.json",
        ],
        ["涉及数据集", "LiveCodeBench, BBEH, RealBench, CodeProjectEval"],
        [
            "涉及模型",
            "gpt-5.4（写代码仓库那几组的主力）, gpt-5.3-codex-spark（主力冷却期的备用）, "
            "gpt-5-mini（LiveCodeBench 那一批）",
        ],
        ["最新一次运行", "2026-08-11 00:20 UTC 结束（simpy / imapclient 的 test_first 切片）"],
    ],
    [70, 425],
)
story.append(PageBreak())

# ---------------------------------------------------------------- 名词

h1("一、先把词说清楚")
p(
    "下面这些词在报告里反复出现，先用大白话定义一遍。如果某个说法在这一节里没有出现过，"
    "那它在正文里也不会出现。"
)
table(
    [
        ["说法", "在这份报告里指什么"],
        [
            "一道题",
            "给智能体一份设计文档（需求、类图、架构说明），让它从空目"
            "录开始把一个 Python 库写出来。一道题就是一个仓库。",
        ],
        ["可见测试", "题目自带、允许智能体看到并反复运行的少量测试（CodeProjectEval 约 10 个）。"],
        [
            "隐藏测试",
            "打分用的那套测试，智能体全程看不到（CodeProjectEval 约 186 个，原项目自己的测试）。",
        ],
        [
            "通过率",
            "隐藏测试通过数 ÷ 隐藏测试总数。总数是提前固定写死在配置文件里的，不允许每次运行现算。",
        ],
        ["里程碑", "把一道题切成前后两段来做，例如先把核心接口定下来，再做上层功能。"],
        [
            "验收 / 门",
            "每段做完时跑一次检查：能不能编译、能不能 im"
            "port、文档要求的名字有没有、可见测试过不过。",
        ],
        ["定稿", "只有验收通过的那段代码才会被写进最终仓库；没通过就不写。"],
        [
            "候选",
            "同一段活儿的几个不同做法（比如把失败信息喂回去重做一次、多给一点预算、加一个修补角色）。",
        ],
        [
            "挑选规则",
            "几个候选做完后按顺序比：先看质量，质量一样看花钱，再一样看 token 用量，挑第一名定稿。",
        ],
        [
            "自写测试",
            "让一个智能体先把设计文档翻译成一份可执行的测试（放在 spec"
            "_tests/），后面的智能体照着它实现，我们也用它当质量尺子。",
        ],
    ],
    [62, 433],
)

h2("这份报告要回答的三个问题")
table(
    [
        ["问题", "怎么做的对比", "在哪一节"],
        [
            "多智能体比单智能体强吗",
            "同一批题：一个智能体直接写 / 一个智能体加测试反馈和一次修复 / 四个智能体分工",
            "第三节",
        ],
        [
            "把一道题切成多段验收有用吗",
            "同一份计划：切成两段各验收一次 vs 把两段合成一段最后验收一次",
            "第四节",
        ],
        ["段内做多方案挑选有用吗", "同一段活儿：只做一次 vs 做 2–3 个候选再挑一个", "第五节"],
    ],
    [110, 300, 85],
)
# ---------------------------------------------------------------- 数据集

h1("二、我们跑过的数据集")

table(
    [
        ["数据集", "题目总量", "我们实际用了", "给智能体什么", "最终怎么判分"],
        [
            "LiveCodeBench<br/>(release_v6)",
            "1055 道算法题",
            "dev 60 道（易/中/难各 20）。另有 60 道 heldout 已定义但从没跑过。",
            "题面 + 公开样例",
            "隐藏用例 pass@1",
        ],
        [
            "BBEH",
            "24 个子集，共 4980 道推理题",
            "3 道（只做过通路验证）",
            "题面 + 计算工具",
            "答案模糊匹配",
        ],
        [
            "RealBench<br/>(level2)",
            "17 个项目，其中 8 个环境合格",
            "固定 5 个：NodeFlow, SnoopR, xproj, emojichef, floquet",
            "TASK.md, REQUIREMENTS.md, public_desig"
            "n/ (类图与目录树), 外加一份空目录骨架，<b>没有任何可见测试</b>",
            "把原项目自己的测试覆盖上去跑",
        ],
        [
            "CodeProjectEval<br/>(python 子集)",
            "18 个仓库，环境能跑的 11 个",
            "真正跑过 6 个：bplustree, pyjwt, simpy, imapclient, voluptuous, deprecated",
            "PRD, 类图, 架构文档, 目录树, 外加约 10 个可见测试",
            "把约 186 个隐藏测试覆盖上去跑，分母写死",
        ],
    ],
    [72, 78, 128, 122, 95],
    "来源：configs/manifests/lcb_*.json；BBEH 数据根目录计数；"
    "realbench_codex_vanilla/selected_tasks/manifest.json；"
    "CodeProjectEval python-subset 目录 + outputs/cpe_env_probe2.json。",
)

h2("2.1 每个数据集的情况和它自带的麻烦")

h3("LiveCodeBench：一道题就是一个函数，没有拆段的空间")
li(
    [
        "60 道题跑过三种配置，是四个数据集里唯一有一批完整、可比、题量够的结果。",
        "但一道题只要写一个函数，几十行，不存在“先定接口再做上层”的结构，"
        "所以它能回答“多智能体有没有用”，回答不了“拆里程碑有没有用”。",
        "记录里注明：60 道 heldout 是留给最终定稿用的，至今没跑过；"
        "所以 LiveCodeBench 上的数字全部来自调参集，严格说都算“开发期数字”。",
    ]
)

h3("BBEH：只做过 3 道题的通路验证，不构成结果")
li(
    [
        "4980 道题里只跑过 3 道（3/3 全对），目的是验证“智能体能调用工具并交出答案”这条链路通了。",
        "它不涉及写代码仓库，跟拆里程碑这条主线没有关系，后面不再出现。",
    ]
)

h3("RealBench：题目结构最像真实项目，但没有可见测试，所以“验收”是我们自己发明的尺子")
li(
    [
        "17 个项目里 8 个合格（其余 9 个因为要连云"
        "服务、参考测试自己都不过、或者收不到测试而被排除），"
        "我们固定用其中 5 个。",
        "关键结构问题：题目只给类图和目录树，<b>不给任何可以运行的测试</b>。"
        "所以每段结束时能检查的只有“编译过不过、import 过不过、类图里写的名字导出了没有”。"
        "这等于是拿我们自己定义的标准去卡一个数据集本来没定义的东西。",
        "隐藏测试会 import 文档里从来没出现过的名字。已确认的例子"
        "：NodeFlow 的隐藏测试要 <b>IF</b> 这个名字，"
        "而类图里 __init__ 的导出列表是空的。这部分分数对任何系统都是拿不到的。",
        "单题波动大到没法比批次：同一份代码的两次运行，NodeFlow 一次 1.00 一次 0.00，"
        "差别只是它有一次把 Integer/Float/IF 从包根重新导出了、另一次没有。",
        "最干净的一批（rb-isolated5，5 道题各 1 次）：隐藏测试 micro 通过率 0.292、"
        "macro 0.182、完整通过的仓库 0/5。但这一批因为没走到规划器（落回了模板切分）"
        "而被判为<b>不能用来谈拆段效果</b>，只能证明工作区隔离修好了。",
    ]
)

h3("CodeProjectEval：目前唯一适合做拆段实验的数据集，但可用面很窄")
li(
    [
        "18 个仓库里只有 11 个环境能跑（参考实现在可见和隐藏两套测试上都全绿）。"
        "被排除的 7 个原因具体：cookiecutter 隐藏测试自己挂 2 个, "
        "djangorestframework-simplejwt 挂 12 个, flask 收集阶段报错, rsa 挂 1 个, "
        "trailscraper 缺依赖, xmnlp 装不上 onnxruntime, zxcvbn 一个测试都收不到。",
        "跑之前必须用 -o addopts= 把仓库自带的 pytest 配置屏蔽掉，"
        "否则它们会把覆盖率门槛、mypy、代码风格检查一起挂到 pytest 上，判的是风格不是行为。",
        "有些分数天生拿不到。按“文档里到底能不能推出这个名字”算出来的上限："
        "pyjwt 只有 0.41, parsel 只有 0.25, bplustree 0.85, imapclient 0.98。"
        "所以<b>绝对分数不能横向比不同仓库</b>，只能在同一个仓库里比不同做法。",
        "最早的一次端到端运行就撞上这个：bplustree 可见测试全过、代码定稿了 1132 行，"
        "隐藏测试 0.000，因为隐藏测试 import 了一个叫 ENDIAN 的常量，"
        "而这个名字在 PRD、两份类图和架构文档里出现次数是 0。",
    ]
)
# ---------------------------------------------------------------- 实验一

h1("三、实验一：单智能体 vs 单阶段多智能体")
p(
    "数据集是 LiveCodeBench 的 dev 集, 共 60 道题，三种配置各跑一遍，评分用官方隐藏用例。"
    "这是唯一一次题量够大的对比。"
)

table(
    [
        ["配置", "怎么做", "评到的题数", "隐藏通过率", "平均耗时", "修复触发 / 修复成功"],
        [
            "B0 一个智能体直接写",
            "看题就写，写完就交",
            "58 / 60",
            "0.776",
            "约 29 秒",
            "没有修复环节",
        ],
        [
            "B1 一个智能体 + 公开测试 + 最多修一次",
            "写完先跑公开样例，失败就改一次",
            "59 / 60",
            "<b>0.898</b>",
            "约 36 秒",
            "0.15 / 0.667",
        ],
        [
            "B2 四个智能体分工",
            "算法分析 + 边界分析 + 写代码 + 修复，各司其职",
            "60 / 60",
            "0.833",
            "约 67 秒",
            "0.133 / 0.375",
        ],
    ],
    [96, 118, 48, 52, 50, 78],
    "来源：EXP-20260713-04 / -05 / -06。三次的评到题数不同（58/59/60），"
    "差的那 1–2 道是没跑完的，不是做错的，所以三个数字不是在完全相同的题集上算的。",
)

bars(
    "LiveCodeBench dev 60 题：隐藏测试通过率",
    ["B0 单智能体", "B1 单智能体+测试+修一次", "B2 四智能体分工"],
    [0.7759, 0.8983, 0.8333],
    "柱子上的数字就是表里的通过率。",
    bar_colors=[colors.HexColor("#9bb7d4"), colors.HexColor("#1f4e79"), colors.HexColor("#6b8fae")],
)

h3("这批数据说明了什么")
li(
    [
        "<b>让智能体自己跑一遍测试再改一次，是这三者里最划算的一步</b>："
        "0.776 → 0.898，涨了 12 个百分点，代价只是多花 7 秒。",
        "<b>加人分工反而掉了</b>：四个智能体 0.833，比一个智能体加测试反馈还低 6.5 个百分点，"
        "而耗时和花费是它的 2–4 倍。",
        "更具体的失败原因记在案：B2 交出去之前公开测试通过率其实很高（0.906），"
        "但一旦要修就修不动（修复成功率 0.375，B1 是 0.667）。"
        "也就是说四个智能体串起来生成了一版“看起来对”的代码，出了问题却没人能定位。",
        "这批结果正是后来做“拆里程碑 + 段内挑选”的动机："
        "单纯把智能体排成一条流水线不涨分，得给它中途验收和回退的机会。",
    ]
)
p(
    "读的时候要注意：这批是 2026-07-13 的老实现，用的模型是 gpt-5-mini，"
    "而后面写代码仓库那几组用的是 gpt-5.4 和 gpt-5.3-codex-spark。"
    "所以不要把这里的 0.898 和后面 CodeProjectEval 的任何数字放在一起比。"
)
# ---------------------------------------------------------------- 实验二

h1("四、实验二：一段做完 vs 切成两段各验收一次")

h2("4.1 怎么保证公平")
li(
    [
        "问的是“中途验收有没有用”，不是“多花钱有没有用”，"
        "所以对照组不是随便配的：<b>先让规划器给出两段的计划，再把这份计划合并成一段</b>，"
        "同样的智能体、同样的顺序、同样的预算，唯一区别是中间有没有一次验收和一次定稿。",
        "两边都重放同一份冻结的计划，所以“规划器这次心情如何”不进入比较。",
        "每个仓库 3 次重复；重复上限被硬性设为 5 次，驱动脚本拒绝更大的数字。",
    ]
)

h2("4.2 结果")
table(
    [
        ["仓库", "一段（对照）", "两段", "实际用掉的智能体轮次<br/>一段 / 两段", "差值"],
        ["simpy", "0.805（3 次）", "0.732（3 次）", "5 / 4", "−0.074"],
        ["bplustree", "0.034（2 次）", "0.307（2 次）", "5 / 4.5", "+0.274"],
        ["pyjwt", "0.510（3 次）", "0.771（3 次）", "4 / 2", "+0.261"],
    ],
    [70, 100, 100, 130, 60],
    "来源：EXP-20260809-04 与 outputs/cpe_ab/ab_summary_role_pool.json。"
    "bplustree 两边各有 1 次因为生成的代码里有死循环、跑隐藏测试时超时，所以只剩 2 次。",
)

bars(
    "CodeProjectEval：一段 vs 两段（隐藏测试通过率）",
    ["simpy 一段", "simpy 两段", "bplustree 一段", "bplustree 两段", "pyjwt 一段", "pyjwt 两段"],
    [0.805, 0.732, 0.034, 0.307, 0.510, 0.771],
    "灰蓝 = 一段做完，深蓝 = 切成两段。",
    bar_colors=[
        colors.HexColor("#9bb7d4"),
        colors.HexColor("#1f4e79"),
        colors.HexColor("#9bb7d4"),
        colors.HexColor("#1f4e79"),
        colors.HexColor("#9bb7d4"),
        colors.HexColor("#1f4e79"),
    ],
)

h3("这两个正的差值到底是什么带来的")
p(
    "不是代码写得更好，而是<b>少了几次归零</b>。pyjwt 一段那三次的单次成绩是 "
    "0.000 / 0.769 / 0.762，两段是 0.741 / 0.827 / 0.745。把那个 0 去掉，两边基本打平。"
)
p(
    "那个 0 就是全部机制：四个智能体都跑完了，最后唯一的一次验收没过；"
    "而规则是“没过就不写进最终仓库”，于是那次运行的仓库里 jwt/ 目录一行代码都没有。"
    "另外两次一段的运行各定稿了约 1877 行。九次一段的运行里，有两次什么都没定稿、一次超时。"
)
p(
    "切成两段的好处是<b>后半段失败不会把前半段一起抹掉</b>。"
    "换句话说：<b>拆段买的是下限，不是上限</b>——凡是一段那边能定稿成功的场合，"
    "它的分数不低于甚至高于两段（simpy 0.805 对 0.732）。"
)

h3("顺带发现：两段还更便宜")
table(
    [
        ["pyjwt，5 次重复", "隐藏通过率", "每次花费", "每次耗时", "验收通过次数"],
        ["一段", "0.595 ± 0.334", "$2.66", "14.4 分钟", "4 次（5 次运行）"],
        ["两段", "<b>0.744 ± 0.039</b>", "<b>$0.93</b>", "10.6 分钟", "10 次（5 次运行 × 2 段）"],
    ],
    [110, 110, 80, 90, 100],
    "来源：outputs/cpe_ab/trials-parallel-n5.jsonl。",
)
li(
    [
        "便宜的原因是两段那边可以<b>提前收工</b>：中间那次探测通过了，"
        "配在后面的“修补”智能体就根本不会被创建。"
        "12 次带门的段里，探测通过了 10 次，修补角色 10 次都没启动。",
        "token 上更夸张：pyjwt 两段用了 89.9 万 token，一段用了 365 万，"
        "只有 <b>24.7%</b>，而分数还更高。差距比轮次比例还大，"
        "因为一条流水线上每多一环，就要把前面攒下来的上下文再读一遍。",
    ]
)

h3("另外还跑了“一个智能体干全部”作为地板线")
table(
    [
        ["仓库", "一个智能体干全部", "两段", "备注"],
        ["pyjwt", "0.740 ± 0.039（3 次）", "0.750 ± 0.040", "两者打平"],
        ["simpy", "0.723 ± 0.066（3 次）", "0.757 ± 0.054", "两者打平"],
    ],
    [70, 150, 120, 155],
    "来源：CHANGELOG 2026-08-10 用固定分母重算后的汇总表。"
    "这一列和上面 4.2 那张表不是同一批运行，不要交叉相减。",
)
p(
    "这条地板线很值得注意：<b>在这两个仓库上，一个智能体单干的成绩和整套多智能体分段流程差不多</b>。"
    "多出来的那套结构，目前买到的是“失败时不至于归零”，还不是“成功时分数更高”。"
)

h3("这批实验的弱点（必须一起报）")
li(
    [
        "每个格子只有 2–3 次重复，而组内波动比组间差值还大（pyjwt 一段的标准差 0.334）。"
        "所以上面那两个正差值只能读成“和下限机制的说法一致”，<b>不能当成测出来的效果大小</b>。",
        "所谓“预算对齐”只对齐了智能体数量和步数上限。"
        "配置里写的 max_tokens 在 Codex 后端下根本不生效——74 个节点里有 35 个超过了它。"
        "真正起作用的只有超时时间和步数。",
        "更早一批 18 次运行已经作废：其中 17 次用的是旧版提示词构造器，"
        "1 次用的是新版，混在一起平均过。作废的原因写在 EXP-20260809-03。",
    ]
)
# ---------------------------------------------------------------- 实验三

h1("五、实验三：段内做多方案挑选 vs 不挑选")

h2("5.1 这里说的“挑选”是什么")
p(
    "一段活儿做完、验收没过的时候，系统不是直接失败，而是就这一段再生成 2–3 个不同的做法："
    "把失败信息喂回去重做、多给一点步数和时间、或者在流程里插一个专门修补的角色。"
    "几个做法各自跑完各自验收，然后按“先比质量、再比花钱、再比 token”挑一个定稿。"
)

h2("5.2 唯一一次真实模型下的开关对比：imapclient")
table(
    [
        ["配置", "隐藏通过率", "段验收通过", "每次花费", "每次耗时"],
        ["挑选打开（每段最多 2 个候选）", "<b>0.163 ± 0.082</b>", "10 / 10", "$5.08", "40 分钟"],
        [
            "挑选关闭",
            "<b>0.000 ± 0.000</b>",
            "0 / 10（5 次运行全部死在第一段）",
            "$1.90",
            "13 分钟",
        ],
    ],
    [150, 90, 145, 55, 55],
    "来源：EXP-20260810-01，outputs/cpe_tuning/{on,off}_trials.jsonl。"
    "同一份冻结计划、同一批预算、两组同时启动，n=5。",
)
bars(
    "imapclient：段内挑选开 / 关（隐藏测试通过率，n=5）",
    ["挑选关闭", "挑选打开"],
    [0.000, 0.163],
    "关闭那一组不是“分低”，是 5 次运行全部卡在第一段、第二段根本没开始。",
    ymax=0.4,
    bar_colors=[colors.HexColor("#b9c6d4"), colors.HexColor("#1f4e79")],
)
li(
    [
        "imapclient 第一段的验收<b>每次都失败</b>"
        "。不开挑选，运行就停在这里，整个仓库 0 分，五次全部如此。"
        "开了挑选，两个候选每次都把它修好，10 段全部定稿。代价是 2.7 倍的钱和 3 倍的时间。",
        "<b>选题比调参重要得多。</b>原本准备在 pyjwt 上做这个实验，"
        "但那边的验收 15 次里有 14 次报“通过、满分”，"
        "而同一批代码的隐藏测试还有四分之一是错的——它的可见测试只有 10 个，隐藏测试有 290 个。"
        "在一个几乎不会失败的门上做挑选实验，等于买彩票。"
        "先用三次单跑（deprecated $0.21, voluptuous $1.34, imapclient $5.33）"
        "找到一个门会稳定失败的仓库，才开始正式跑。",
    ]
)

h2("5.3 一个必须说清楚的事：早期那些“帕累托搜索”实验没有真实模型的质量结论")
p(
    "记录里 2026-07-20 到 07-23 有几条关于多目标搜索的条目（M6、M6.1、Stage-2）。"
    "它们全部是<b>用假数据和固定桩跑通的工程验收</b>，状态写的就是 smoke / mock，"
    "条目里直接写着“不要用它声称真实模型上的质量提升”。"
    "那几次证明的是“这条代码路径能走通、报告可复现、隐藏标签不会泄漏到选择里”，"
    "不是“搜索能提分”。"
)
p(
    "所以到今天为止，<b>“段内挑选有没有用”只有 imapclient 这一组真实数据</b>："
    "在一个门必然失败的仓库上，它把 0 变成了 0.163。"
)

h2("5.4 挑选机制本身当时是坏的：质量这一轴分不出高低")
li(
    [
        "那 10 个候选的质量分全是 1.0（验收就是 0/1 的过与不过，候选都过了），"
        "于是排序自动落到最后一档“谁 token 少”。"
        "第 4 次运行的两个候选差了 1837 个 token —— 占 207 万的 0.09%。"
        "也就是说<b>那次其实是在抛硬币</b>，最终成绩在 0.094 到 0.315 之间飘也符合这个解释。",
        "改法是给质量找一把更细的尺子：让一个智能体先把设计文"
        "档翻成一份可执行测试（放在 spec_tests/），"
        "后面的智能体照着它实现，候选之间按这份测试的通过条数排序。"
        "同一次搜索里所有候选共用同一份测试，所以事后还能把“所有候选都过”和“所有候选都不过”的"
        "那些恒定测试剔除掉，只按真正有差别的测试排序。",
        "第一次真跑就撞上一个反直觉的问题：<b>写代码的智能体把这份测试删了</b>。"
        "因为它的提示词里有一条“只交文档里写到的文件”，而 spec_tests/ 这个目录任何文档都没提。"
        "已修：现在明确告诉它这个目录是那条规则的例外、不许改不许删。",
    ]
)

h2("5.5 昨夜最新一次运行（2026-08-11 00:20 UTC 结束）")
table(
    [
        ["仓库", "花费", "耗时", "段数 / 定稿", "隐藏测试原始计数", "本次是否触发挑选"],
        ["imapclient", "$4.81", "22.3 分钟", "2 / 2", "过 68、错 185、报错 2（共 267）", "没有"],
        ["simpy", "$16.71", "66.1 分钟", "2 / 2", "过 72、错 77（共 149）", "第二段触发，3 个候选"],
    ],
    [66, 48, 58, 60, 148, 115],
    "来源：outputs/cpe_tuning_multi/slice_imapclient_simpy.jsonl 及两个批次的 hidden_eval.json。"
    "两次都用 gpt-5.3-codex-spark（主力模型在冷却期"
    "），因此<b>不能和前面任何用 gpt-5.4 的数字比较</b>。",
)
li(
    [
        "两次的通过率都<b>被规则扣留了</b>，没有发布成分数：算下来分别是 68/267 和 72/149，"
        "但因为各有 1 个隐藏测试文件不在固定分母表里、只能靠静态数数估出来，"
        "规则要求“分母有疑问就不发分数”，所以字段是空的。这是个小配置缺口，不是运行失败。",
        "simpy 第二段的验收确实失败了一次（可见测试里 test_"
        "store_sim 断言事件数是 191，实现跑出 188），"
        "于是启动了挑选：三个候选（喂回失败信息、加修补角色、加预算）都跑完，"
        "最后定稿的是“加预算”那个。整轮搜索花了 $8.23，被选中那条本身占 $4.40。",
        "这里有一条新发现值得记下来：<b>门会不会失败取决于模型，不是仓库固有属性</b>。"
        "用 gpt-5.4 时 simpy 的门每次都过（所以它一直被当成“不能用来做挑选实验”的仓库），"
        "换成弱一点的 spark 它就会失败。这意味着“哪些题适合研究段内挑选”这个结论要绑定模型来讲。",
    ]
)
# ---------------------------------------------------------------- 适不适合

h1("六、数据集适不适合做“多段验收”这类实验")
p("这是这几周花掉时间最多、也最值得单独讲的一节。结论先放表里。")

table(
    [
        ["仓库", "规划器会切几段", "第一次验收会失败吗", "文档能撑起的分数上限", "适合做什么"],
        ["imapclient", "2 段（稳定）", "<b>每次都失败</b>", "0.98", "唯一适合研究段内挑选的仓库"],
        [
            "pyjwt",
            "2 段（稳定）",
            "几乎不失败（15 次里 14 次报满分）",
            "0.41",
            "适合比一段/两段，不适合研究挑选",
        ],
        ["simpy", "2 段（稳定）", "gpt-5.4 下不失败；spark 下会失败", "1.00", "适合比一段/两段"],
        [
            "bplustree",
            "2 段（不稳定，3 次采样里 2、2、1）",
            "偶尔失败",
            "0.85",
            "能用，但容易超时、方差大",
        ],
        [
            "voluptuous / tinydb",
            "只切 1 段（4 次采样全是 1 段）",
            "—",
            "1.00",
            "只能当“不该拆”的对照",
        ],
        ["deprecated", "只切 1 段", "—", "1.00", "只能当便宜的探针"],
    ],
    [76, 110, 110, 78, 121],
    "来源：EXP-20260810-04、EXP-20260808-02/-04、outputs/cpe_planner_probe.json、outputs/cpe_ceiling.json。",
)

h3("由此得到的几条硬结论")
li(
    [
        "<b>“能切成两段”远远不等于“能用来做段内挑选实验”。</b>"
        "18 个仓库里只有 4 个稳定切两段；这 4 个里，在主力模型下只有 imapclient 的门会稳定失败。"
        "段内挑选只在门失败时才启动，所以样本量实际上是 1 个仓库。",
        "<b>门太松是个真问题。</b>pyjwt 的门用 10 个可见测试去卡 290 个隐藏测试，"
        "结果它对着一份四分之一是错的代码报“满分”。"
        "在这种门上做任何调优都测不出东西，这也是为什么要另外造一把更细的尺子。",
        "<b>RealBench 结构上更像真实项目，却更不适合现在这套实验。</b>"
        "它没有可见测试，验收只能查编译和导出；隐藏测试又引用文档里没有的名字；"
        "单题方差大到同一份代码能从 1.00 掉到 0.00。"
        "在这种噪声下，5 道题跑 1 次的批次根本分不开 0.29 和 0.40。",
        "<b>LiveCodeBench 和 BBEH 不参与这条线。</b>前者一题一个函数，没有段可拆；后者不写代码。",
    ]
)
# ---------------------------------------------------------------- 坑

h1("七、我们在测量上踩过的坑，以及现在定下的规矩")
p(
    "这一节比结果本身重要：下面每一条都曾经制造过一个不存在的结论，"
    "而且每一条都只对某一组有利或有害，不会互相抵消。"
)

table(
    [
        ["坑", "具体表现", "现在的规矩"],
        [
            "分母是算出来的，不是钉死的",
            "隐藏测试总数用 “def test_” 数出来，可 pytest 的参数化会把 1 个函数展开成几十个用例。"
            "bplustree 数出来 59 个，实际收集到 356 个，于是通过率报成了 2.54。"
            "pyjwt 有 25 次运行里的 23 次公布了大于 1 的比率，两个月没人被拦住。",
            "总数钉死在 configs/codeprojecteval_suite_sizes.json；"
            "只要有任何一个文件的数是猜的，就<b>直接扣留分数</b>，只报原始计数。",
        ],
        [
            "超时被当成 0 分",
            "隐藏测试跑到墙上时间的运行被按 0.000 平均进去，凭空造出 0.313 的效果。",
            "“没测到”和“测出来是 0 分”分开报；没测完的运行不进平均。",
        ],
        [
            "两组用了不同的提示词构造器",
            "18 次运行里 17 次是旧版、1 次是新版，被当成同一个条件平均。",
            "每次运行记录自己的构造器；汇总脚本拒绝平均混了两种的组。",
        ],
        [
            "对照组被偷偷削弱",
            "“每段最多几个智能体”这个上限本来只约束规划器，"
            "却也被套在重放冻结计划上；而合并成一段的对照组正好把所有智能体塞进一段，"
            "于是 bplustree 的对照组少跑了 1 个智能体。",
            "重放冻结计划时不再套这个上限；那批对照组作废重跑。",
        ],
        [
            "验收门本身没有梯度",
            "门是过/不过，候选都过了，质量这一轴就是常数，排序自动落到 token，"
            "等于按噪声挑（两个候选差 0.09%）。",
            "验收改成按阶段给分；再加一份自写测试当细尺子；"
            "同一次搜索里剔除所有候选表现一致的测试，只按有差别的部分排序。",
        ],
        [
            "重复次数不受控",
            "驱动脚本以前接受 --repeats 10，很容易一不小心把预算花光。",
            "上限硬性设为 5，更大的数字直接拒绝。",
        ],
        [
            "落回模板切分却当成拆段结果",
            "有几批 RealBench 运行其实没走到规划器，用的是按目录形状的机械切分。",
            "没有 milestone_plan_draft.json 的运行判为作废，从所有比较里剔除。",
        ],
    ],
    [90, 245, 160],
)
# ---------------------------------------------------------------- 结论

h1("八、总结与建议")

h2("8.1 到今天为止，哪些说法是站得住的")
table(
    [
        ["说法", "证据强度", "依据"],
        [
            "让智能体自己跑测试再修一次，是性价比最高的一步（+12 个百分点）",
            "较强（60 道题）",
            "LiveCodeBench dev，EXP-20260713-04/-05",
        ],
        [
            "单纯把智能体排成流水线不涨分，还更贵更慢",
            "较强（60 道题）",
            "同上 + EXP-20260713-06",
        ],
        [
            "把一道题切成两段各验收一次，主要作用是防止整次运行归零，同时更省钱省 token",
            "机制清楚，效果大小没测准（每格 2–3 次）",
            "EXP-20260809-04、trials-parallel-n5.jsonl",
        ],
        [
            "在验收必然失败的仓库上，段内多候选挑选把 0 变成 0.163",
            "单仓库，n=5",
            "EXP-20260810-01",
        ],
        [
            "早期的多目标搜索实验不能作为质量结论",
            "确定（条目自己写明是假数据）",
            "EXP-20260720-03、EXP-20260723-02",
        ],
        [
            "一个智能体单干在 pyjwt / simpy 上和整套流程打平",
            "各 3 次，只是地板线",
            "CHANGELOG 2026-08-10 重算表",
        ],
    ],
    [200, 110, 185],
)

h2("8.2 现在卡在哪")
li(
    [
        "<b>昨夜两次运行的分数被扣留</b>：imapclient 68/267、simpy 72/149 都算得出来，"
        "但各有 1 个隐藏测试文件不在固定分母表里。补上这一个格子就能出分，属于半小时的活。",
        "<b>主力模型在冷却期</b>，昨夜用的是弱模型。弱模型的数字自成一套，"
        "不能和之前 gpt-5.4 的表格混读；冷却解除后需要用主力模型重跑一遍才能进正式比较。",
        "<b>能做段内挑选实验的仓库太少</b>：主力模型下只有 imapclient 的门会稳定失败。"
        "要么继续用自写测试当尺子、让“门过了但分数低”也能触发挑选，"
        "要么找更多门会失败的仓库。",
        "<b>自写测试和独立评分之间有张力</b>：让写代码的智能体照着可见的测试实现，"
        "它当然会把这些测试跑通（昨夜 imapclient 38/38、simpy 29/29 和 51/51 全过），"
        "这样这把尺子又饱和了。需要考虑让写测试的智能体和实现的智能体信息不对称。",
    ]
)

h2("8.3 建议的下一步顺序")
li(
    [
        "先把固定分母表补齐（不花钱），让昨夜两次运行能出分。",
        "然后决定自写测试这把尺子怎么保持不饱和——这是段内挑选能不能继续做下去的前提。",
        "冷却解除后，用主力模型在 imapclient, pyjwt, simpy 上各跑 3 次带挑选的运行，"
        "这样才有第一批多仓库的段内挑选数据。",
        "RealBench 这条线在没有可见测试的问题解决前先不加题；现在加题只会增加噪声，不会增加结论。",
    ]
)

story.append(Spacer(1, 10))
p(
    "<font color='#5f6b7a'>报告生成脚本：scripts/make_experiment_review_pdf.py。"
    "每张表下面的来源行都指向仓库里可以直接打开核对的文件。</font>"
)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(OUT),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
        title="AdaMAS 对比实验汇总与数据集分析",
        author="AdaMAS",
    )
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
