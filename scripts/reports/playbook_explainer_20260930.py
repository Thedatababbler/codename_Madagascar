# -*- coding: utf-8 -*-
"""剧本表（playbook）到底是什么：给非代码读者的 3 页说明（milestones 分支，2026-09-30）.

    .venv/bin/python scripts/reports/playbook_explainer_20260930.py docs/reports/adamas-playbook-explainer-20260930.pdf
"""
import sys

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

pdfmetrics.registerFont(TTFont("CJK", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", subfontIndex=0))

INK = colors.HexColor("#1A1A2E")
MUTED = colors.HexColor("#4A5568")
ACCENT = colors.HexColor("#1F497D")
RULE = colors.HexColor("#D5DAE3")
BAND = colors.HexColor("#EEF1F6")
BOX = colors.HexColor("#E8EEF7")
BOX2 = colors.HexColor("#FBEFE6")
BOX3 = colors.HexColor("#E9F4EC")
GOLD = colors.HexColor("#C9A84C")

title = ParagraphStyle("t", fontName="CJK", fontSize=18, leading=24, textColor=INK, wordWrap="CJK")
sub = ParagraphStyle("s", fontName="CJK", fontSize=9.2, leading=13, textColor=MUTED, wordWrap="CJK")
h1 = ParagraphStyle("h1", fontName="CJK", fontSize=13.5, leading=18, textColor=ACCENT, spaceBefore=11, spaceAfter=4)
body = ParagraphStyle("b", fontName="CJK", fontSize=10.2, leading=15.5, textColor=INK, wordWrap="CJK", spaceAfter=4)
bullet = ParagraphStyle("bl", parent=body, leftIndent=11, bulletIndent=0, spaceAfter=2)
cell = ParagraphStyle("c", fontName="CJK", fontSize=8.8, leading=11.6, textColor=INK, wordWrap="CJK")
cellb = ParagraphStyle("cb", parent=cell, textColor=colors.white)
note = ParagraphStyle("n", fontName="CJK", fontSize=8.4, leading=12, textColor=MUTED, wordWrap="CJK", spaceBefore=2)
quote = ParagraphStyle("q", parent=body, leftIndent=8, rightIndent=8, backColor=BOX3, borderPadding=6, spaceBefore=4, spaceAfter=8)


def P(t, st=body):
    return Paragraph(t, st)


def bl(t):
    return Paragraph(t, bullet, bulletText="•")


def table(rows, widths, size=8.8):
    data = [[Paragraph(str(c), cellb if i == 0 else cell) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "CJK"), ("FONTSIZE", (0, 0), (-1, -1), size),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BOX", (0, 0), (-1, -1), 0.5, RULE), ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
    ]))
    return t


def _box(d, x, y, w, h, text, fill=BOX, size=7.6):
    d.add(Rect(x, y, w, h, fillColor=fill, strokeColor=ACCENT, strokeWidth=0.6, rx=2, ry=2))
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        yy = y + h / 2 + (len(lines) - 1) * size * 0.65 - i * size * 1.3
        d.add(String(x + w / 2, yy - size * 0.35, ln, fontName="CJK", fontSize=size, fillColor=INK, textAnchor="middle"))


def _arrow(d, x1, y1, x2, y2, color=ACCENT):
    import math
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=0.8))
    a = math.atan2(y2 - y1, x2 - x1)
    L = 4
    d.add(Polygon([x2, y2, x2 - L * math.cos(a - 0.5), y2 - L * math.sin(a - 0.5),
                   x2 - L * math.cos(a + 0.5), y2 - L * math.sin(a + 0.5)], fillColor=color, strokeColor=color, strokeWidth=0.3))


def fig_flow():
    W, H = 178 * mm, 58 * mm
    d = Drawing(W, H)
    d.add(String(2 * mm, H - 6 * mm, "图 1  验收门失败后，剧本表在哪一步被用到", fontName="CJK", fontSize=8.5, fillColor=MUTED))
    y = H - 24 * mm
    bw, bh = 31 * mm, 15 * mm
    xs = [2 * mm, 37 * mm, 72 * mm, 107 * mm, 142 * mm]
    labels = ["验收门失败\n(或门过了但内部分 < 0.9)",
              "诊断\n症状是哪一类？\n哪个节点该负责？\n推荐换成什么角色？",
              "查剧本表\n用 (症状类别, 当前模板)\n取出适用的几行",
              "按顺序把每一行\n变成一个候选\n(最多 3 个)",
              "跑候选、打分\n不低于现任才提交\n否则保留现任"]
    fills = [BOX2, BOX, BOX3, BOX, BOX]
    for x, t, f in zip(xs, labels, fills):
        _box(d, x, y, bw, bh, t, fill=f, size=7.0)
    for a, b in zip(xs, xs[1:]):
        _arrow(d, a + bw, y + bh / 2, b, y + bh / 2)
    d.add(String(2 * mm, y - 9 * mm, "现在默认先跑 2 个“探针”（原设计原样再跑两次）判断失败是每次都出现还是偶尔出现，再查表。剧本表本身不变，只是查哪张表、哪一行由探针结果决定。",
                 fontName="CJK", fontSize=7.4, fillColor=MUTED))
    d.add(String(2 * mm, y - 15 * mm, "LLM 只在第 2 步出现，而且只回答三个填空题；第 3、4 步是纯规则，不调用模型。",
                 fontName="CJK", fontSize=7.4, fillColor=MUTED))
    return d


def build(out):
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=17 * mm, rightMargin=17 * mm, topMargin=14 * mm, bottomMargin=13 * mm,
                            title="AdaMAS 剧本表：一个简单的解释", author="AdaMAS")
    W = A4[0] - 34 * mm
    s = [P("AdaMAS 的“剧本表”到底是什么", title),
         P("给不看代码的读者 · milestones 分支 · 2026-09-30", sub), Spacer(1, 3 * mm)]

    s += [P("1. 一句话", h1),
          P("<b>剧本表是一张“出了什么毛病、就试哪种改法”的对照表。</b>一个里程碑没过验收门时，系统不是让大模型自由发挥想一个新方案，"
            "而是先判断毛病属于哪一类，再到这张表里按类别取出几条事先写好的改法，逐条试。表里每一条叫“一行”，我之前在报告里写的“行”就是它。"),
          P("打个比方：修车厂的工单。车开不动（门失败），师傅先看是没油、点火坏了还是变速箱坏了（诊断），然后翻检修手册里对应型号和故障的那一页（查表），"
            "按手册上的第一条、第二条办法依次试（生成候选），修好了就交车（提交），没修好就把车按原样还回去（保留现任）。手册是固定的，师傅只负责判断故障和填零件型号。", quote)]

    s += [P("2. 这张表长什么样", h1),
          P("表有两个“查找键”和一组“内容”："),
          bl("<b>查找键一：毛病类别</b>，只有三种。<b>没做完</b>（超时、步数用光、代码里留着 TODO）；<b>做完了但行为不对</b>（能跑，测试大量失败）；"
             "<b>结构性问题</b>（失败散落在不相关的地方，或者同一种改法试过两次都没动静）。"),
          bl("<b>查找键二：这个里程碑当前用的模板</b>。模板就是里程碑内部“几个人、什么顺序”的固定形状，比如 test_first = 出题者 → 实现者 → 修复者。"),
          bl("<b>内容：一个改法</b>。改法只有四种成分：换成哪个模板形状；哪个位置换成什么角色；把失败清单交给谁；给谁加多少步数和时间。"
             "行里没提到的位置一律保持原样，所以一行剧本不会偷偷把规划器选好的角色降级。"),
          P("下面是把 26 行全部翻译成人话后的样子（按当前模板分组）："),
          table([["当前模板", "毛病类别", "表里的改法，按尝试顺序"],
                 ["任何模板", "行为不对", "把失败用例的名字塞进出问题那个人的提示词里，再跑一次"],
                 ["任何模板", "没做完", "给那个人加一点步数和时间；或加很多，并叮嘱“先做出能跑的最小版本”"],
                 ["test_first\n(出题者→实现者→修复者)", "行为不对", "① 失败清单交给修复者 → ② 在修复者前面加一个只读的评论者，先读清单再修 → ③ 修两轮"],
                 ["test_first", "没做完", "给实现者加预算"],
                 ["gate_then_repair", "行为不对 / 没做完", "同 test_first 的第①条 / 给实现者加预算"],
                 ["review_then_fix\n(出题者→实现者→审阅者→修复者)", "行为不对", "换一个审阅角度；或改成两个审阅者并行；没做完时去掉审阅者把预算给修复者"],
                 ["chain\n(出题者→合约→实现→接线)", "行为不对", "把空着的第三个位置填上诊断推荐的专家；或在两个写代码的人之间插一个只读的人"],
                 ["solo（只用于回放旧计划）", "行为不对", "从一个人改成带修复者 / 带审阅者 / 三人链"],
                 ["带出题者的所有模板", "门过了但分数低", "<b>续修候选</b>：一个人在现任的成果上继续，只拿到“每次都失败”的清单和证据；<b>节点重采样</b>：只把出问题的那个人重跑 N 次，其它人保持不动；失败清单塞给出问题的人"],
                 ["test_first 系", "门过了但分数低", "在通过的实现者后面加一个“改进者”；再在改进者前面加评论者"]],
                [36 * mm, 26 * mm, W - 62 * mm]),
          P("最后两组来自“质量表”：门已经通过、但内部得分低于 0.9 时用的另一张表。它和失败表分开，是因为失败表里的“修复者”只在门失败时才运行，放进质量搜索会编译、运行、然后什么也不做。", note)]

    s += [PageBreak(), P("3. 一次完整的例子", h1),
          P("假设 tinydb 的第 2 个里程碑用 test_first 模板跑完，验收门结果是 30 条用例失败 12 条。接下来发生的事："),
          bl("<b>探针。</b>同一设计原样再跑两次。发现 12 条失败里有 9 条三次都失败（<b>持续失败</b>），3 条时有时无（<b>偶发失败</b>）。"),
          bl("<b>诊断。</b>规则先看：9 条持续失败是不是全在同一类（比如全是导入错误）？不是，就问模型三个填空题：毛病类别？（答：行为不对）哪个节点该负责？（答：实现者）"
             "推荐哪个角色去修、哪个角色去读？（答：gate_repairer 修，behaviour_critic 读）。模型答的角色名必须在角色池里，否则作废；置信度低于门槛就退回查表默认值。"),
          bl("<b>查表。</b>持续失败集非空 → 质量表里“续修候选”这一行适用：一个修复者在现任工作区上继续，提示词里只有那 9 条持续失败的名字和它们的源码与输出。"
             "偶发失败只有 3 条、不集中在一个节点 → “节点重采样”那一行的前置条件不满足，跳过。"),
          bl("<b>生成候选。</b>预算是 3 个：2 个已经被探针用掉，剩 1 个给续修候选。（这就是我之前说“重采样常被续修候选挤掉”的原因：一个里程碑只剩 1 个名额。）"),
          bl("<b>跑与选。</b>续修候选跑完，9 条持续失败修好 7 条，没有新失败；分数高于现任且过了门 → 提交。若它比现任低，保留现任，什么都不改。"),
          P("整个过程模型只被问了一次填空题；表、顺序、预算、提交规则全是固定的。换一道题、换一个模型，走的是同一张表，所以可以按行统计“这一行到底修好了多少条持续失败”。")]

    s += [P("4. 为什么要用表，而不是让模型直接想办法", h1),
          bl("<b>可枚举。</b>候选只能是表里的行，加上诊断填进去的角色和证据。搜索空间小，每个候选都能说清“为什么试它”。"),
          bl("<b>可回放、可统计。</b>每个候选记录用了哪一行、修好了哪些失败、有没有回归。攒够次数就能看出哪一行值得留、哪一行该删。"
             "已经删掉一行：把失败清单塞给 test_first 的实现者重做，三次对照全输给不改任何东西的原设计，因为它等于让实现者带着几个测试名从零再来，修不了任何东西。"),
          bl("<b>模型不擅长这个决定。</b>早期实验里（EXP-20260831-01）让表里的改拓扑行放开跑，没有一行赢过原设计原样重跑；收益全部能用“多跑几次挑最好”解释。"
             "于是把预算改成先花在“这个失败是不是每次都出现”上，只对持续失败做定向修复，这才有了 6/8 修好、0 回归的记录。")]

    s += [P("5. 图示", h1), fig_flow()]

    s += [P("6. 几个容易混淆的词", h1),
          table([["词", "意思"],
                 ["剧本 / 剧本表 / 行", "同一个东西：改法对照表和表里的一条"],
                 ["锚", "候选列表的第 0 个，永远是“原设计不改，只把失败清单加进提示词”再跑一次。它是所有候选的对照组"],
                 ["探针", "原设计原样再跑几次，用来区分“每次都失败”和“偶尔失败”，本身不是改法"],
                 ["持续失败 / 偶发失败", "所有探针里都失败 / 只在部分探针里失败"],
                 ["续修候选", "质量表里的一行：一个修复者在现任成果上继续，只看持续失败清单"],
                 ["节点重采样", "质量表里的一行：固定其它人，只把出问题的那个人重跑 N 次，样本一致就提前停"],
                 ["重出题", "套件一个用例都收集不到或全零时，先让出题者重写套件，不算剧本表的行，但走同一条选择规则"],
                 ["现任", "这个里程碑目前已经跑出来的最好结果；候选只有不低于它才会替换它"],
                 ["模板 / 槽位 / 角色", "模板 = 几个人和顺序；槽位 = 模板里的一个位置；角色 = 填进位置的人（出题者、实现者、修复者、评论者等 12 种）"]],
                [36 * mm, W - 36 * mm]),
          P("代码位置：configs/subgraph_templates/（模板）、configs/roles/（角色）、src/orchestra/control/fast_loop/playbooks.py（两张表）、"
            "playbook_generator.py（把行变成候选）、controller.py（探针、分流、选择）。", note)]

    def on_page(canvas, doc_):
        canvas.saveState()
        canvas.setFont("CJK", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(A4[0] - 17 * mm, 8 * mm, f"AdaMAS · 剧本表说明 · 2026-09-30 · 第 {doc_.page} 页")
        canvas.restoreState()

    doc.build(s, onFirstPage=on_page, onLaterPages=on_page)
    print("wrote", out)


if __name__ == "__main__":
    build(sys.argv[1])
