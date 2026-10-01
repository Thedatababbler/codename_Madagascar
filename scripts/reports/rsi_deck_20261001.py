# -*- coding: utf-8 -*-
"""AdaMAS-RSI deck (18 pages, Chinese): recursive self-improvement at the orchestration layer.

    .venv/bin/python scripts/reports/rsi_deck_20261001.py docs/slides/adamas-rsi.drawio

Style: pptgen-drawio style 4 (tech blue). Diagrams are built from rectangles,
text and inline SVG icons (data URIs; drawio2pptx rasterises them with resvg).
Connectors are not used because the exporter drops their geometry; arrows are
glyphs or thin bars.
"""
import base64
import html
import sys

MAIN = "#0170C1"
DEEP = "#0B4F8A"
ACC = "#F28C28"
BG = "#FFFFFF"
INK = "#1A1A2E"
MUTED = "#4A5568"
LINE = "#D9E2EC"
SOFT = "#E8F1FA"
SOFT2 = "#F4F7FB"
GREEN = "#2E8B57"
RED = "#C0392B"
GREY = "#9AA5B1"
FONT = "微软雅黑"
EN = "Century Gothic"
FOOT = "AdaMAS-RSI · 编排层上的递归自我改进 · 2026-10"

_id = [0]


def nid():
    _id[0] += 1
    return f"c{_id[0]}"


def esc(t):
    return html.escape(t, quote=True).replace("\n", "&#xa;")


def rect(x, y, w, h, fill, stroke="none", rounded=0, dashed=False, sw=2):
    d = "dashed=1;dashPattern=12 8;" if dashed else ""
    return (f'<mxCell id="{nid()}" value="" style="rounded={rounded};whiteSpace=wrap;html=1;fillColor={fill};'
            f'strokeColor={stroke};strokeWidth={sw};{d}" vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')


def ellipse(x, y, w, h, fill, stroke="none", sw=2, dashed=False):
    d = "dashed=1;dashPattern=12 8;" if dashed else ""
    return (f'<mxCell id="{nid()}" value="" style="ellipse;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};strokeWidth={sw};{d}" '
            f'vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')


def text(x, y, w, h, t, size=32, color=INK, bold=False, align="left", valign="top", font=FONT):
    return (f'<mxCell id="{nid()}" value="{esc(t)}" style="text;html=1;whiteSpace=wrap;strokeColor=none;fillColor=none;'
            f'align={align};verticalAlign={valign};fontSize={size};fontStyle={1 if bold else 0};fontColor={color};'
            f'fontFamily={font};" vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')


def box(x, y, w, h, t, fill=SOFT, size=28, color=INK, bold=False, align="center", stroke=MAIN, rounded=1, sw=2, dashed=False):
    d = "dashed=1;dashPattern=12 8;" if dashed else ""
    return (f'<mxCell id="{nid()}" value="{esc(t)}" style="rounded={rounded};whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};'
            f'strokeWidth={sw};{d}align={align};verticalAlign=middle;fontSize={size};fontStyle={1 if bold else 0};fontColor={color};fontFamily={FONT}'
            f';spacingLeft=14;spacingRight=14;" vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')


# ---------------------------------------------------------------- icons (24x24 stroke icons as SVG data URIs)

ICONS = {
    "target": '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2" fill="{c}"/>',
    "table": '<rect x="3" y="4" width="18" height="16" rx="1"/><path d="M3 9h18M3 14h18M9 4v16M15 4v16"/>',
    "doc": '<path d="M6 2h8l5 5v15H6z"/><path d="M14 2v5h5M9 12h7M9 16h7"/>',
    "pen": '<path d="M4 20l4-1 11-11-3-3L5 16z"/><path d="M13 7l3 3"/>',
    "compass": '<circle cx="12" cy="12" r="10"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
    "cycle": '<path d="M20 12a8 8 0 1 1-3-6.2"/><path d="M20 4v5h-5"/>',
    "gear": '<circle cx="12" cy="12" r="3.5"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/>',
    "shield": '<path d="M12 2l8 3v6c0 5-3.5 9-8 11-4.5-2-8-6-8-11V5z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/><circle cx="12" cy="16" r="1.5" fill="{c}"/>',
    "wall": '<path d="M2 20h20M2 15h20M2 10h20M7 20v-5M15 20v-5M11 15v-5M18 15v-5M7 10V5M15 10V5M2 5h20"/>',
    "search": '<circle cx="10" cy="10" r="7"/><path d="M15 15l6 6"/>',
    "route": '<path d="M5 20V4"/><path d="M5 8h10l3 2-3 2H5"/><path d="M5 14h8l3 2-3 2H5"/>',
    "wrench": '<path d="M14.5 3.5a5 5 0 0 0-6.4 6.4L3 15l3 3 5.1-5.1a5 5 0 0 0 6.4-6.4l-3 3-2-2z"/>',
    "check": '<circle cx="12" cy="12" r="10"/><path d="M7 12.5l3.5 3.5L17 9"/>',
    "ledger": '<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M8 8h8M8 12h8M8 16h5"/><circle cx="6.5" cy="8" r="0.6" fill="{c}"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "flask": '<path d="M9 3h6M10 3v6l-6 10a1.5 1.5 0 0 0 1.3 2.2h13.4A1.5 1.5 0 0 0 20 19l-6-10V3"/><path d="M7 16h10"/>',
    "chart": '<path d="M3 21h18"/><rect x="5" y="11" width="3.5" height="8"/><rect x="10.5" y="6" width="3.5" height="13"/><rect x="16" y="13" width="3.5" height="6"/>',
    "box": '<path d="M3 8l9-5 9 5v10l-9 5-9-5z"/><path d="M3 8l9 5 9-5M12 13v10"/>',
    "replay": '<path d="M4 12a8 8 0 1 0 2.4-5.7"/><path d="M4 4v5h5"/><path d="M11 9.5v5l4-2.5z" fill="{c}"/>',
    "robot": '<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V4M9 4h6"/><circle cx="9" cy="13" r="1.5" fill="{c}"/><circle cx="15" cy="13" r="1.5" fill="{c}"/><path d="M9 17h6"/>',
    "person": '<circle cx="12" cy="7" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "layers": '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5M3 17l9 5 9-5"/>',
    "code": '<path d="M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16"/>',
    "quote": '<path d="M5 6h6v6H7a4 4 0 0 1-2 4v2a6 6 0 0 0 4-6V6zM13 6h6v6h-4a4 4 0 0 1-2 4v2a6 6 0 0 0 4-6V6z"/>',
    "eye": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "tree": '<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><circle cx="12" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5L5 16.5M12 11.5v5M12 11.5l7 5"/>',
    "map": '<path d="M3 6l6-2 6 2 6-2v14l-6 2-6-2-6 2z"/><path d="M9 4v14M15 6v14"/>',
    "flag": '<path d="M5 22V3"/><path d="M5 4h12l-3 4 3 4H5"/>',
    "scale": '<path d="M12 3v18M4 21h16"/><path d="M3 8h18"/><path d="M6 8l-3 7h6zM18 8l-3 7h6z"/>',
    "db": '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    "wave": '<path d="M2 12h3l2-6 3 12 3-9 2 5 2-3h5"/>',
    "play": '<circle cx="12" cy="12" r="10"/><path d="M10 8l6 4-6 4z" fill="{c}"/>',
    "bolt": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="4" cy="6" r="1.2" fill="{c}"/><circle cx="4" cy="12" r="1.2" fill="{c}"/><circle cx="4" cy="18" r="1.2" fill="{c}"/>',
    "cross": '<circle cx="12" cy="12" r="10"/><path d="M8 8l8 8M16 8l-8 8"/>',
    "star": '<path d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.4 6.1 20.5l1.2-6.5L2.5 9.4l6.6-.9z"/>',
}


_ICON_CACHE = {}


def _icon_png_b64(name, color, px):
    """PNG (rasterised with cairosvg) as base64 whose text never contains "svg".

    drawio2pptx 0.0.7 treats any data URI containing the substring "svg" as an
    SVG and hands it to a rasteriser whose API does not match the installed
    resvg, so the picture is silently dropped. Its parser also splits styles
    on ";", so the base64 marker has to sit in the media type ("png+base64")
    rather than after a semicolon. The payload is re-rendered with a harmless
    <desc> element until its base64 is free of the substring.
    """
    key = (name, color, px)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    import cairosvg

    body = ICONS[name].replace("{c}", color)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" '
           f'stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    for n in range(60):
        # a different raster size gives different bytes; <desc> would not
        png = cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=px + n, output_height=px + n)
        data = base64.b64encode(png).decode("ascii")
        if "svg" not in data.lower():
            _ICON_CACHE[key] = data
            return data
    raise RuntimeError(f"icon {name}: no clean encoding")


def icon(name, x, y, size=72, color=MAIN):
    data = _icon_png_b64(name, color, max(96, int(size * 3)))
    return (f'<mxCell id="{nid()}" value="" style="shape=image;imageAspect=1;image=data:image/png+base64,{data};" '
            f'vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="{size}" height="{size}" as="geometry"/></mxCell>')


def page(name, cells):
    body = "".join(cells)
    return (f'<diagram id="{nid()}" name="{esc(name)}"><mxGraphModel page="1" pageWidth="1920" pageHeight="1080">'
            f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root></mxGraphModel></diagram>')


PAGE_NO = [0]


def frame(title, en=""):
    PAGE_NO[0] += 1
    cells = [rect(0, 0, 1920, 1080, BG), rect(0, 0, 1920, 130, MAIN), rect(0, 130, 1920, 8, ACC),
             text(70, 0, 1500, 130, title, size=50, color="#FFFFFF", bold=True, valign="middle"),
             rect(0, 1044, 1920, 3, LINE),
             text(70, 1044, 1200, 34, FOOT, size=22, color=MUTED, valign="middle"),
             text(1620, 1044, 240, 34, f"{PAGE_NO[0]} / 18", size=22, color=MUTED, align="right", valign="middle")]
    if en:
        cells.append(text(1180, 0, 680, 130, en, size=28, color="#CFE3F5", align="right", valign="middle", font=EN))
    return cells


def plain(name, cells):
    PAGE_NO[0] += 1
    return page(name, cells)


def bullets(x, y, w, items, size=32, gap=14, mark=ACC):
    out = []
    cpl = max(8, int(w / (size * 1.12)))
    for it in items:
        sub = it.startswith("  ")
        t = it.strip()
        lines = sum((max(1, -(-len(p) // (cpl - (2 if sub else 0)))) for p in t.split("\n")))
        h = int(lines * size * 1.5) + 14
        ox = 40 if sub else 0
        if sub:
            out.append(ellipse(x + ox, y + int(size * 0.5), 12, 12, MUTED))
        else:
            out.append(rect(x, y + int(size * 0.3), 8, int(size * 0.95), mark))
        out.append(text(x + ox + 28, y, w - ox - 28, h, t, size=size - (2 if sub else 0), color=INK if not sub else MUTED))
        y += h + gap
    return out, y


def heading(x, y, w, t, size=38, color=MAIN):
    return text(x, y, w, int(size * 1.45), t, size=size, color=color, bold=True, valign="middle")


def arrow_r(x, y, w=70, color=ACC, size=54):
    return text(x, y, w, 70, "→", size=size, color=color, bold=True, align="center", valign="middle", font="Arial")


def arrow_d(x, y, h=60, color=ACC, size=54):
    return text(x, y, 70, h, "↓", size=size, color=color, bold=True, align="center", valign="middle", font="Arial")


def arrow_u(x, y, h=60, color=ACC, size=54):
    return text(x, y, 70, h, "↑", size=size, color=color, bold=True, align="center", valign="middle", font="Arial")


def hbar(x, y, w, color=MAIN, t=4):
    return rect(x, y, w, t, color)


def vbar(x, y, h, color=MAIN, t=4):
    return rect(x, y, t, h, color)


def labeled_icon(name, x, y, label, size=72, color=MAIN, lw=220, lsize=24, lcolor=INK):
    return [icon(name, x + (lw - size) // 2, y, size, color), text(x, y + size + 6, lw, 70, label, size=lsize, color=lcolor, align="center")]


def section_tag(x, y, w, t, fill=MAIN):
    return box(x, y, w, 54, t, fill=fill, size=26, color="#FFFFFF", bold=True, stroke="none", rounded=1)


pages = []

# ================================================================ 1 cover
c = [rect(0, 0, 1920, 1080, BG), rect(0, 0, 1920, 18, MAIN), rect(0, 1040, 1920, 40, MAIN),
     text(110, 250, 1000, 150, "AdaMAS-RSI", size=96, color=MAIN, bold=True, valign="middle", font=EN),
     text(110, 400, 1050, 110, "在编排层上的递归自我改进", size=60, bold=True, valign="middle"),
     rect(110, 530, 160, 8, ACC),
     text(110, 560, 1050, 150, "让系统自己改进“如何设计与修复多智能体拓扑”", size=36, color=MUTED, valign="middle"),
     text(110, 900, 1000, 60, "AdaMAS 项目 · 分支 rsi · 2026 年 10 月", size=26, color=MUTED, valign="middle")]
# concentric rings: planner (outer) -> test author -> design cycle -> milestone (inner)
cx, cy = 1480, 560
rings = [(400, "#E3EEF8", "外层循环 · 规划器", "compass"), (300, "#CFE1F3", "慢循环 · 出题者", "pen"), (200, "#A9CCEA", "设计循环 · 两张表", "table"), (100, MAIN, "里程碑", "target")]
for r, fill, lab, ic in rings:
    c.append(ellipse(cx - r, cy - r, 2 * r, 2 * r, fill, stroke="#FFFFFF", sw=3))
c.append(icon("target", cx - 36, cy - 36, 72, "#FFFFFF"))
c.append(text(cx - 190, cy - 400 + 14, 380, 44, "外层循环 · 规划器", size=24, color=DEEP, align="center", valign="middle"))
c.append(text(cx - 190, cy - 300 + 12, 380, 44, "慢循环 · 出题者", size=24, color=DEEP, align="center", valign="middle"))
c.append(text(cx - 190, cy - 200 + 10, 380, 44, "设计循环 · 剧本表与首轮表", size=24, color=DEEP, align="center", valign="middle"))
c.append(text(cx - 120, cy + 44, 240, 44, "里程碑执行", size=22, color="#FFFFFF", align="center", valign="middle"))
c.append(icon("compass", cx - 378, cy - 28, 56, DEEP)); c.append(icon("pen", cx - 278, cy - 28, 56, DEEP)); c.append(icon("table", cx - 178, cy - 28, 56, DEEP))
pages.append(plain("封面", c))

# ================================================================ 2 overview
c = frame("一页总览", "OVERVIEW")
c.append(text(70, 150, 690, 90, "AdaMAS 在每个里程碑上解题；RSI 让它从每次解题的结果里，改进下一次的设计与修复方式", size=26, color=INK, bold=True, valign="middle"))
# left: components and timescales
c.append(heading(70, 240, 600, "四个可进化部件", size=32))
comp = [("table", "修复剧本表"), ("layers", "首轮兼容表"), ("pen", "出题者"), ("compass", "规划器")]
yy = 300
for ic, lab in comp:
    c.append(icon(ic, 80, yy, 52)); c.append(text(150, yy, 480, 52, lab, size=30, valign="middle")); yy += 70
c.append(heading(70, 600, 620, "四个时间尺度", size=32))
ts = [("bolt", "快速搜索：不进化，只产生数据"), ("cycle", "设计循环：改两张表"), ("clock", "慢循环：改出题者"), ("map", "外层循环：改规划器")]
yy = 660
for ic, lab in ts:
    c.append(icon(ic, 80, yy, 50)); c.append(text(150, yy, 560, 50, lab, size=27, valign="middle")); yy += 66
# right: nested loop diagram
X0, Y0 = 780, 180
layers = [(1060, 820, "#EAF2FA", "外层循环 · 进化规划器 · 惰性触发", "compass"),
          (900, 690, "#D6E6F5", "慢循环 · 进化出题者 · held-out 错误累积后", "pen"),
          (740, 560, "#BFD8F0", "设计循环 · 进化剧本表与首轮兼容表 · 每 6 题", "table")]
for i, (w, h, fill, lab, ic) in enumerate(layers):
    x = X0 + (1060 - w) // 2; y = Y0 + (i * 65)
    c.append(rect(x, y, w, h, fill, stroke="#FFFFFF", rounded=1, sw=3))
    c.append(icon(ic, x + 18, y + 12, 44, DEEP)); c.append(text(x + 70, y + 8, w - 90, 50, lab, size=24, color=DEEP, bold=True, valign="middle"))
# inner milestone box
mx, my = X0 + 230, Y0 + 290
c.append(rect(mx, my, 600, 300, MAIN, rounded=1))
c.append(icon("target", mx + 20, my + 16, 56, "#FFFFFF"))
c.append(text(mx + 90, my + 12, 480, 60, "里程碑执行（快速搜索）", size=28, color="#FFFFFF", bold=True, valign="middle"))
inner = ["首轮 → 探针 → 路由诊断", "R0 默认修复 + 剧本行候选", "统一验收 → 提交 → 写账本"]
for i, t in enumerate(inner):
    c.append(text(mx + 30, my + 90 + i * 62, 560, 56, t, size=26, color="#FFFFFF", valign="middle"))
# data out / versions in
c.append(box(X0 + 20, Y0 + 650, 300, 54, "数据向外流：账本、错题库", fill="#FFFFFF", size=22, stroke=ACC, color=ACC))
c.append(arrow_r(X0 + 320, Y0 + 640, 60))
c.append(text(X0 + 700, Y0 + 640, 60, 70, "←", size=54, color=MAIN, bold=True, align="center", valign="middle", font="Arial"))
c.append(box(X0 + 760, Y0 + 640, 280, 74, "新版本向内流：表格、提示词", fill="#FFFFFF", size=22, stroke=MAIN, color=MAIN))
pages.append(page("总览", c))

# ================================================================ 3 where recursion happens
c = frame("递归发生在哪里", "WHERE THE RECURSION IS")
# left small loop
c.append(section_tag(70, 170, 860, "快速搜索：单个里程碑内的小循环（有界自我精炼，不是 RSI）", fill=GREY))
lx, ly = 120, 260
steps = [("cross", "持续失败"), ("wrench", "修复（R0 / 剧本行）"), ("check", "统一验收")]
for i, (ic, lab) in enumerate(steps):
    x = lx + i * 270
    c.append(box(x, ly + 70, 220, 150, "", fill=SOFT2, stroke=GREY))
    c.append(icon(ic, x + 74, ly + 82, 64, MUTED)); c.append(text(x, ly + 150, 220, 60, lab, size=24, color=MUTED, align="center", valign="middle"))
    if i < 2:
        c.append(arrow_r(x + 220, ly + 110, 50, color=GREY))
c.append(text(lx, ly + 240, 760, 60, "↺ 验收不过就再修，对着固定的门收敛", size=26, color=MUTED, valign="middle", font=FONT))
bl, yb = bullets(90, ly + 320, 820, ["它会收敛、可以评估，但它本身不改进“怎么修”", "产出只有账本记录：每个候选逐用例的修复与回归"], size=28)
c += bl
# right big loop
c.append(section_tag(990, 170, 860, "设计循环：跨里程碑的大循环", fill=ACC))
rx, ry = 1010, 250
nodes = [("target", "很多里程碑", rx, ry + 70), ("ledger", "账本 + 错题库", rx + 420, ry + 70), ("table", "新版本的两张表", rx + 420, ry + 330), ("cycle", "回到下一批里程碑", rx, ry + 330)]
for ic, lab, x, y in nodes:
    c.append(box(x, y, 300, 160, "", fill="#FFF4E8", stroke=ACC, sw=3))
    c.append(icon(ic, x + 118, y + 14, 64, ACC)); c.append(text(x, y + 88, 300, 60, lab, size=24, color=INK, align="center", valign="middle"))
c.append(arrow_r(rx + 310, ry + 115, 100))
c.append(arrow_d(rx + 535, ry + 240, 80))
c.append(text(rx + 310, ry + 375, 100, 70, "←", size=54, color=ACC, bold=True, align="center", valign="middle", font="Arial"))
c.append(arrow_u(rx + 115, ry + 240, 80))
c.append(box(rx + 330, ry + 265, 60, 0, "", fill="none", stroke="none"))
c.append(box(rx + 215, ry + 253, 290, 54, "递归在这里", fill=ACC, size=28, color="#FFFFFF", bold=True, stroke="none"))
bl, yb = bullets(1010, ry + 520, 840, ["汇总很多里程碑的修复结果，改进剧本表和首轮兼容表", "新版本用于后面的题，又产生新数据，进入下一轮", "递归的对象是“改进机制”本身，不是某一次的代码"], size=28, mark=ACC)
c += bl
pages.append(page("递归在哪里", c))

# ================================================================ 4 why RSI
c = frame("为什么算 RSI", "WHY THIS COUNTS AS RSI")
bl, yb = bullets(70, 170, 860, [
    "递归：这一轮改进的产出（新表格）是下一轮改进的输入（新数据）",
    "自我：改什么、留什么，由进化 agent 提议、统计规则决定，运行期间人不逐条审批",
    "有界：评估器在每个循环内固定，并有外部锚点（held-out 绊线）",
    "人的角色：开始前设定目标、阈值和边界；不参与每一轮取舍",
    "定位：改进对象是“编排设计”，回路在表格层面闭合；评估器在慢循环中受外部锚点约束",
], size=28)
c += bl
# axes chart on right
ax, ay, aw, ah = 1040, 230, 780, 640
c.append(rect(ax, ay, aw, ah, SOFT2, stroke=LINE))
c.append(vbar(ax + 120, ay + 30, ah - 120, MAIN)); c.append(hbar(ax + 120, ay + ah - 90, aw - 160, MAIN))
c.append(text(ax + 30, ay + 10, 300, 40, "回路闭合程度 ↑", size=22, color=MAIN, bold=True))
c.append(text(ax + aw - 300, ay + ah - 36, 290, 34, "改进什么 →", size=22, color=MAIN, bold=True, align="right"))
ylabs = ["闭环", "人在环上", "人在环中"]
for i, l in enumerate(ylabs):
    c.append(text(ax + 10, ay + 60 + i * 160, 105, 40, l, size=20, color=MUTED, align="right"))
xlabs = ["部署行为", "训练策略", "评估器", "研究过程"]
for i, l in enumerate(xlabs):
    c.append(text(ax + 130 + i * 160, ay + ah - 80, 150, 36, l, size=20, color=MUTED, align="center"))
pts = [("bolt", "快速搜索", 0, 2, GREY), ("table", "设计循环", 1, 0, ACC), ("pen", "慢循环", 2, 1, MAIN)]
for ic, lab, xi, yi, col in pts:
    px = ax + 150 + xi * 160; py = ay + 40 + yi * 160
    c.append(ellipse(px, py, 100, 100, "#FFFFFF", stroke=col, sw=4)); c.append(icon(ic, px + 22, py + 22, 56, col))
    c.append(text(px - 40, py + 100, 180, 36, lab, size=20, color=col, bold=True, align="center"))
pages.append(page("为什么算RSI", c))

# ================================================================ 5 what evolves
c = frame("什么进化，什么不进化", "WHAT MAY CHANGE")
# outer fixed zone
ox, oy, ow, oh = 90, 180, 1740, 840
c.append(rect(ox, oy, ow, oh, "#F1F3F6", stroke=GREY, rounded=1, sw=3))
c.append(text(ox + 24, oy + 10, 500, 50, "固定区（不进化）", size=30, color=MUTED, bold=True, valign="middle"))
fixed = [("gear", "标准流程：探针、路由诊断、R0、统一验收"), ("shield", "验收门与守卫：custody、包影子、环境清理、零分守卫"), ("lock", "held-out 隔离：密封，只有绊线能读"), ("robot", "进化 agent 本身：提示词、校验器、预算")]
for i, (ic, lab) in enumerate(fixed):
    x = ox + 40 + (i % 2) * 860; y = oy + 80 + (i // 2) * 100
    c.append(icon(ic, x, y, 56, MUTED)); c.append(text(x + 70, y, 760, 56, lab, size=25, color=MUTED, valign="middle"))
# wall
c.append(rect(ox + 60, oy + 300, ow - 120, 26, "#B55F1F"))
for i in range(0, ow - 120, 70):
    c.append(rect(ox + 60 + i + (35 if (i // 70) % 2 else 0), oy + 300, 2, 26, "#F1F3F6"))
c.append(icon("wall", ox + ow - 150, oy + 268, 44, "#B55F1F"))
c.append(text(ox + 60, oy + 330, ow - 120, 44, "墙：进化 agent 不可越过（只能提议表格行；不能改代码、门、守卫、出题者、规划器）", size=24, color="#B55F1F", bold=True, align="center", valign="middle"))
# inner evolvable zone
ix, iy, iw, ih = ox + 60, oy + 400, ow - 120, 400
c.append(rect(ix, iy, iw, ih, SOFT, stroke=MAIN, rounded=1, sw=3))
c.append(text(ix + 24, iy + 10, 500, 50, "可进化区", size=30, color=MAIN, bold=True, valign="middle"))
ev = [("table", "修复剧本表", "表格行"), ("layers", "首轮兼容表", "表格行"), ("pen", "出题者提示词", "慢循环，待实现"), ("compass", "规划器", "外层循环，待实现")]
for i, (ic, lab, sub) in enumerate(ev):
    x = ix + 60 + i * 400
    c.append(box(x, iy + 80, 340, 260, "", fill="#FFFFFF", stroke=MAIN))
    c.append(icon(ic, x + 134, iy + 100, 72)); c.append(text(x, iy + 190, 340, 50, lab, size=28, bold=True, align="center", valign="middle"))
    c.append(text(x, iy + 250, 340, 50, sub, size=22, color=MUTED, align="center", valign="middle"))
c.append(text(ox + 40, oy + oh - 60, ow - 80, 50, "原则：测量工具和安全机制在可修改范围之外，否则进化可能学会“绕过门”", size=26, color=INK, bold=True, align="center", valign="middle"))
pages.append(page("什么进化", c))

# ================================================================ 6 carrier
c = frame("用什么载体进化：表格为主", "THE CARRIER")
cards = [("code", "代码", "表达力强，但难审计，容易改坏护栏", GREY), ("quote", "提示词", "灵活，但是否有效只能靠完整运行判断", GREY), ("table", "表格", "每行可校验、可比较、可回滚", MAIN)]
for i, (ic, lab, desc, col) in enumerate(cards):
    x = 70 + i * 290
    c.append(box(x, 175, 270, 230, "", fill="#FFFFFF" if col == GREY else SOFT, stroke=col, sw=3))
    c.append(icon(ic, x + 99, 190, 72, col)); c.append(text(x, 265, 270, 44, lab, size=28, bold=True, color=col, align="center", valign="middle"))
    c.append(text(x + 14, 312, 242, 90, desc, size=20, color=MUTED, align="center", valign="middle"))
bl, yb = bullets(70, 440, 860, ["剧本和首轮设计用表格；进化 agent 输出 YAML 行，方案类行附固定格式的说明文本", "出题者用提示词，放在最慢的循环里", "代码不进化", "理由：约束自我修改的范围，让每一次改动都能被验证"], size=27)
c += bl
# anatomy of a row
rx, ry = 1000, 175
c.append(section_tag(rx, ry, 850, "一行剧本的解剖（E3-T1）", fill=MAIN))
fields = [("错误类", "E3 主路径行为", "flag"), ("动作类型", "T 拓扑：换成 continuation_double", "layers"), ("槽位编辑", "改进者之后加第二个改进者", "person"),
          ("证据路由", "失败清单与输出交给 improver", "route"), ("预算 / 前置条件", "无增量；要求现任已跑过一轮修复", "scale"), ("状态", "active（已启用）", "check")]
for i, (k, v, ic) in enumerate(fields):
    y = ry + 70 + i * 118
    c.append(rect(rx, y, 850, 104, SOFT2 if i % 2 else "#FFFFFF", stroke=LINE))
    c.append(icon(ic, rx + 16, y + 26, 52)); c.append(text(rx + 84, y + 8, 260, 88, k, size=25, bold=True, color=MAIN, valign="middle"))
    c.append(text(rx + 340, y + 8, 500, 88, v, size=24, valign="middle"))
pages.append(page("载体", c))

# ================================================================ 7 standard procedure
c = frame("标准流程：进化的测量基座", "THE FIXED PROCEDURE")
px, py = 70, 200
stages = [("search", "P1 探针", "原设计重跑\n分出持续失败与偶发失败"), ("route", "P2 路由诊断", "错误分类\n非本里程碑的问题路由出去"),
          ("wrench", "P3 默认修复 R0", "在现任工作区上\n带证据继续修"), ("check", "P4 统一验收", "修好持续失败、零回归\n前序套件不新增失败")]
for i, (ic, lab, desc) in enumerate(stages):
    x = px + i * 450
    c.append(box(x, py, 380, 330, "", fill="#FFFFFF", stroke=MAIN, sw=3))
    c.append(rect(x, py, 380, 70, MAIN, rounded=1)); c.append(icon(ic, x + 16, py + 10, 50, "#FFFFFF"))
    c.append(text(x + 80, py, 290, 70, lab, size=28, color="#FFFFFF", bold=True, valign="middle"))
    c.append(text(x + 20, py + 90, 340, 220, desc, size=25, color=INK, align="center", valign="middle"))
    if i < 3:
        c.append(arrow_r(x + 380, py + 130, 70, color=MAIN))
# playbook plug-in between P3 and P4
c.append(rect(px + 1350 + 35 - 2, py + 330, 4, 70, ACC))
c.append(box(px + 1120, py + 400, 530, 120, "", fill="#FFF4E8", stroke=ACC, sw=3, dashed=True))
c.append(icon("table", px + 1140, py + 424, 64, ACC)); c.append(text(px + 1220, py + 400, 420, 120, "剧本表：插件\n在 R0 之外再给一个候选位", size=24, color=INK, valign="middle"))
bl, yb = bullets(70, 600, 1780, ["每次搜索都走同一条流程，不论剧本表怎么变", "所有剧本行的效果都相对 R0 衡量：同一次搜索里，行 vs R0 的逐用例净修复之差进入账本", "流程一变，账本就失去参照，所以流程本身不进化"], size=29)
c += bl
pages.append(page("标准流程", c))

# ================================================================ 8 repair table
c = frame("部件一：修复剧本表", "COMPONENT 1 · REPAIR TABLE")
bl, yb = bullets(70, 165, 700, ["作用：门失败或分数偏低时，按错误类选修复动作", "主键：九个错误类；五类动作：方案 S、拓扑 T、角色 R、预算 B、重采样 N", "每行相对 R0 的配对增量进入账本，按错误类排序，小样本向 R0 收缩", "四种状态：启用、试用、候补、淘汰", "进化 agent 针对“未解决池”提议新行"], size=26)
c += bl
# matrix: classes x actions
rows_tbl = {"E1": {"B": ("E1-B2", "active"), "T": ("E1-T1 / T2", "cand")}, "E2": {"S": ("E2-S1", "cand"), "R": ("E2-R1", "active")},
            "E3": {"S": ("E3-S1", "cand"), "R": ("E3-R1", "cand"), "T": ("E3-T1", "active")}, "E4": {"S": ("E4-S1", "cand"), "R": ("E4-R1", "cand")},
            "E5": {"S": ("E5-S1", "cand"), "R": ("E5-R1", "cand")}, "E6": {"S": ("E6-S1", "cand"), "R": ("E6-R1", "cand")},
            "E7": {"S": ("E7-S1", "cand"), "R": ("E7-R1", "cand"), "T": ("E7-T1", "active")}, "E8": {"S": ("E8-S1", "cand"), "B": ("E8-B1", "cand")},
            "E9": {"S": ("E9-S1", "cand"), "T": ("E9-T1 / T2", "active")}, "*": {"S": ("U-S1", "trial"), "B": ("U-B1", "active"), "N": ("U-N1", "active")}}
names = {"E1": "未完成", "E2": "接口面", "E3": "主路径", "E4": "状态转换", "E5": "错误路径", "E6": "边界协议", "E7": "跨模块", "E8": "性能", "E9": "分散", "*": "通用"}
acts = ["S 方案", "T 拓扑", "R 角色", "B 预算", "N 重采样"]
tx, ty = 830, 165; cw0 = 190; cw = 170; rh = 62
c.append(box(tx, ty, cw0, rh, "错误类 \\ 动作", fill=MAIN, size=20, color="#FFFFFF", bold=True, stroke="none", rounded=0))
for j, a in enumerate(acts):
    c.append(box(tx + cw0 + j * cw, ty, cw, rh, a, fill=MAIN, size=20, color="#FFFFFF", bold=True, stroke="none", rounded=0))
colors = {"active": ("#D7F0DF", GREEN), "trial": ("#FFF1DB", ACC), "cand": ("#FFFFFF", GREY)}
for i, cls in enumerate(["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9", "*"]):
    y = ty + rh + i * rh
    c.append(box(tx, y, cw0, rh, f"{cls} {names[cls]}", fill=SOFT2, size=20, bold=True, stroke=LINE, rounded=0, align="left"))
    for j, a in enumerate("STRBN"):
        cell = rows_tbl.get(cls, {}).get(a)
        if cell:
            fill, st = colors[cell[1]]
            c.append(box(tx + cw0 + j * cw, y, cw, rh, cell[0], fill=fill, size=19, color=INK, stroke=st, rounded=0))
        else:
            c.append(rect(tx + cw0 + j * cw, y, cw, rh, "#FFFFFF", stroke=LINE))
ly = ty + rh * 12 + 10
for k, (lab, key) in enumerate([("启用", "active"), ("试用", "trial"), ("候补", "cand")]):
    fill, st = colors[key]
    c.append(rect(tx + k * 260, ly, 40, 36, fill, stroke=st)); c.append(text(tx + 50 + k * 260, ly, 200, 36, lab, size=22, color=MUTED, valign="middle"))
c.append(text(tx + 780, ly, 260, 36, "另有 4 条旧剧本行（通用 T）", size=20, color=MUTED, valign="middle"))
pages.append(page("剧本表", c))

# ================================================================ 9 first-pass table
c = frame("部件二：首轮兼容表（首轮设计者）", "COMPONENT 2 · FIRST-PASS TABLE")
bl, yb = bullets(70, 165, 690, ["作用：里程碑开始前，按特征在规划器的设计上叠加预防措施", "F0 等于规划器原选择；F1–F6 是增量：给实现者异常清单、加只读审阅者、换形状、加预算", "没有同场对照：线上随机分配做初筛，错题重跑做最终判定", "判定：最终结果不差，总成本不高于“F0 + 事后修复”，目标错误类确实减少"], size=26)
c += bl
# feature profile -> matched entries -> topology
fx, fy = 800, 170
c.append(section_tag(fx, fy, 280, "里程碑特征", fill=MAIN))
feats = [("类型", "基础型"), ("文件数", "4"), ("公共符号", "18"), ("异常数", "6"), ("状态转换", "0"), ("依赖深度", "0")]
for i, (k, v) in enumerate(feats):
    y = fy + 70 + i * 66
    c.append(rect(fx, y, 280, 58, SOFT2 if i % 2 else "#FFFFFF", stroke=LINE))
    c.append(text(fx + 14, y, 150, 58, k, size=22, color=MUTED, valign="middle")); c.append(text(fx + 160, y, 110, 58, v, size=22, bold=True, align="right", valign="middle"))
    c.append(rect(fx + 290, y + 18, int(16 + 100 * [0.5, 0.3, 0.9, 0.6, 0.0, 0.0][i]), 20, MAIN if i < 4 else LINE))
c.append(arrow_r(fx + 415, fy + 230, 60))
mx = fx + 485
c.append(section_tag(mx, fy, 260, "匹配到的条目", fill=ACC))
ents = [("F1", "基础型 → 加合同评审", "trial"), ("F2", "异常数 ≥ 5 → 异常清单", "cand"), ("F0", "规划器原样", "active")]
for i, (k, v, st) in enumerate(ents):
    y = fy + 70 + i * 120
    fill, sc = colors[st]
    c.append(box(mx, y, 260, 104, "", fill=fill, stroke=sc, sw=3)); c.append(text(mx + 14, y + 2, 232, 50, k, size=26, bold=True, valign="middle"))
    c.append(text(mx + 14, y + 46, 232, 56, v, size=18, color=MUTED, valign="middle"))
c.append(arrow_r(mx + 265, fy + 230, 60))
tx2 = mx + 330
c.append(section_tag(tx2, fy, 300, "叠加后的拓扑", fill=MAIN))
topo = [("pen", "出题者"), ("person", "实现者"), ("eye", "合同评审（F1 新增）"), ("wrench", "门修复者")]
for i, (ic, lab) in enumerate(topo):
    y = fy + 70 + i * 100
    hl = i == 2
    c.append(box(tx2, y, 300, 80, "", fill="#FFF4E8" if hl else "#FFFFFF", stroke=ACC if hl else MAIN, sw=3 if hl else 2))
    c.append(icon(ic, tx2 + 14, y + 14, 52, ACC if hl else MAIN)); c.append(text(tx2 + 76, y, 220, 80, lab, size=22, valign="middle"))
    if i < 3:
        c.append(arrow_d(tx2 + 115, y + 76, 40, color=MAIN, size=34))
c.append(box(70, 720, 1780, 70, "", fill=SOFT2, stroke=LINE))
c.append(icon("flask", 90, 731, 48, ACC)); c.append(text(150, 720, 1690, 70, "试用中的 F 条目线上随机分配：符合触发特征的里程碑，一半用该条目、一半用 F0，分配结果记入账本（assignment = randomized）", size=23, valign="middle"))
c.append(box(70, 810, 1780, 70, "", fill=SOFT2, stroke=LINE))
c.append(icon("scale", 90, 821, 48, MAIN)); c.append(text(150, 810, 1690, 70, "首轮通过率只做预测校准（触发 → 实际错误类的对应），启用与否只看预防效果，两者分开统计", size=23, valign="middle"))
pages.append(page("首轮表", c))

# ================================================================ 10 interplay
c = frame("两张表如何互通", "HOW THE TWO TABLES CONNECT")
c.append(box(70, 180, 1780, 70, "共享：行格式、状态机、账本、角色池和模板库", fill=SOFT, size=28, bold=True, stroke=MAIN))
# two table cards with arrows
lx, rx, ty_ = 120, 1240, 300
for x, ic, lab, sub in [(lx, "table", "修复剧本表", "事后修"), (rx, "layers", "首轮兼容表", "事前防")]:
    c.append(box(x, ty_, 560, 300, "", fill="#FFFFFF", stroke=MAIN, sw=3))
    c.append(icon(ic, x + 236, ty_ + 24, 88)); c.append(text(x, ty_ + 130, 560, 60, lab, size=34, bold=True, align="center", valign="middle"))
    c.append(text(x, ty_ + 200, 560, 60, sub, size=26, color=MUTED, align="center", valign="middle"))
c.append(hbar(700, ty_ + 70, 520, ACC, 6)); c.append(text(1150, ty_ + 36, 80, 70, "▶", size=40, color=ACC, align="center", valign="middle", font="Arial"))
c.append(text(700, ty_ + 84, 520, 80, "提升为预防版本\n某行在某类错误上反复有效，且该类错误在某种里程碑上频发", size=20, color=INK, align="center"))
c.append(hbar(700, ty_ + 230, 520, MAIN, 6)); c.append(text(690, ty_ + 196, 80, 70, "◀", size=40, color=MAIN, align="center", valign="middle", font="Arial"))
c.append(text(700, ty_ + 244, 520, 80, "形状与角色作为前置条件\n首轮已加的角色，剧本不重复加", size=20, color=INK, align="center"))
bl, yb = bullets(70, 660, 1780, ["剧本 → 首轮：有效的修法提议它的预防版本作为新 F 条目（source_rows 必须指向来源行）", "首轮 → 剧本：首轮选择的形状成为剧本行的前置条件（lacks_role、template）", "并非所有修法都有预防版本：主路径行为、性能、分散类只能事后修"], size=28)
c += bl
pages.append(page("互通", c))

# ================================================================ 11 test author
c = frame("部件三：出题者（慢循环）", "COMPONENT 3 · TEST AUTHOR")
c.append(box(70, 165, 760, 60, "状态：接口已实现（路由记录、账本重建接口），进化逻辑待实现", fill="#FFF4E8", size=24, color="#B55F1F", bold=True, stroke=ACC))
bl, yb = bullets(70, 250, 760, ["作用：定义“什么算对”，是其他部件的测量工具", "进化信号：训练题上，held-out 归因表里归给出题者的错误", "进化方式：新提示词生成新套件，用记忆回放给已存工作区重新打分；只看出错结果，不看 held-out 源码", "硬约束：每条断言必须引用文档原句，由机械检查保证，不交给进化"], size=26)
c += bl
# 2x2 grid
gx, gy, gw, gh = 920, 200, 900, 700
cw2, rh2 = 330, 250
c.append(text(gx + 240, gy, 660, 50, "held-out 通过          held-out 失败", size=24, color=MAIN, bold=True, align="center", valign="middle"))
c.append(text(gx, gy + 60, 230, 250, "门通过", size=26, color=MAIN, bold=True, align="center", valign="middle"))
c.append(text(gx, gy + 60 + rh2 + 20, 230, 250, "门失败", size=26, color=MAIN, bold=True, align="center", valign="middle"))
cells4 = [(0, 0, "一致：对", "check", "#EAF7EE", GREEN), (0, 1, "漏测\n门过了，held-out 没过", "search", "#FFF4E8", ACC),
          (1, 0, "误判\n门挂了，held-out 其实过", "cross", "#FFF4E8", ACC), (1, 1, "一致：错\n归给实现与剧本", "wrench", SOFT2, GREY)]
for r, col, lab, ic, fill, st in cells4:
    x = gx + 240 + col * (cw2 + 20); y = gy + 60 + r * (rh2 + 20)
    c.append(box(x, y, cw2, rh2, "", fill=fill, stroke=st, sw=4 if st == ACC else 2))
    c.append(icon(ic, x + 135, y + 20, 60, st)); c.append(text(x + 10, y + 95, cw2 - 20, 150, lab, size=23, align="center", valign="middle"))
c.append(text(gx + 240, gy + 620, 680, 50, "两个归给出题者的格子，加上“用例冲突：两条用例无法同时满足”", size=22, color=MUTED, align="center", valign="middle"))
pages.append(page("出题者", c))

# ================================================================ 12 planner
c = frame("部件四：规划器（外层循环）", "COMPONENT 4 · PLANNER")
c.append(box(70, 165, 760, 60, "状态：只实现了错误路由，进化逻辑待实现", fill="#FFF4E8", size=24, color="#B55F1F", bold=True, stroke=ACC))
bl, yb = bullets(70, 250, 780, ["作用：把设计文档拆成里程碑", "触发：惰性。只有“held-out 失败但没有里程碑负责”或“边界模糊”的错误累积到阈值才启动", "进化方式：先用规划审计做便宜筛选，再用整题重跑验证", "代价：规划一变，错题库的检查点失效，需要重建；按里程碑特征记的账本经验仍可用"], size=26)
c += bl
# milestone chain with a gap
mx, my = 900, 300
chain = ["基础契约", "核心数据结构", "文本格式", "公共 API 集成"]
for i, lab in enumerate(chain):
    x = mx + i * 240
    c.append(box(x, my, 200, 140, "", fill="#FFFFFF", stroke=MAIN, sw=3)); c.append(icon("target", x + 70, my + 14, 60)); c.append(text(x, my + 80, 200, 50, lab, size=22, align="center", valign="middle"))
    if i < 3:
        c.append(hbar(x + 200, my + 68, 40, MAIN, 6))
gx2 = mx + 2 * 240 - 60
c.append(ellipse(gx2 + 10, my + 150, 100, 100, "#FFF4E8", stroke=ACC, sw=4, dashed=True))
c.append(icon("cross", gx2 + 30, my + 170, 60, ACC))
c.append(text(gx2 - 220, my + 260, 560, 100, "无人负责的失败\nheld-out 失败，但两个里程碑都不认领：边界落在缝隙里", size=22, color="#B55F1F", align="center", valign="middle"))
c.append(icon("compass", mx + 380, my + 400, 64, MAIN)); c.append(text(mx + 460, my + 400, 500, 64, "累积到阈值 → 规划审计 → 整题重跑验证", size=24, valign="middle"))
pages.append(page("规划器", c))

# ================================================================ 13 propose and accept
c = frame("改动如何被提出和接受", "PROPOSE AND ACCEPT")
c.append(section_tag(70, 165, 840, "提出：进化 agent", fill=MAIN)); c.append(icon("robot", 760, 170, 44, "#FFFFFF"))
bl, yb = bullets(70, 235, 840, ["输入：错误类统计、未解决池、跨表的提升线索", "输出：新剧本行或新 F 条目（YAML）", "限制：只能用已有角色和模板；不能含训练题标识符；每轮最多三条"], size=26)
c += bl
c.append(section_tag(70, 520, 840, "接受：统计规则，不靠 LLM 判断", fill=ACC)); c.append(icon("scale", 760, 525, 44, "#FFFFFF"))
bl, yb = bullets(70, 590, 840, ["试用至少 m 次，相对 R0 净修复为正、回归不更多，才升为启用", "两次试用都失败，淘汰；启用后最近 2m 次为负，降回候补", "新升启用的行还要过 held-out 绊线"], size=26, mark=ACC)
c += bl
# state machine
sx, sy = 980, 230
states = [("候补", GREY, sx, sy + 150), ("试用", ACC, sx + 330, sy + 150), ("启用", GREEN, sx + 660, sy + 150)]
for lab, col, x, y in states:
    c.append(ellipse(x, y, 180, 180, "#FFFFFF", stroke=col, sw=5)); c.append(text(x, y, 180, 180, lab, size=34, bold=True, color=col, align="center", valign="middle"))
c.append(ellipse(sx + 330, sy + 575, 180, 120, "#F1F3F6", stroke=GREY, sw=3)); c.append(text(sx + 330, sy + 575, 180, 120, "淘汰", size=30, bold=True, color=MUTED, align="center", valign="middle"))
c.append(arrow_r(sx + 190, sy + 205, 130, color=MAIN)); c.append(text(sx + 170, sy + 110, 170, 60, "选入试用位", size=20, color=MAIN, align="center", valign="middle"))
c.append(arrow_r(sx + 520, sy + 205, 130, color=GREEN)); c.append(text(sx + 490, sy + 110, 200, 60, "≥ m 次配对\n分数 > 0", size=18, color=GREEN, align="center", valign="middle"))
c.append(hbar(sx + 90, sy + 370, 420, ACC, 3)); c.append(text(sx + 60, sy + 340, 60, 60, "◀", size=26, color=ACC, align="center", valign="middle", font="Arial"))
c.append(text(sx + 110, sy + 378, 400, 44, "第一次试用失败：降回候补", size=20, color=ACC, valign="middle"))
c.append(hbar(sx + 90, sy + 440, 750, RED, 3)); c.append(text(sx + 60, sy + 410, 60, 60, "◀", size=26, color=RED, align="center", valign="middle", font="Arial"))
c.append(text(sx + 110, sy + 448, 740, 44, "启用后最近 2m 次配对分数 < 0，或 held-out 绊线触发：降回候补", size=20, color=RED, valign="middle"))
c.append(arrow_d(sx + 385, sy + 505, 60, color=GREY, size=44)); c.append(text(sx + 470, sy + 505, 300, 60, "第二次试用失败", size=20, color=MUTED, valign="middle"))
pages.append(page("提出与接受", c))

# ================================================================ 14 ledger
c = frame("数据驱动（一）：账本与配对对照", "LEDGER AND PAIRED CONTROL")
bl, yb = bullets(70, 165, 820, ["每个候选一条记录：错误类、所用的行、逐用例的修复与回归、相对 R0 的增量、成本、工作区引用", "两个对照组：探针（什么都不做）、R0（标准做法）", "衡量：逐用例的稳定修复和稳定回归，而不是总分", "排序分数向 R0 收缩：n/(n+n0)·mean(Δ)，防止新行靠一两次运气排到前面"], size=26)
c += bl
# record card
rx, ry = 960, 165
c.append(section_tag(rx, ry, 880, "一条账本记录", fill=MAIN)); c.append(icon("ledger", rx + 820, ry + 5, 44, "#FFFFFF"))
rec = [("record_id", "tinydb:storage:E3-T1"), ("error_classes", "[E3]"), ("row_id / state", "E3-T1 / trial"), ("fixed / regressed", "3 / 0"),
       ("delta_vs_R0", "+1"), ("cost.usd", "0.31"), ("workspace_ref", "…/candidates/E3-T1/repo")]
for i, (k, v) in enumerate(rec):
    y = ry + 66 + i * 50
    c.append(rect(rx, y, 880, 46, SOFT2 if i % 2 else "#FFFFFF", stroke=LINE))
    c.append(text(rx + 14, y, 300, 46, k, size=21, color=MAIN, valign="middle", font="Consolas")); c.append(text(rx + 330, y, 540, 46, v, size=21, valign="middle", font="Consolas"))
# paired bars
by = ry + 450
c.append(text(rx, by, 880, 40, "行 vs R0 的配对增量（同一次搜索）", size=24, bold=True, color=MAIN, valign="middle"))
base = by + 240
bars = [("探针", 0, GREY), ("R0", 2, MAIN), ("E3-T1", 3, ACC), ("E3-S1", 1, "#7FB8E6")]
c.append(hbar(rx + 40, base, 800, MUTED, 3))
for i, (lab, v, col) in enumerate(bars):
    x = rx + 90 + i * 200
    c.append(rect(x, base - v * 55, 110, v * 55 if v else 4, col))
    c.append(text(x - 30, base + 8, 170, 40, lab, size=22, align="center", valign="middle")); c.append(text(x - 30, base - v * 55 - 44, 170, 40, f"净修复 {v}", size=20, color=MUTED, align="center", valign="middle"))
pages.append(page("账本", c))

# ================================================================ 15 routing
c = frame("数据驱动（二）：把错误路由给对的部件", "ROUTING FAILURES")
c.append(box(760, 165, 400, 90, "", fill=MAIN, stroke="none")); c.append(icon("cross", 790, 184, 52, "#FFFFFF")); c.append(text(850, 165, 300, 90, "持续失败", size=30, color="#FFFFFF", bold=True, valign="middle"))
c.append(hbar(160, 300, 1600, MAIN, 4)); c.append(vbar(958, 255, 45, MAIN, 4))
leaves = [("wrench", "当前里程碑造成的回归", "→ 剧本", MAIN), ("target", "前序里程碑带着失败提交", "→ 前序里程碑的设计与剧本", MAIN),
          ("pen", "前序套件没覆盖、套件可疑、用例冲突", "→ 出题者", ACC), ("compass", "无人负责、边界模糊", "→ 规划器", ACC), ("shield", "环境问题", "→ 守卫机制", GREY)]
for i, (ic, cond, dest, col) in enumerate(leaves):
    x = 90 + i * 350
    c.append(vbar(x + 138, 300, 50, MAIN, 4))
    c.append(box(x, 350, 280, 300, "", fill="#FFFFFF", stroke=col, sw=3))
    c.append(icon(ic, x + 110, 366, 60, col)); c.append(text(x + 12, 436, 256, 120, cond, size=22, align="center", valign="middle"))
    c.append(box(x + 20, 575, 240, 60, dest, fill=col, size=22, color="#FFFFFF", bold=True, stroke="none"))
bl, yb = bullets(70, 720, 1780, ["每个失败先判断归谁，再决定由谁来改；路由记录写入账本，慢循环和外层循环只读属于自己的那部分", "好处：每个部件只拿到属于自己的错误，归因干净，剧本不会为出题者的错背锅"], size=28)
c += bl
pages.append(page("路由", c))

# ================================================================ 16 bank / replay / rerun
c = frame("数据驱动（三）：错题库、错题重跑、记忆回放", "BANK · RERUN · REPLAY")
c.append(icon("box", 70, 170, 56)); c.append(text(140, 170, 1700, 56, "错题库：训练题的里程碑检查点（前序快照、冻结套件、现任工作区）；失败必收，成功按比例抽样", size=26, valign="middle"))
cols = [("replay", "记忆回放", "复用已存结果，不重新执行", ["输入：账本、已存工作区", "成本：零模型调用", "用途：排序规则验证、预测校准、出题者更新后重建账本"], MAIN),
        ("play", "错题重跑", "从检查点重新执行", ["输入：错题库条目", "成本：每次一个里程碑的运行", "用途：给少见错误类补样本、判定首轮 F 条目"], ACC)]
for i, (ic, lab, sub, items, col) in enumerate(cols):
    x = 70 + i * 900
    c.append(box(x, 250, 860, 420, "", fill="#FFFFFF", stroke=col, sw=3))
    c.append(icon(ic, x + 24, 270, 64, col)); c.append(text(x + 110, 262, 700, 80, f"{lab}  ·  {sub}", size=28, bold=True, color=col, valign="middle"))
    bl, yb = bullets(x + 30, 360, 800, items, size=25, mark=col)
    c += bl
for i, (ic, lab) in enumerate([("scale", "新旧配对"), ("check", "评估器固定"), ("target", "前序状态固定")]):
    x = 70 + i * 600
    c.append(box(x, 700, 560, 70, "", fill=SOFT2, stroke=LINE)); c.append(icon(ic, x + 16, 711, 48)); c.append(text(x + 80, 700, 470, 70, f"重跑规矩：{lab}", size=24, valign="middle"))
c.append(hbar(70, 820, 1780, RED, 4)); c.append(icon("lock", 70, 850, 56, RED))
c.append(text(140, 850, 1700, 60, "held-out 只在这里：训练题上的密封绊线。门上变好但 held-out 变差，就撤销升级并路由给出题者", size=25, color=RED, valign="middle"))
pages.append(page("错题库", c))

# ================================================================ 17 timescales
c = frame("进化的快慢", "FOUR TIMESCALES")
hdr = ["层", "频率", "进化对象", "主要信号"]
rows17 = [("bolt", "快速搜索", "每个里程碑", "不进化，只写账本", "门的逐用例结果"),
          ("cycle", "设计循环", "每 6 道题，或攒够 12 个失败", "剧本表、首轮兼容表", "配对增量、错题重跑"),
          ("clock", "慢循环", "held-out 错误累积后", "出题者", "归给出题者的 held-out 错误"),
          ("map", "外层循环", "惰性触发", "规划器", "无人负责的失败、规划审计")]
widths = [360, 440, 480, 500]
tx, ty = 70, 165
cx_ = tx
for j, h in enumerate(hdr):
    c.append(box(cx_, ty, widths[j], 60, h, fill=MAIN, size=24, color="#FFFFFF", bold=True, stroke="none", rounded=0)); cx_ += widths[j]
for i, (ic, *vals) in enumerate(rows17):
    y = ty + 60 + i * 78; cx_ = tx
    for j, v in enumerate(vals):
        fill = SOFT2 if i % 2 else "#FFFFFF"
        c.append(box(cx_, y, widths[j], 78, ("        " if j == 0 else "") + v, fill=fill, size=22, bold=(j == 0), stroke=LINE, rounded=0, align="left"))
        cx_ += widths[j]
    c.append(icon(ic, tx + 14, y + 14, 50))
# swimlane timeline
ly0 = 640
c.append(text(70, ly0 - 50, 600, 40, "更新时刻（越外层越稀疏）", size=24, bold=True, color=MAIN, valign="middle"))
lanes = [("快速搜索", 14, MAIN), ("设计循环", 5, ACC), ("慢循环", 2, DEEP), ("外层循环", 1, GREY)]
for i, (lab, n, col) in enumerate(lanes):
    y = ly0 + i * 58
    c.append(text(70, y, 240, 50, lab, size=22, color=col, bold=True, valign="middle")); c.append(hbar(330, y + 24, 1500, LINE, 3))
    for k in range(n):
        x = 330 + int((k + 0.5) * 1500 / n)
        c.append(rect(x - 6, y + 10, 12, 32, col))
bl, yb = bullets(70, 890, 1780, ["每道题只用一个版本，版本只在题与题之间切换", "越慢的层越基础：它一变，内层的账本就要用记忆回放重建"], size=26, gap=8)
c += bl
pages.append(page("快慢", c))

# ================================================================ 18 safety and status
c = frame("安全、可控与当前状态", "SAFETY AND STATUS")
c.append(section_tag(70, 165, 860, "可控机制", fill=MAIN))
ctrl = [("gear", "所有功能默认关闭"), ("doc", "设计循环默认只报告不发布"), ("replay", "版本不可变，可回滚；回滚时新升启用的行降回候补"), ("lock", "held-out 密封，只有绊线模块能读")]
for i, (ic, lab) in enumerate(ctrl):
    y = 240 + i * 90
    c.append(icon(ic, 80, y, 56)); c.append(text(150, y, 780, 56, lab, size=26, valign="middle"))
c.append(section_tag(70, 620, 860, "当前状态（2026-10-01）", fill=ACC))
bl, yb = bullets(70, 690, 860, ["七个实现阶段完成，单元测试全部通过", "首轮付费对照已跑：CPE 7 道训练题（gpt-5.5），进化臂与基线同噪声水平，三处缺陷在跑中修复", "NL2Repo 三题系列（gpt-5.6-sol）进行中；设计循环已以只报告模式跑过一轮"], size=24, mark=ACC)
c += bl
# roadmap
rx, ry = 1000, 180
c.append(section_tag(rx, ry, 850, "路线图", fill=MAIN))
steps = [("冒烟测试", True), ("训练题对照（基线 vs 标准流程）", True), ("第一次设计循环（只报告）", True), ("错题重跑与发布 v2", False), ("慢循环：出题者进化", False), ("测试题评估（冻结版本，各跑 2 次）", False)]
for i, (lab, done) in enumerate(steps):
    y = ry + 80 + i * 118
    col = GREEN if done else GREY
    c.append(vbar(rx + 36, y + 70, 48, col if done else LINE, 4) if i < len(steps) - 1 else rect(0, 0, 0, 0, BG))
    c.append(ellipse(rx + 14, y + 10, 48, 48, col if done else "#FFFFFF", stroke=col, sw=3))
    c.append(text(rx + 14, y + 10, 48, 48, "✓" if done else "", size=26, color="#FFFFFF", bold=True, align="center", valign="middle", font="Arial"))
    c.append(box(rx + 90, y, 760, 68, lab, fill="#FFFFFF" if done else SOFT2, size=24, color=INK if done else MUTED, stroke=col, dashed=not done, align="left"))
pages.append(page("状态", c))

xml = '<mxfile host="app.diagrams.net">' + "".join(pages) + "</mxfile>"
out = sys.argv[1] if len(sys.argv) > 1 else "docs/slides/adamas-rsi.drawio"
with open(out, "w", encoding="utf-8") as fh:
    fh.write(xml)
print(f"wrote {out}: {len(pages)} pages, {_id[0]} cells")
