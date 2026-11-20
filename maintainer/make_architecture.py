#!/usr/bin/env python3
"""生成 README 开头那张架构图（docs/architecture.svg）。

图讲一件事：你只跟一个 PM 提需求，它拆活、派活、盯活、收活，
所有状态落进中心库，闸门决定每一步能不能过。

改文案之后重跑本脚本即可，坐标不用手调：

    python maintainer/make_architecture.py
"""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture.svg"

W, H = 1280, 920
INK = "#2A2520"
MUTED = "#8A7663"
LINE = "#C4552D"
PAPER = "#FAF6EF"
CARD = "#FDFBF7"
TINT = "#F3E9DC"
DASH = "#B9A894"
WHITE = "#FFFFFF"
FONT = "Microsoft YaHei, PingFang SC, Noto Sans SC, sans-serif"
# GitHub 正文宽度只有 800 多像素，1280 宽的图会缩到 0.67，字号要按这个比例放大
SCALE = 1.18


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, size=13, fill=INK, weight="normal", anchor="start", opacity=None):
    size = round(size * SCALE, 1)
    extra = f' opacity="{opacity}"' if opacity else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
        f'font-weight="{weight}" text-anchor="{anchor}"{extra}>{esc(s)}</text>'
    )


def rect(x, y, w, h, fill, stroke=None, rx=14, dashed=False, width=1.5):
    dash = ' stroke-dasharray="7 6"' if dashed else ""
    stroke_attr = f' stroke="{stroke}" stroke-width="{width}"{dash}' if stroke else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{stroke_attr}/>'


def badge(cx, cy, n, r=14):
    return (
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{LINE}"/>'
        + text(cx, cy + 4.5, str(n), size=13, fill=WHITE, weight="bold", anchor="middle")
    )


def arrow(x1, y1, x2, y2, color=LINE, dashed=False, width=2.2):
    dash = ' stroke-dasharray="7 6"' if dashed else ""
    marker = "arrow-dash" if dashed else "arrow"
    return (
        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
        f'stroke-width="{width}"{dash} marker-end="url(#{marker})"/>'
    )


parts: list[str] = []
add = parts.append

add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
add("<defs>")
add(f'<marker id="arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
    f'<path d="M0,0 L10,4 L0,8 z" fill="{LINE}"/></marker>')
add(f'<marker id="arrow-dash" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
    f'<path d="M0,0 L10,4 L0,8 z" fill="{DASH}"/></marker>')
add("</defs>")

# 背景
add(rect(0, 0, W, H, PAPER, rx=0))

# 顶部：巡航回环
add(text(640, 46, "巡航：没人回复的活，定期查工单，续上", 14, MUTED, anchor="middle"))
add('<path d="M 1010 108 L 1010 72 L 505 72 L 505 106" fill="none" '
    f'stroke="{DASH}" stroke-width="2" stroke-dasharray="7 6" marker-end="url(#arrow-dash)"/>')
add(badge(1010, 72, 7, 11))

# 左：你
add(rect(30, 150, 170, 120, CARD, LINE, rx=18, width=2))
add(text(115, 198, "你", 30, LINE, "bold", "middle"))
add(text(115, 228, "只提需求", 13, INK, anchor="middle"))
add(text(115, 250, "和拍板", 13, INK, anchor="middle"))

# 左：外部交接
add(rect(30, 400, 170, 170, CARD, DASH, rx=18, dashed=True, width=1.8))
add(text(45, 428, "别的项目", 14, INK, "bold"))
add(text(45, 456, "六节交接包", 12.5, INK))
add(text(45, 478, "intake_gate 查缺节", 12, MUTED))
add(text(45, 500, "过闸门就接手", 12.5, INK))
add(arrow(202, 470, 258, 470))
add(text(228, 452, "交进来", 10.5, LINE, anchor="middle"))

# 中：PM
add(rect(260, 110, 470, 470, INK, None, rx=22))
add(text(290, 154, "AI 工作区 · 你的 PM", 22, WHITE, "bold"))
add(text(290, 178, "从 PRD 开始，一张单负责到合并", 13, "#D9C9B6"))

rows = [
    ("拆活", "PRD → 工单", "写清交付物、不做什么、验收条件"),
    ("派活", "判断项目 → 开执行会话", "上下文刚好够用：不多也不少"),
    ("盯活", "收执 → 再分派", "停住的活定期查库、续上"),
    ("收活", "独立验收 → auto_merge", "预演、测试、回退点、合并记录"),
]
for i, (tag, title, body) in enumerate(rows):
    y = 200 + i * 82
    add(rect(285, y, 420, 66, "#3A342D", None, rx=12))
    add(text(305, y + 27, tag, 15, "#F5B98A", "bold"))
    add(text(348, y + 27, title, 13, "#F2E9DE"))
    add(text(305, y + 52, body, 11.5, "#C9BAA8"))

add(text(290, 548, "工单是管理对象，会话是可替换的执行器", 12, "#BFAE9C"))

# 右：执行会话
add(rect(780, 110, 470, 260, CARD, DASH, rx=18, dashed=True, width=1.8))
add(text(802, 142, "执行会话 · 多个项目", 14, INK, "bold"))
add(text(1228, 142, "每单一棵工作树", 11.5, MUTED, anchor="end"))
projects = ["项目 A · 新功能", "项目 B · 流程改造", "项目 C · 另一条线"]
for i, name in enumerate(projects):
    y = 162 + i * 66
    add(rect(802, y, 426, 52, TINT, None, rx=10))
    add(text(822, y + 23, name, 13, INK, "bold"))
    add(text(822, y + 42, "独立分支，主检出不动", 11.5, MUTED))

# 右：独立验收
add(rect(780, 410, 470, 78, "#F6EFE4", LINE, rx=14, width=1.8))
add(text(802, 440, "独立验收", 15, LINE, "bold"))
add(text(802, 464, "换一个会话判；做的人不能签字", 12.5, INK))

# 右：合并
add(rect(780, 518, 470, 92, CARD, DASH, rx=14, dashed=True, width=1.8))
add(text(802, 548, "auto_merge", 15, INK, "bold"))
add(text(802, 574, "预演 → 跑测试 → 打回退点 → 合并 → 写记录", 12.5, INK))
add(text(802, 596, "红了回到合并之前", 11.5, MUTED))

# 箭头与编号
add(arrow(200, 210, 258, 210))
add(badge(228, 186, 1))
add(text(228, 240, "提需求", 10.5, MUTED, anchor="middle"))

add(arrow(732, 240, 776, 240))
add(badge(754, 216, 2))
add(text(754, 270, "派活", 10.5, MUTED, anchor="middle"))

add(arrow(776, 330, 732, 330))
add(badge(754, 306, 3))
add(text(754, 360, "回执", 10.5, MUTED, anchor="middle"))

add(arrow(732, 450, 776, 450))
add(badge(754, 426, 4))
add(text(754, 480, "安排验收", 10.5, MUTED, anchor="middle"))

add(arrow(1015, 492, 1015, 514))
add(badge(1042, 503, 5, 11))

add(arrow(1015, 614, 1015, 636))
add(badge(1042, 625, 6, 11))

# 底部：中心库
add(rect(30, 640, 1220, 112, TINT, None, rx=16))
add(text(52, 674, "中心库（SQLite）· 一个库管所有项目", 15, INK, "bold"))
add(text(52, 702, "工单 · 尝试 · 提交 · 证据 · 验收 · 合并记录", 13, INK))
add(text(52, 728, "会话可以丢，账不能丢", 12.5, MUTED))
add(text(1228, 674, "换会话、换人、过两周，都查得到", 12, MUTED, anchor="end"))

# 底部：闸门
add(rect(30, 770, 1220, 112, CARD, DASH, rx=16, dashed=True, width=1.8))
add(text(52, 802, "闸门 · 条件不满足就非零退出", 14, INK, "bold"))
gates = ["work_gate", "doc_gate", "close_gate", "intake_gate", "claims_gate", "ref_gate"]
for i, name in enumerate(gates):
    x = 52 + i * 196
    add(rect(x, 818, 180, 38, "#F6EFE4", None, rx=9))
    add(text(x + 90, 843, name, 12.5, INK, anchor="middle"))

add("</svg>")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(parts), encoding="utf-8")
print(f"已写入 {OUT}")
