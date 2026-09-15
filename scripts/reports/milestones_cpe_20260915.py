# -*- coding: utf-8 -*-
"""One-page PDF: feature-first milestone planning on CodeProjectEval (interim, 2026-09-15)."""
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

pdfmetrics.registerFont(TTFont("CJK", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", subfontIndex=0))

INK = colors.HexColor("#1A1A2E")
MUTED = colors.HexColor("#4A5568")
ACCENT = colors.HexColor("#1F497D")
RULE = colors.HexColor("#D5DAE3")
UP = colors.HexColor("#1B6E3A")
DOWN = colors.HexColor("#A33A2F")
BAND = colors.HexColor("#EEF1F6")

title = ParagraphStyle("t", fontName="CJK", fontSize=17.5, leading=23, textColor=INK, wordWrap="CJK")
sub = ParagraphStyle("s", fontName="CJK", fontSize=9, leading=13, textColor=MUTED, wordWrap="CJK")
h2 = ParagraphStyle("h", fontName="CJK", fontSize=11.5, leading=15, textColor=ACCENT, spaceBefore=8, spaceAfter=3)
body = ParagraphStyle("b", fontName="CJK", fontSize=9.6, leading=14.2, textColor=INK, alignment=TA_LEFT, wordWrap="CJK", spaceAfter=1.5)
bullet = ParagraphStyle("bl", parent=body, leftIndent=9, bulletIndent=0)
cell = ParagraphStyle("c", fontName="CJK", fontSize=8.6, leading=11, textColor=INK)
note = ParagraphStyle("n", fontName="CJK", fontSize=8, leading=11.5, textColor=MUTED, wordWrap="CJK", spaceBefore=2)


def bl(text):
    return Paragraph(text, bullet, bulletText="•")


rows = [
    # task, milestones, feature, two-ms, best baseline
    ("bplustree", 5, 0.902, 0.890, 0.890),
    ("tinydb", 4, 0.868, 0.750, 0.877),
    ("pyjwt", 5, 0.779, 0.813, 0.748),
    ("simpy", 4, 0.805, 0.765, 0.765),
    ("imapclient", 5, 0.412, 0.356, 0.390),
    ("python-hl7", 5, 0.530, 0.530, 0.480),
    ("drf-simplejwt", 5, 0.759, 0.565, 0.031),
    ("flask", 5, 0.763, 0.639, 0.000),
]
mean_f = sum(r[2] for r in rows) / len(rows)
mean_o = sum(r[3] for r in rows) / len(rows)
mean_b = sum(r[4] for r in rows) / len(rows)

data = [["题目", "里程碑数", "按功能拆分", "原 2 里程碑", "差值", "最好基线"]]
styles = []
for i, (t, n, f, o, b) in enumerate(rows, start=1):
    d = round(f - o, 3)
    data.append([t, str(n), f"{f:.3f}", f"{o:.3f}", f"{d:+.3f}", f"{b:.3f}"])
    if abs(d) > 0.04:
        styles.append(("TEXTCOLOR", (4, i), (4, i), UP if d > 0 else DOWN))
    if f >= max(o, b):
        styles.append(("TEXTCOLOR", (2, i), (2, i), ACCENT))
data.append(["平均（8 题）", "", f"{mean_f:.3f}", f"{mean_o:.3f}", f"{mean_f - mean_o:+.3f}", f"{mean_b:.3f}"])
last = len(data) - 1

table = Table(data, colWidths=[34 * mm, 20 * mm, 26 * mm, 26 * mm, 20 * mm, 24 * mm], rowHeights=6.4 * mm)
table.setStyle(TableStyle([
    ("FONTNAME", (0, 0), (-1, -1), "CJK"),
    ("FONTSIZE", (0, 0), (-1, -1), 9.2),
    ("TEXTCOLOR", (0, 0), (-1, -1), INK),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ("ALIGN", (1, 0), (-1, 0), "CENTER"),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("ROWBACKGROUNDS", (0, 1), (-1, last - 1), [colors.white, BAND]),
    ("LINEBELOW", (0, last - 1), (-1, last - 1), 0.8, ACCENT),
    ("FONTSIZE", (0, last), (-1, last), 9.8),
    ("TEXTCOLOR", (2, last), (2, last), ACCENT),
    ("TEXTCOLOR", (4, last), (4, last), UP),
    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ("LEFTPADDING", (0, 0), (-1, -1), 5),
    ("BOX", (0, 0), (-1, -1), 0.5, RULE),
    *styles,
]))

story = [
    Paragraph("AdaMAS 按功能拆分里程碑：CodeProjectEval 多阶段题结果", title),
    Paragraph("2026-09-15 · 模型 gpt-5.5 · 分支 milestones（cc9c52fb）· 指标为 held-out 单元测试通过率 · CPE 中全部 10 道多阶段题", sub),
    Spacer(1, 3 * mm),
    Paragraph("做了什么", h2),
    bl("规划器从“风险优先、默认 1 个里程碑”改为“按文档功能模块拆 2–5 个，基础先行、整合最后”，每个里程碑独立写套件、独立验收、提交后冻结。18 道 CPE 题的平均里程碑数从 1.33 变为 4.67。"),
    bl("里程碑内部的搜索与修复与 bestn 臂相同：探针区分持续/偶发失败，持续失败走续修候选，偶发失败走节点重采样，套件全零走重出题。"),
    bl("运行中补上三处验收门缺陷（已合回 bestn）：超时改用 signal 方式并给未完成用例记名；0 分里程碑不得提交到已有基础之上；支持从断点续跑。"),
    Paragraph("结果", h2),
    table,
    Paragraph("蓝色：不低于旧版与最好基线。差值着色：超出同设计重跑噪声（约一个用例，0.04）。每格为单次运行。未计入平均的 2 题（严格评测拒绝计分）：trailscraper 0.67* / 0.60* / 0.65*，portalocker 所有方法 0.00*（held-out 依赖文档未写的 LockerType）。", note),
    Paragraph("发现", h2),
    bl("8 题中 7 题不低于原 2 里程碑版（python-hl7 持平），平均 +0.064。明显超出噪声的是 tinydb（+0.118）、drf-simplejwt（+0.194）和 flask（+0.124）。10 道题的 46 个里程碑全部提交，修好验收门后每题一次跑完，0 分守卫未触发。"),
    bl("拆分解决了它针对的结构性损失：tinydb 旧版冻结在第 1 里程碑、后面无法触及的 LRUCache 缺陷，被第 1 里程碑自己的套件抓到并修好；bplustree 树层挂住，在该里程碑的验收门暴露并被 0 分守卫挡住（不挡时 held-out 为 0.329）。"),
    bl("唯一落后的 pyjwt（−0.034）：27 个新失败中 12 个同源于一个文档未写的导入细节（白盒），其余 13 个是第 1 里程碑套件未覆盖的错误路径——覆盖广度问题出在早期里程碑，而新加的广度要求只作用于最后的整合里程碑。"),
    bl("节点级重采样作为剧本表的一行已启用，但只真正运行 1 次（drf-simplejwt，1.00，与探针平分被弃），另有多次因每里程碑只剩 1 个槽位让给续修候选，未对结果产生影响。"),
    Paragraph("局限与成本", h2),
    bl("样本少：8 道计分题、每格 1 次。bplustree 与 tinydb 运行时尚无整合广度要求，其余题有。本批用去 prolite 账户约 55% 周额度。"),
    bl("修好验收门后每题一次跑完（48–205 分钟）；bplustree 因验收门与账户窗口问题共耗 4 个 5 小时窗口。"),
    Paragraph("下一步", h2),
    bl("NL2Repo 9 道核心题的按功能拆分计划已生成（平均 4.44 个里程碑），待跑；把广度要求扩展到所有里程碑；对差值在噪声内的题重复运行后，再决定是否全量替换规划器。"),
]

out = sys.argv[1]
doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=13 * mm, bottomMargin=11 * mm,
                        title="AdaMAS 按功能拆分里程碑：CPE 多阶段题结果", author="AdaMAS")
doc.build(story)
print("wrote", out)
