# -*- coding: utf-8 -*-
"""图表离线预览工具（本机无前端时"看得见图"的方案，交付物 4 第三批决策 ⑩）。

用法：
    .venv\\Scripts\\python.exe 01.code\\tools\\chart_preview.py --record a_000123
    .venv\\Scripts\\python.exe 01.code\\tools\\chart_preview.py --record a_000123 --type bar,pie
    .venv\\Scripts\\python.exe 01.code\\tools\\chart_preview.py --record a_000123 --granularity 日,月
    .venv\\Scripts\\python.exe 01.code\\tools\\chart_preview.py --record a_000123 --open

输出：`02.data/tmp/chart_{record_code}.html`，**自包含**（`plotly.min.js` 取自 plotly 包内联，
不依赖 CDN，断网可用），浏览器双击即看。

为什么不做成接口（决策 ⑩）：PRD 9.1 的接口清单不该为"开发期看图"多一个端点（还要进鉴权、
归属隔离与用例）；本脚本**直调 service 且不写数据库**（`persist=False`），产出的 HTML 还能当
评审证据存档。

页面自上而下：
    1. 记录信息（问题 / 报表 / SQL / slot / 行数 / 耗时）
    2. **自动选型**图（`chart_hint` + 形状兜底重生成，不落库）
    3. 记录里保存的图（`analyses.chart_json`，回写过的会与 2 不同）
    4. **各图表类型对比**（默认六种全试，画不出来的把 400 文案如实展示）
    5. **各聚合粒度对比**（`--granularity 日,月` 触发快照重聚合）
    6. 结果表前 50 行（图表与表格同源，便于交叉核对）

注意：本工具**只读**正式库，但会连 `02.data/power_agent.db` 读取记录，请勿在演示机上乱跑
（它不会修改任何数据）。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ── 让脚本能从任意工作目录运行：把 `01.code/backend` 加进 sys.path ──────────
_TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = _TOOLS_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import DATA_DIR  # type: ignore[import-not-found]  # noqa: E402 —— 路径均以项目根为基准，与 CWD 无关
from app.core.errors import BizError  # type: ignore[import-not-found]  # noqa: E402
from app.db.models import Analysis, UploadedReport  # type: ignore[import-not-found]  # noqa: E402
from app.db.session import control_session  # type: ignore[import-not-found]  # noqa: E402
from app.services import chart_service  # type: ignore[import-not-found]  # noqa: E402

#: 输出目录（`02.data/tmp/`）
OUTPUT_DIR = DATA_DIR / "tmp"

#: plotly 配置：去掉框选/套索（UIUX 7.2「框选缩放默认关闭，避免误操作」）与 logo
PLOTLY_CONFIG: dict[str, Any] = {
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["select2d", "lasso2d"],
    "toImageButtonOptions": {"format": "png", "scale": 2},  # UIUX 7.1：导出 2 倍图
}

#: 结果表预览行数
TABLE_PREVIEW_ROWS = 50


# ══════════════════════════════════════════════════════════════════════════
#  数据读取（直连本地库，不做归属校验：这是本地开发工具）
# ══════════════════════════════════════════════════════════════════════════


async def load_record(record_code: str) -> dict[str, Any]:
    """读取一条分析记录 + 其报表 schema（返回纯 dict，不持有 ORM 对象）。"""
    from sqlalchemy import select

    async with control_session() as session:
        analysis = await session.scalar(
            select(Analysis).where(Analysis.record_code == record_code)
        )
        if analysis is None:
            raise SystemExit(f"分析记录不存在：{record_code}")
        report = (
            await session.get(UploadedReport, analysis.report_id)
            if analysis.report_id
            else None
        )
        schema_columns = None
        if report is not None and report.schema_json:
            try:
                schema_columns = (json.loads(report.schema_json) or {}).get("columns")
            except json.JSONDecodeError:  # pragma: no cover —— 脏数据兜底
                schema_columns = None
        return {
            "record_code": analysis.record_code,
            "user_id": analysis.user_id,
            "question": analysis.question,
            "status": analysis.status,
            "sql": analysis.sql_text,
            "slot": json.loads(analysis.slot_json) if analysis.slot_json else {},
            "columns": json.loads(analysis.result_columns_json) if analysis.result_columns_json else [],
            "rows": json.loads(analysis.result_rows_json) if analysis.result_rows_json else [],
            "row_count": analysis.result_row_count,
            "truncated": bool(analysis.result_truncated),
            "narrative": analysis.narrative,
            "chart_json": analysis.chart_json,
            "created_at": analysis.created_at,
            "elapsed_ms": analysis.elapsed_ms,
            "llm_model": analysis.llm_model,
            "report_code": report.report_code if report else None,
            "report_title": report.filename if report else None,
            "table_name": report.table_name if report else None,
        }


# ══════════════════════════════════════════════════════════════════════════
#  各图对比（全部走 `rerender_chart(persist=False)`，不写库）
# ══════════════════════════════════════════════════════════════════════════


async def _rerender(record: dict[str, Any], **kwargs: Any) -> tuple[dict | None, str | None]:
    """调重绘服务；业务错误（400/404）不抛出而是作为"这张图画不出来"的说明返回。"""
    try:
        payload = await chart_service.rerender_chart(
            record["user_id"], record["record_code"], persist=False, **kwargs
        )
        return payload, None
    except BizError as exc:
        return None, f"{exc.error_code}｜{exc.message}（{(exc.data or {}).get('hint', '')}）"


async def collect(record: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    """组装页面的所有区块：`[{title, note, payload, error}]`。"""
    sections: list[dict[str, Any]] = []

    payload, error = await _rerender(record)
    sections.append({
        "title": "① 自动选型（重生成，不落库）",
        "note": "按 slot.chart_hint 优先、数据形状兜底",
        "payload": payload,
        "error": error,
    })

    stored = None
    if record["chart_json"]:
        stored = {"figure": json.loads(record["chart_json"]), "chart_type": "-", "persisted": True}
    sections.append({
        "title": "② 记录里保存的图（analyses.chart_json）",
        "note": "被 `POST /charts/rerender` 回写过的记录，这里与 ① 不同",
        "payload": stored,
        "error": None if stored else "该记录没有保存图表（未执行、失败，或结果不适合可视化）",
    })

    for chart_type in args.types:
        payload, error = await _rerender(record, chart_type=chart_type)
        sections.append({
            "title": f"③ 类型对比：{chart_service.TYPE_LABELS.get(chart_type, chart_type)}",
            "note": f"chart_type={chart_type}",
            "payload": payload,
            "error": error,
        })

    for granularity in args.granularities:
        payload, error = await _rerender(record, granularity=granularity)
        sections.append({
            "title": f"④ 粒度对比：按「{granularity}」重聚合",
            "note": "从结果快照重算（不改 SQL、不调 LLM）；只能由细变粗",
            "payload": payload,
            "error": error,
        })

    return sections


# ══════════════════════════════════════════════════════════════════════════
#  HTML 组装
# ══════════════════════════════════════════════════════════════════════════

_CSS = """
:root { --line:#D9DEE7; --soft:#EEF1F5; --bg:#F5F7FA; --text:#1F2933; --muted:#6B7280;
        --primary:#1D4ED8; --peak:#E8590C; --wide:#F1F3F5; }
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:var(--bg); color:var(--text);
       font: 14px/1.6 "Microsoft YaHei", "Segoe UI", system-ui, sans-serif; }
header { max-width:1320px; margin:0 auto 16px; }
h1 { font-size:20px; margin:0 0 8px; }
h2 { font-size:16px; margin:0 0 4px; }
.card { max-width:1320px; margin:0 auto 16px; background:#fff; border:1px solid var(--line);
        border-radius:10px; padding:20px; }
.meta { display:grid; grid-template-columns:120px 1fr; gap:6px 12px; color:var(--text); }
.meta dt { color:var(--muted); }
.note { color:var(--muted); font-size:13px; margin:0 0 12px; }
.q { font-weight:600; }
pre { background:var(--wide); border:1px solid var(--soft); border-radius:6px; padding:10px;
      overflow:auto; font-size:12.5px; margin:0; }
table { border-collapse:collapse; width:100%; font-size:12.5px; }
th, td { border:1px solid var(--soft); padding:4px 8px; text-align:left; white-space:nowrap; }
th { background:var(--wide); }
.empty { background:var(--wide); border:1px dashed var(--line); border-radius:6px;
         padding:14px; color:var(--muted); }
.tag { display:inline-block; background:var(--soft); border-radius:999px; padding:1px 10px;
       font-size:12px; margin:0 6px 6px 0; }
.err { color:#B42318; }
summary { cursor:pointer; color:var(--muted); }
"""


def _escape(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value), quote=True)


def _figure_html(figure: dict[str, Any] | None, *, inline_js: bool) -> str:
    """单张图 → HTML 片段；第一张内联 plotly.min.js，其余复用（保持文件自包含）。"""
    if not figure:
        return ""
    import plotly.graph_objects as go
    import plotly.io as pio

    # 后端 figure 不含主题（决策 ⑪），这里交给 plotly 默认模板渲染成亮色底
    return pio.to_html(
        go.Figure(figure),
        include_plotlyjs="inline" if inline_js else False,
        full_html=False,
        config=PLOTLY_CONFIG,
        default_height="520px",
    )


def _summary_html(payload: dict[str, Any]) -> str:
    """把口径标记渲染成标签（merged_count / downsampled_from / gap_count / R3 …）。"""
    tags: list[str] = []
    for key, label in (
        ("chart_type", "类型"), ("unit", "单位"), ("x_axis", "X 轴"),
        ("top_n", "TopN"), ("merged_count", "合并项数"),
        ("downsampled_from", "抽稀前点数"), ("gap_count", "缺口数"),
    ):
        value = payload.get(key)
        if value not in (None, 0, "", []):
            tags.append(f'<span class="tag">{label}：{_escape(value)}</span>')
    if payload.get("r3_adjusted"):
        tags.append('<span class="tag">已按 R3 收敛</span>')
    for key, label in (
        ("dropped_columns", "R3 略去"), ("excluded_columns", "量纲排除"),
    ):
        value = payload.get(key) or []
        if value:
            tags.append(f'<span class="tag">{label}：{_escape("、".join(map(str, value)))}</span>')
    if payload.get("series"):
        tags.append(f'<span class="tag">系列：{_escape("、".join(map(str, payload["series"])))}</span>')
    if payload.get("reaggregated"):
        tags.append('<span class="tag">快照重聚合</span>')
    if payload.get("snapshot_truncated"):
        tags.append('<span class="tag">快照已截断</span>')
    figure = payload.get("figure")
    if figure:
        size = len(json.dumps(figure, ensure_ascii=False).encode("utf-8"))
        tags.append(f'<span class="tag">figure：{size / 1024:.1f} KB</span>')
    return "".join(tags)


def _table_html(record: dict[str, Any]) -> str:
    rows = record["rows"][:TABLE_PREVIEW_ROWS]
    names = [
        (column.get("name") if isinstance(column, dict) else str(column))
        for column in record["columns"]
    ]
    if not names or not rows:
        return '<div class="empty">没有结果数据（未执行 / 执行失败 / 结果为空）。</div>'
    head = "".join(f"<th>{_escape(name)}</th>" for name in names)
    body = "".join(
        "<tr>" + "".join(f"<td>{_escape(row.get(name))}</td>" for name in names) + "</tr>"
        for row in rows
    )
    more = (
        f'<p class="note">共 {record["row_count"]} 行，此处展示前 {len(rows)} 行'
        + ("（结果快照已截断）" if record["truncated"] else "")
        + "。</p>"
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}"


def build_html(record: dict[str, Any], sections: list[dict[str, Any]], args: argparse.Namespace) -> str:
    """拼装自包含 HTML（`plotly.min.js` 只内联一次）。"""
    first_rendered = True
    blocks: list[str] = []
    for section in sections:
        payload = section.get("payload") or {}
        figure = payload.get("figure")
        body = ""
        if figure:
            body = _figure_html(figure, inline_js=first_rendered)
            first_rendered = False
            body += f'<div style="margin-top:8px">{_summary_html(payload)}</div>'
        elif section.get("error"):
            body = f'<div class="empty err">{_escape(section["error"])}</div>'
        elif payload.get("chart_type") == "table":
            body = '<div class="empty">按请求以表格呈现（数据见页面底部「结果表」）。</div>'
        else:
            reason = payload.get("reason") or "没有可绘制的数据"
            body = f'<div class="empty">已降级为表格：{_escape(reason)}</div>'
        blocks.append(
            f'<section class="card"><h2>{_escape(section["title"])}</h2>'
            f'<p class="note">{_escape(section.get("note") or "")}</p>{body}</section>'
        )

    slot = json.dumps(record["slot"], ensure_ascii=False)
    meta = [
        ("问题", f'<span class="q">{_escape(record["question"])}</span>'),
        ("记录", _escape(record["record_code"])),
        ("状态", f'{_escape(record["status"])}（{_escape(record["created_at"])}，{_escape(record["elapsed_ms"])} ms）'),
        ("报表", f'{_escape(record["report_code"])} / {_escape(record["report_title"])} / 表 {_escape(record["table_name"])}'),
        ("行数", f'{_escape(record["row_count"])}{"（截断）" if record["truncated"] else ""}'),
        ("slot", f"<code>{_escape(slot)}</code>"),
        ("SQL", f'<pre>{_escape(record["sql"])}</pre>'),
        ("模型", _escape(record["llm_model"])),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    meta_html = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in meta)
    granularity_note = (
        f'粒度对比：{"、".join(args.granularities)}；' if args.granularities else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>图表预览 · {_escape(record["record_code"])}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1>智电析报 · 图表离线预览</h1>
  <p class="note">本地开发工具（直调 service、不写数据库）：{granularity_note}图表与表格使用同一份结果数据。</p>
</header>
<section class="card">
  <h2>记录信息</h2>
  <dl class="meta">{meta_html}</dl>
</section>
{"".join(blocks)}
<section class="card">
  <h2>结果表（图表同源）</h2>
  {_table_html(record)}
</section>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════
#  命令行入口
# ══════════════════════════════════════════════════════════════════════════


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把一条分析记录的图表渲染成自包含 HTML（离线可看）")
    parser.add_argument("--record", required=True, help="分析记录编号，如 a_000123")
    parser.add_argument(
        "--type", dest="type_text", default="all",
        help=f"要对比的图表类型，逗号分隔；all=全部六种（{'/'.join(chart_service.CHART_TYPES)}）",
    )
    parser.add_argument(
        "--granularity", dest="granularity_text", default="",
        help="要对比的聚合粒度，逗号分隔（小时/日/月/年/全期）；留空则不生成粒度对比",
    )
    parser.add_argument("--out", default=None, help="输出 HTML 路径（默认 02.data/tmp/chart_{record}.html）")
    parser.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    args = parser.parse_args(argv)

    args.types = (
        list(chart_service.CHART_TYPES)
        if args.type_text.strip().lower() == "all"
        else [item.strip() for item in args.type_text.split(",") if item.strip()]
    )
    invalid = [item for item in args.types if item not in chart_service.CHART_TYPES]
    if invalid:
        parser.error(f"未知图表类型：{'、'.join(invalid)}（可选 {'/'.join(chart_service.CHART_TYPES)}）")

    args.granularities = [
        item.strip() for item in args.granularity_text.split(",") if item.strip()
    ]
    invalid = [
        item for item in args.granularities if item not in chart_service.GRANULARITY_FORMATS
    ]
    if invalid:
        parser.error(
            f"未知粒度：{'、'.join(invalid)}"
            f"（可选 {'/'.join(chart_service.GRANULARITY_FORMATS)}）"
        )
    return args


async def run(args: argparse.Namespace) -> int:
    record = await load_record(args.record)
    sections = await collect(record, args)
    out_path = Path(args.out) if args.out else OUTPUT_DIR / f"chart_{record['record_code']}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_html(record, sections, args), encoding="utf-8")

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"记录 {record['record_code']}：{record['question']}")
    print(f"  报表 {record['report_code']} / 表 {record['table_name']}，结果 {record['row_count']} 行")
    for section in sections:
        payload = section.get("payload") or {}
        if payload.get("figure"):
            figure = payload["figure"]
            markers = []
            if payload.get("r3_adjusted"):
                markers.append("R3")
            if payload.get("merged_count"):
                markers.append(f"合并{payload['merged_count']}项")
            if payload.get("downsampled_from"):
                markers.append(f"抽稀自{payload['downsampled_from']}")
            if payload.get("gap_count"):
                markers.append(f"缺口{payload['gap_count']}")
            if payload.get("reaggregated"):
                markers.append("重聚合")
            shape = f"figure {len(json.dumps(figure, ensure_ascii=False)) / 1024:.1f} KB"
            print(
                f"  [ok] {section['title']}：{payload.get('chart_type')}"
                f"（{'、'.join(markers) or '—'}，{shape}）"
            )
        elif section.get("error"):
            print(f"  [--] {section['title']}：{section['error']}")
        elif payload.get("chart_type") == "table":
            print(f"  [--] {section['title']}：按请求以表格呈现（数据见结果表）")
        else:
            print(f"  [--] {section['title']}：已降级为表格（{payload.get('reason')}）")
    print(f"输出：{out_path}（{size_mb:.2f} MB，自包含，双击即看）")

    if args.open and hasattr(os, "startfile"):
        os.startfile(out_path)  # noqa: S606 —— 本地开发工具的便利开关
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
