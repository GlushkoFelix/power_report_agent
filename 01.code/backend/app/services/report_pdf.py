"""评测报告 Markdown → PDF（PRD 9.1 / 交付物 8，v1.19.12 补齐）。

**为什么放在后端**：用户实测反馈"导出的 .md 还得拿工具打开才看得出排版"。后端本来就有
`reportlab`（交付物 2 的依赖树里带进来的），且报告就是后端生成的 Markdown —— 在这里渲染一次，
前端点一下就能拿到**真 PDF**（浏览器 / WPS / Word 直接打开，表格与分页都保留）。

**为什么"渲染现有 Markdown"而不是"从结构化数据另画一份"**：报告是**单一事实源**
（`eval_service._write_report` 产出的 `.md` 同时被测试与文档引用）。若再从 `result_json` 重画，
一旦改了报告模板忘了改 PDF，"下载的 PDF 与在线报告不一致"——所以这里只做**渲染**。

支持的语法子集（与 `_write_report` 的产出一一对齐）：
`#`–`####` 标题、`|` 表格（首行为表头）、`- ` 列表、`> ` 引用、`---` 分隔线、
空行分段、`**粗体**`、`` `代码` ``。

中文字体用 reportlab 自带的 `UnicodeCIDFont('STSong-Light')`（Adobe CID 字体，**不需要**外部 TTF，
换机器也能跑）；`✅/❌/⚠️` 这类 STSong 里没有的符号会被替换成文字标记，避免渲染成空白方块。
"""

from __future__ import annotations

import io
import re
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

__all__ = ["to_pdf"]

#: 中文字体（reportlab 自带 CID 字体；注册失败时退回 Helvetica——中文会缺字，但不至于导出失败）
CJK_FONT = "STSong-Light"
FALLBACK_FONT = "Helvetica"

#: 报告里用到的符号 → PDF 可见的文字（STSong 覆盖 CJK 与拉丁，不覆盖 emoji）
_SYMBOL_MAP = {
    "✅": "[通过]",
    "❌": "[未通过]",
    "⚠️": "[注意]",
    "⚠": "[注意]",
    "🔴": "[高]",
    "🟠": "[中]",
    "🟢": "[低]",
    "📊": "",
    "🌟": "",
    "🟡": "[中]",
}


def _register_font() -> str:
    """注册中文字体并返回字体名（幂等；失败退回 Helvetica）。"""
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(CJK_FONT))
        return CJK_FONT
    except Exception:  # noqa: BLE001 —— 极端环境下也要能导出（宁可缺字，不可 500）
        return FALLBACK_FONT


def _clean(text: str) -> str:
    """替换 emoji 与不可渲染字符（超 BMP 的字符直接丢弃）。"""
    out = text
    for symbol, replacement in _SYMBOL_MAP.items():
        out = out.replace(symbol, replacement)
    return "".join(ch for ch in out if ord(ch) <= 0xFFFF)


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """行内标记：`**粗体**` 与 `` `代码` ``（先转义，再挂标签）。"""
    out = _xml_escape(_clean(text))
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', out)
    return out


def _styles(font: str) -> dict[str, ParagraphStyle]:
    base = {"fontName": font, "textColor": colors.HexColor("#1F2937")}
    return {
        "body": ParagraphStyle("body", **base, fontSize=8.5, leading=12.5, spaceAfter=2),
        "cell": ParagraphStyle("cell", **base, fontSize=7, leading=9.5),
        "cell_head": ParagraphStyle("cell_head", **base, fontSize=7, leading=9.5),
        1: ParagraphStyle("h1", **base, fontSize=17, leading=22, spaceBefore=4, spaceAfter=8),
        2: ParagraphStyle("h2", **base, fontSize=12.5, leading=17, spaceBefore=8, spaceAfter=4),
        3: ParagraphStyle("h3", **base, fontSize=10, leading=14, spaceBefore=6, spaceAfter=3),
        4: ParagraphStyle("h4", **base, fontSize=9, leading=13, spaceBefore=4, spaceAfter=2),
        "bullet": ParagraphStyle(
            "bullet", **base, fontSize=8.5, leading=12.5, leftIndent=10, bulletIndent=2
        ),
        "quote": ParagraphStyle(
            "quote",
            **{**base, "textColor": colors.HexColor("#4B5563")},
            fontSize=8,
            leading=12,
            leftIndent=8,
        ),
    }


def _is_separator_row(cells: list[str]) -> bool:
    return all(set(cell) <= set("-: ") and cell for cell in cells)


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _make_table(block: list[str], styles: dict[str, ParagraphStyle], font: str, width: float):
    """`|` 表格块 → reportlab Table（列宽均分、单元格自动换行、长表跨页重复表头）。"""
    rows = [_split_row(line) for line in block]
    rows = [row for row in rows if not _is_separator_row(row)]
    if not rows:
        return None
    columns = max(len(row) for row in rows)
    for row in rows:
        row += [""] * (columns - len(row))

    body: list[list[Any]] = []
    for index, row in enumerate(rows):
        style = styles["cell_head"] if index == 0 else styles["cell"]
        body.append([Paragraph(_inline(cell), style) for cell in row])

    table = Table(
        body, colWidths=[width / columns] * columns, repeatRows=1, hAlign="LEFT"
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF1F5")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9D1DC")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def _flowables(markdown: str, styles: dict[str, ParagraphStyle], font: str, width: float):
    """把 Markdown 逐行翻译成 Flowable 序列（够用的子集，见模块 docstring）。"""
    lines = markdown.splitlines()
    story: list[Any] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = raw.strip()

        if not line:
            story.append(Spacer(1, 4))
            index += 1
            continue

        # 表格块
        if line.startswith("|"):
            block = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index])
                index += 1
            table = _make_table(block, styles, font, width)
            if table is not None:
                story += [table, Spacer(1, 6)]
            continue

        # 分隔线 / 分页
        if set(line) <= set("-— "):
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#D9DEE7")))
            story.append(Spacer(1, 4))
            index += 1
            continue
        if line.startswith("<!-- pagebreak -->"):
            story.append(PageBreak())
            index += 1
            continue

        # 标题
        if line.startswith("#"):
            level = min(len(line) - len(line.lstrip("#")), 4)
            story.append(Paragraph(_inline(line[level:].strip()), styles[level]))
            index += 1
            continue

        # 列表 / 引用
        if line.startswith(("- ", "* ")):
            story.append(
                Paragraph(_inline(line[2:].strip()), styles["bullet"], bulletText="•")
            )
            index += 1
            continue
        if line.startswith("> "):
            story.append(Paragraph(_inline(line[2:].strip()), styles["quote"]))
            index += 1
            continue

        story.append(Paragraph(_inline(line), styles["body"]))
        index += 1
    return story


def to_pdf(markdown: str, *, title: str = "评测报告") -> bytes:
    """把报告 Markdown 渲染为 PDF 字节（A4 **横向**：报告里多列表格很宽，纵向会挤爆）。"""
    font = _register_font()
    styles = _styles(font)
    buffer = io.BytesIO()
    margin = 12 * mm
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
        title=title,
        author="智电析报",
    )
    width = doc.pagesize[0] - 2 * margin

    def _footer(canvas, _doc) -> None:  # pragma: no cover - 渲染细节
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawRightString(
            doc.pagesize[0] - margin, 6 * mm, f"第 {canvas.getPageNumber()} 页"
        )
        canvas.drawString(margin, 6 * mm, "智电析报 · 评测报告")
        canvas.restoreState()

    doc.build(_flowables(markdown, styles, font, width), onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
