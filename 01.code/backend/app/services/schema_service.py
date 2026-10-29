"""智电析报 · Schema 模块（交付物 3）：注入文本的生成、组装与预算控制。

对应文档：
- PRD F1.4（Schema 描述文本生成的六段结构）、9.1（`GET /system/schema-text`）、3.3（Schema 注入为前置步骤）
- 数据库设计 4.2（`schema_json` 结构与写入方）
- UIUX 5.3 区域 4（数据字典抽屉：「该文本与注入给模型的说明逐字一致」）

两条产出路径：

| 报表来源 | 文本来源 | 说明 |
| --- | --- | --- |
| 预置 5 张 | `02.data/schema/schema_text.md`（**原文**） | 组装 = 文件全局头 + 该表段落 + 「二、口径红线」+「三、SQLite 可用函数」+「四、常用问题示例」+ 使用提示；**逐字取自交付物原文**，不重新渲染 |
| 用户上传 | 由 `schema_json` + `quality_json` **生成** | 补齐 F1.4 的另外四段：通用红线（含**口径未知声明**）、SQLite 函数清单、常用问题示例（列名启发式）、使用提示；生成结果随 `schema_json["injection_text"]` 落库（零 DDL） |

预算（本轮决策 3）：按**字符启发式**估算 token（中文字符 1 token、其余 4 字符 1 token），阈值 2000；
超预算时按**固定顺序**降级（字段说明截断 → 示例值只留 1 个 → 示例问题减少到 3 条），
**字段清单骨架（字段名/类型/单位/可否 SUM）永不裁剪**，并在响应里回显 `est_tokens` / `truncated`。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import forbidden, not_found, validation_error
from app.db.models import Conversation, UploadedReport

logger = logging.getLogger("zdxb.schema")

__all__ = [
    "EST_TOKEN_BUDGET",
    "INJECTION_TEXT_KEY",
    "SYSTEM_DEFAULT_SOURCE",
    "build_upload_schema_text",
    "ensure_injection_text",
    "estimate_tokens",
    "render_injection_text",
    "resolve_active_report",
]

#: 单表注入文本的 token 预算（PRD F1.4：≤2000 tokens）
EST_TOKEN_BUDGET = 2000

#: `schema_json` 中存放生成文本的键（零 DDL：JSON 内新增键）
INJECTION_TEXT_KEY = "injection_text"

#: `/system/schema-text` 的取值来源标记
CONVERSATION_SOURCE = "conversation"
SYSTEM_DEFAULT_SOURCE = "system_default"

#: 字段说明在降级时截断到的长度
_DESCRIPTION_LIMIT = 40

#: 示例问题：最多保留条数 / 降级后保留条数 / 无匹配时的兜底条数
_MAX_QUESTIONS = 6
_MIN_QUESTIONS = 3

#: 中文字符区间（用于 token 估算）
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")


# ── token 估算与预算 ────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """字符启发式估算 token 数（无新增依赖；Qwen 分词与 tiktoken 本就不同，用 2 倍余量兜底）。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return int(cjk + (len(text) - cjk) / 4)


# ── 上传表：注入文本生成（F1.4 六段）────────────────────────────────────────

#: 常见电力/通用列名特征 → 推荐问题模板（按序匹配，命中即取，最多 `_MAX_QUESTIONS` 条）
QUESTION_TEMPLATES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("peak", "flat", "valley", "峰", "平", "谷"), "峰段、平段、谷段各自占比多少？"),
    (("total", "consumption", "kwh", "电量", "电能"), "全期总用电量是多少？"),
    (("power", "功率", "kw"), "最大功率与平均功率分别是多少？"),
    (("voltage", "电压"), "电压越界的情况如何？"),
    (("current", "电流"), "电流的最大值与平均值是多少？"),
    (("ratio", "rate", "coverage", "占比", "比例", "率"), "各时段的占比情况如何？"),
    (("unmetered", "未计量"), "未计量用电量有多少？"),
    (("sm1", "sm2", "sm3", "kitchen", "laundry", "分表", "厨房", "洗衣"), "各分表/设备的用电排名如何？"),
    (("weekday", "weekend", "星期", "周末"), "周末与工作日的用电有什么差异？"),
    (("hour", "小时"), "日内各小时的用电曲线如何？"),
)

#: 时间列通用问题（有时间列就一定给）
_TIME_QUESTIONS = (
    "本表的时间范围与总行数是多少？",
    "按月汇总看用电趋势如何？",
    "哪个月（或哪一天）的用电量最高？",
)

#: 无任何特征命中时的兜底问题
FALLBACK_QUESTIONS = (
    "本表的时间范围与总行数是多少？",
    "数据中缺失值最多的是哪些列？",
    "哪一列的数值波动最大？",
)

#: SQLite 可用函数清单（生成 SQL 时的白名单；与 `schema_text.md` 第三节一致）
SQLITE_FUNCTIONS_BLOCK = """## 三、SQLite 可用函数（生成 SQL 时只能用这些）

- SUM / AVG / MIN / MAX / COUNT / COUNT(DISTINCT ...)
- ROUND(x, n) / ABS(x) / CAST(x AS INTEGER)
- SQRT(x) / POWER(x, y) / EXP(x) / LN(x) / LOG10(x)
- strftime('%Y', dt) / datetime(...) / julianday(...)
- CASE WHEN ... THEN ... ELSE ... END / COALESCE(x, 0) / NULLIF(x, 0)
- 注意：**没有 STDDEV_POP / STDDEV_SAMP / VAR_POP**，标准差请写成 SQRT(AVG(x*x) - AVG(x)*AVG(x))
- 注意：**没有 MEDIAN / PERCENTILE**，如需中位数请用 pandas 路径或自行排序取中"""


def build_upload_schema_text(
    schema: dict[str, Any],
    quality: dict[str, Any] | None = None,
    *,
    verbosity: int = 0,
) -> str:
    """为**上传的动态表**生成与预置表同构的注入文本（F1.4 六段）。

    `verbosity` 为预算降级层级（0 原始 → 3 最简），由 `build_injection_text` 按需递增。
    """
    schema = schema or {}
    dataset = (quality or {}).get("dataset") or {}
    columns = list(schema.get("columns") or [])
    table = schema.get("table") or ""
    title = schema.get("title") or ""
    time_range = schema.get("time_range") or []

    lines: list[str] = [
        "# 智电析报 · 报表 Schema 说明（供分析助手使用）",
        "",
        "- 口径版本：**未应用本项目口径**（用户上传数据）",
        f"- 数据来源：用户上传文件 `{title}`",
        f"- 数据规模：{_fmt(schema.get('row_count'))} 行 × {len(columns)} 列",
        f"- 时间范围：{_range_text(time_range)}",
        "",
        "## 一、可用报表（表）",
        "",
        f"### {table} — {title or '用户上传报表'}",
        f"- 说明：用户上传的用电报表，已按系统规则规范列名并派生时间维度列",
        f"- 行数：{_fmt(schema.get('row_count'))}",
        f"- 时间范围：{_range_text(time_range)}",
        "",
        "| 字段 | 类型 | 单位 | 可否 SUM | 说明 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for column in columns:
        lines.append(
            "| `{name}` | {type} | {unit} | {agg} | {desc} |".format(
                name=column.get("name", ""),
                type=column.get("type", "TEXT"),
                unit=column.get("unit") or "—",
                agg={"sum": "是", "none": "**否**"}.get(column.get("agg") or "", "—"),
                desc=_column_description(column, verbosity),
            )
        )

    lines += ["", "## 二、口径红线规则（本表专属，务必遵守）", ""]
    lines += _upload_red_lines(columns, dataset)

    lines += ["", SQLITE_FUNCTIONS_BLOCK, ""]

    lines += ["## 四、常用问题示例（针对本表）", ""]
    limit = _MIN_QUESTIONS if verbosity >= 3 else _MAX_QUESTIONS
    for question in _sample_questions([c.get("name", "") for c in columns])[:limit]:
        lines.append(f"- {question}")

    lines += [
        "",
        "> 生成回答时请：① 明确使用哪张表与哪个字段；② 数值带单位；③ 比率型字段跨周期汇总须"
        "「分子求和 ÷ 分母求和」重算；④ **本表口径未知**，不得声称套用了任何项目口径，也不得引用预置报表的"
        "红线条款；⑤ 结论如受缺失/重复/恒定值影响，需在解读中说明。",
    ]
    return "\n".join(lines)


def _upload_red_lines(columns: list[dict[str, Any]], dataset: dict[str, Any]) -> list[str]:
    """上传表红线段：口径未知声明 + 比率列清单 + 时间列用法 + 本表质量提示 + 单位量纲。"""
    # 比率/均值型字段：数值型且 agg=none，且**排除系统派生的维度列**
    # （`derived` 由 upload_parse 写入 schema_json；老报表无该键时按列名兜底）
    ratio_columns = [
        column["name"]
        for column in columns
        if column.get("agg") == "none"
        and column.get("type") in ("REAL", "INTEGER")
        and not _is_dimension_column(column)
    ]
    # 单位量纲只对"用户真实的数值列"有意义：派生维度列（如 weekday 的 `0=周一`）不算单位
    unit_columns = [
        f"`{column['name']}`（{column['unit']}）"
        for column in columns
        if column.get("unit")
        and column["unit"] != "—"
        and column.get("type") in ("REAL", "INTEGER")
        and not _is_dimension_column(column)
    ]
    time_column = next((column["name"] for column in columns if column.get("name") == "dt"), None)

    lines = [
        "- **U1（口径未知）**：本表为用户上传数据，**未经本项目口径清洗**——峰平谷时段划分、未计量用电口径、"
        "异常/越界判定阈值均**未应用**。请勿套用预置报表的红线 R1–R7，也不要在解读中声称使用了任何"
        "\u201c口径\u201d或\u201c标准口径\u201d。",
    ]
    if ratio_columns:
        lines.append(
            "- **U2（不可求和的列）**：" + "、".join(f"`{name}`" for name in ratio_columns)
            + " 为比率/均值型字段，**跨周期汇总不可直接 SUM**，必须「分子求和 ÷ 分母求和」重算。"
        )
    if time_column:
        lines.append(
            f"- **U3（时间列）**：唯一标准时间戳是 `{time_column}`（TEXT，`'YYYY-MM-DD HH:MM:SS'`，可直接按"
            "字符串比较做范围过滤）；`year`/`month`/`day`/`hour`/`weekday`/`is_weekend` 是**派生的维度列**，"
            "只可用于分组与过滤，**不可 SUM**。"
        )
    hints: list[str] = []
    if dataset:
        hints.append(
            f"共 {_fmt(dataset.get('total_rows'))} 行；整行缺失 {_fmt(dataset.get('missing_rows'))} 行"
            f"（部分列缺失 {_fmt(dataset.get('partially_missing_rows'))} 行）"
        )
        if dataset.get("duplicate_timestamps"):
            hints.append(
                f"**存在 {_fmt(dataset['duplicate_timestamps'])} 个重复时间戳**，按时间聚合前需确认是否去重"
            )
        if dataset.get("time_axis_gap"):
            hints.append(f"时间轴有 {_fmt(dataset['time_axis_gap'])} 个缺口时间点")
        if dataset.get("coverage") is not None:
            hints.append(f"时间覆盖率 {dataset['coverage']}")
        constant = [
            name
            for name in dataset.get("constant_columns") or []
            # 时间列与派生维度列天然"恒定"于短区间（如一天内 date 只有一个值），不算异常
            if name not in _TIME_LIKE_NAMES and name != "dt"
        ]
        if constant:
            hints.append(
                "恒定为单值的列：" + "、".join(f"`{name}`" for name in constant)
                + "（可能是无效列或区间内无数据，结论需谨慎）"
            )
        if dataset.get("voltage_out_of_range_rows"):
            hints.append(f"电压越界 {_fmt(dataset['voltage_out_of_range_rows'])} 行")
        if dataset.get("negative_power_rows"):
            hints.append(f"功率为负 {_fmt(dataset['negative_power_rows'])} 行")
    if hints:
        lines.append("- **U4（本表特有的质量提示）**：" + "；".join(hints) + "。")
    if unit_columns:
        lines.append(
            "- **U5（单位与量纲）**：带单位的列有 " + "、".join(unit_columns)
            + "，跨列运算前请先对齐单位（不同单位不可直接相加）。"
        )
    return lines


def _sample_questions(names: list[str]) -> list[str]:
    """示例问题：按列名特征**筛选**模板（决策 4），最多 `_MAX_QUESTIONS` 条，无特征命中则给兜底。"""
    haystack = " ".join(name.lower() for name in names if name)
    if not haystack:
        return list(FALLBACK_QUESTIONS)
    questions: list[str] = list(_TIME_QUESTIONS)
    for keywords, question in QUESTION_TEMPLATES:
        if any(keyword in haystack for keyword in keywords) and question not in questions:
            questions.append(question)
    if len(questions) == len(_TIME_QUESTIONS):
        # 除时间列外没有任何电力特征命中 → 补通用兜底
        questions += [question for question in FALLBACK_QUESTIONS if question not in questions]
    return questions[:_MAX_QUESTIONS]


def _is_dimension_column(column: dict[str, Any]) -> bool:
    """是否系统派生的维度/时间列（`derived` 标记为主，列名兜底兼容老数据）。"""
    name = (column.get("name") or "").lower()
    return bool(column.get("derived")) or name in _DIMENSION_COLUMN_NAMES


#: 派生的时间/维度列名（兜底：老报表的 `schema_json` 没有 `derived` 键）
_DIMENSION_COLUMN_NAMES = frozenset(
    {"dt", "year", "month", "day", "hour", "weekday", "is_weekend", "year_month"}
)

#: 时间类列名（恒定值告警需排除）
_TIME_LIKE_NAMES = frozenset({"date", "time", "timestamp", "datetime", "year_month"})


def _column_description(column: dict[str, Any], verbosity: int) -> str:
    """字段说明：原始列名 + 说明 + 样例；降级时截断长度（骨架的字段名/类型/单位不裁剪）。"""
    parts: list[str] = []
    origin = column.get("origin_name")
    description = (column.get("description") or "").replace("|", "/").replace("\n", " ")
    # 说明里已经带了源列名（`upload_parse` 写入的 `源列名：X（词典映射）`）→ 不再重复前缀
    if origin and origin != column.get("name") and origin not in description:
        parts.append(f"源列名 {origin}")
    if description:
        parts.append(description)
    samples = column.get("sample") or []
    if verbosity >= 2:
        samples = samples[:1]
    if samples:
        parts.append("样例 " + ", ".join(str(value) for value in samples))
    text = "；".join(part for part in parts if part)
    if verbosity >= 1:
        text = text[:_DESCRIPTION_LIMIT]
    return text


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _range_text(time_range: Any) -> str:
    if isinstance(time_range, (list, tuple)) and len(time_range) == 2:
        return f"{time_range[0]} → {time_range[1]}"
    return "—"


# ── 预置表：从交付物原文组装 ────────────────────────────────────────────────

def _split_schema_text(markdown: str) -> tuple[str, dict[str, str], str]:
    """把 `schema_text.md` 切成（全局头, {表名: 段落}, 尾部共用段）。

    共用段 = 从 `## 二、` 起的全部内容（口径红线 / SQLite 函数 / 常用问题示例 / 使用提示）。
    """
    lines = markdown.splitlines()
    preamble: list[str] = []
    tables: dict[str, str] = {}
    shared: list[str] = []
    current: list[str] | None = None
    current_name: str | None = None
    in_shared = False

    for line in lines:
        if line.startswith("## 二、") or line.startswith("## 二 "):
            in_shared = True
            current, current_name = None, None
        if in_shared:
            shared.append(line)
            continue
        match = re.match(r"^### (\w[\w\d]*)", line)
        if match:
            if current is not None and current_name:
                tables[current_name] = "\n".join(current).strip()
            current, current_name = [line], match.group(1)
            continue
        if current is not None:
            current.append(line)
        else:
            preamble.append(line)
    if current is not None and current_name and not in_shared:
        tables[current_name] = "\n".join(current).strip()
    return "\n".join(preamble).strip(), tables, "\n".join(shared).strip()


def _preset_injection_text(table_name: str) -> tuple[str, str] | None:
    """预置表注入文本：全局头 + 本表段落 + 共用段（逐字取自 `schema_text.md`）。"""
    path = get_settings().schema_text_path
    if not path.exists():
        return None
    preamble, tables, shared = _split_schema_text(path.read_text(encoding="utf-8"))
    section = tables.get(table_name)
    if not section:
        return None
    parts = [preamble, section, shared]
    return "\n\n".join(part for part in parts if part).strip(), "schema_text.md"


# ── 统一入口 ────────────────────────────────────────────────────────────────

def _minimal_schema(report: UploadedReport) -> dict[str, Any]:
    """`schema_json` 缺失时的最小结构化描述（只用元数据列，不查物理表）。

    兜底场景：老报表（本次升级前上传，`schema_json` 尚未落库）或元数据未回填的预置报表——
    此时注入文本仍应给出正确的**表名与范围**，而不是一个空表名的骨架。
    """
    return {
        "table": report.table_name,
        "title": report.filename or report.table_name,
        "caliber_version": report.caliber_version,
        "row_count": report.row_count,
        "time_range": [report.time_start, report.time_end],
        "columns": [],
    }


def render_injection_text(report: UploadedReport) -> dict[str, Any]:
    """**纯函数**：产出某报表的注入文本（含预算控制），返回 `{text, text_source, est_tokens, truncated}`。

    - 预置报表：取 `schema_text.md` 原文组装（不做裁剪，交付物原文即权威）；
    - 上传报表：优先取 `schema_json["injection_text"]`（upload 时已生成），缺失则现场生成（**不落库**）。
    """
    if report.is_preset:
        preset = _preset_injection_text(report.table_name)
        if preset is not None:
            text, source = preset
            return {
                "text": text,
                "text_source": source,
                "est_tokens": estimate_tokens(text),
                "truncated": False,
            }

    schema = json.loads(report.schema_json) if report.schema_json else _minimal_schema(report)
    quality = json.loads(report.quality_json) if report.quality_json else {}
    cached = schema.get(INJECTION_TEXT_KEY)
    if isinstance(cached, str) and cached.strip():
        return {
            "text": cached,
            "text_source": "generated",
            "est_tokens": schema.get("injection_tokens") or estimate_tokens(cached),
            "truncated": bool(schema.get("injection_truncated")),
        }

    text, truncated = fit_to_budget(schema, quality)
    return {
        "text": text,
        "text_source": "generated",
        "est_tokens": estimate_tokens(text),
        "truncated": truncated,
    }


async def ensure_injection_text(session: AsyncSession, report: UploadedReport) -> dict[str, Any]:
    """在**有活动 session** 的路径上取注入文本，顺带把现场生成的结果回写 `schema_json`。

    兼容本步之前上传的报表（其 `schema_json` 里没有 `injection_text`）：首次访问即补齐，
    避免"老报表永远只有简易回退文本"。
    """
    payload = render_injection_text(report)
    if payload["text_source"] != "generated" or not report.schema_json:
        return payload
    schema = json.loads(report.schema_json)
    if isinstance(schema.get(INJECTION_TEXT_KEY), str) and schema[INJECTION_TEXT_KEY].strip():
        return payload
    schema[INJECTION_TEXT_KEY] = payload["text"]
    schema["injection_tokens"] = payload["est_tokens"]
    schema["injection_truncated"] = payload["truncated"]
    report.schema_json = json.dumps(schema, ensure_ascii=False)
    await session.flush()
    return payload


def fit_to_budget(schema: dict[str, Any], quality: dict[str, Any] | None) -> tuple[str, bool]:
    """按固定顺序降级直到进入 token 预算；返回（文本, 是否发生了裁剪）。"""
    text = build_upload_schema_text(schema, quality, verbosity=0)
    if estimate_tokens(text) <= EST_TOKEN_BUDGET:
        return text, False
    for verbosity in (1, 2, 3):
        text = build_upload_schema_text(schema, quality, verbosity=verbosity)
        if estimate_tokens(text) <= EST_TOKEN_BUDGET:
            logger.info("注入文本超预算，已降级到 verbosity=%d", verbosity)
            return text, True
    logger.warning("注入文本降级到最简仍超预算（列数 %d）", len(schema.get("columns") or []))
    return text, True


async def resolve_active_report(
    session: AsyncSession, user_id: int, conversation_code: str | None
) -> tuple[UploadedReport, str]:
    """确定「当前活跃报表」（决策 2）：会话优先，缺省回落**系统默认活跃报表**。

    返回（报表, 来源标记）。
    """
    if conversation_code:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.conversation_code == conversation_code)
        )
        if conversation is None:
            raise not_found(f"会话不存在：{conversation_code}")
        if conversation.user_id != user_id:
            raise forbidden()
        if conversation.active_report_id is None:
            raise validation_error(
                f"会话 {conversation_code} 尚未选择活跃报表，请先调用 "
                "POST /reports/{report_code}/activate"
            )
        report = await session.get(UploadedReport, conversation.active_report_id)
        if report is None or report.status == "deleted":
            raise not_found("会话的活跃报表已被删除，请重新选择报表")
        return report, CONVERSATION_SOURCE

    report = await session.scalar(
        select(UploadedReport)
        .where(UploadedReport.is_preset == 1, UploadedReport.is_active == 1)
        .order_by(UploadedReport.id)
    )
    if report is None:
        raise not_found("系统默认活跃报表未配置（uploaded_reports.is_active = 1）")
    return report, SYSTEM_DEFAULT_SOURCE
