# -*- coding: utf-8 -*-
"""02_build_schema.py —— 生成 Schema 描述（供大模型理解报表）

产物：
    02.data/schema/<table>.schema.json   （逐表结构化描述）
    02.data/schema/all_schemas.json      （全部报表汇总）
    02.data/schema/schema_text.md        （**注入系统提示词用的自然语言文本**）

设计要点：
    - 字段结构以 `sql/reports.sql` 为唯一事实源（解析 DDL 注释），保证"文档=代码"；
    - 字段的"可否 SUM"由命名规则推导，比率类字段显式标注 `agg: none`（PRD 红线 R6）；
    - 文本中固化 PRD 第 8 章红线规则与 SQLite 可用函数清单（避免模型生成不支持函数）。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

import _common as C

SQL_FILE = C.SQL_DIR / "reports.sql"

CSV_FILES = {
    C.TABLE_HOURLY: "report_hourly.csv",
    C.TABLE_DAILY: "report_daily.csv",
    C.TABLE_MONTHLY: "report_monthly.csv",
    C.TABLE_STRUCTURE: "report_energy_structure.csv",
}

TABLE_TITLES = {
    C.TABLE_MINUTE: "分钟级明细表",
    C.TABLE_HOURLY: "小时报",
    C.TABLE_DAILY: "日报",
    C.TABLE_MONTHLY: "月报",
    C.TABLE_STRUCTURE: "用电结构报表",
}

# SQLite 可用函数清单（PRD 11.1 T8：无 STDDEV_POP，须给出替代写法）
SQLITE_FUNCTIONS = [
    "SUM / AVG / MIN / MAX / COUNT / COUNT(DISTINCT ...)",
    "ROUND(x, n) / ABS(x) / CAST(x AS INTEGER)",
    "SQRT(x) / POWER(x, y) / EXP(x) / LN(x) / LOG10(x)",
    "strftime('%Y', date) / datetime(...) / julianday(...)",
    "CASE WHEN ... THEN ... ELSE ... END / COALESCE(x, 0) / NULLIF(x, 0)",
    "注意：**没有 STDDEV_POP / STDDEV_SAMP / VAR_POP**，标准差请写成 "
    "SQRT(AVG(x*x) - AVG(x)*AVG(x))",
    "注意：**没有 MEDIAN / PERCENTILE**，如需中位数请用 pandas 路径或自行排序取中",
]

RED_LINES = [
    ("R1", "量纲：`*_kwh` 是电量（可相加）；`*_kw` 是功率（**不可相加**，只能求平均/最大/最小）；"
           "`_kwh` 已由 kW÷60、Wh÷1000 统一换算，**不要再次换算**。"),
    ("R2", "未计量用电 `unmetered_kwh` = `gap_kwh` − `sm1_kwh` − `sm2_kwh` − `sm3_kwh`（代表其他电器），"
           "报表已直接提供该列，**不要自行推导公式**。"),
    ("R3", "两套**正交**拆分：按用途（`sm1_kwh` 厨房 / `sm2_kwh` 洗衣房 / `sm3_kwh` 热水空调 / "
           "`unmetered_kwh` 未计量）与按时段（`peak_kwh` 峰 / `flat_kwh` 平 / `valley_kwh` 谷）。"
           "两套各自求和都等于总量，**混加会得到约 2 倍错误结果**，禁止在同一聚合里混用。"),
    ("R4", "`expected_minutes` 是自然时间长度（小时 60 / 日 1440 / 月 = 天数×1440），"
           "与是否有数据无关；`coverage` = `active_minutes` / `expected_minutes`，"
           "**首末周期覆盖率为低值是边界效应，不是异常**。"),
    ("R5", "日均用电 `daily_avg_kwh` = 电量 × 1440 / `active_minutes`（按有效数据折算），"
           "**不可 SUM**；`active_minutes = 0` 时该值为 NULL。"),
    ("R6", "所有比率型字段（`*_ratio`、`coverage`、`load_factor`、`mom_rate`、`yoy_rate`）"
           "**不可 SUM**——跨周期汇总必须「分子求和 ÷ 分母求和」重算。"),
    ("R7", "结构报表中 `period = '全期'` 的行（`is_total_row = 1`）是各年份行的合计，"
           "**求和时绝不可与年份行一起 SUM**（否则结果翻倍）。"),
]

SAMPLE_QUESTIONS = [
    "全期总用电量是多少？",
    "哪个月用电量最高？",
    "峰段用电占比多少？",
    "厨房用电占总用电的比例是多少？",
    "有哪些用电异常日？",
    "周末与工作日的日均用电对比如何？",
    "2010 年比 2009 年用电增长多少？",
    "未计量用电有多少？",
]


# --------------------------------------------------------------------------------------
def parse_ddl(path: Path) -> dict[str, dict]:
    """解析 reports.sql，抽取表描述与字段（名称/类型/注释）。"""
    tables: dict[str, dict] = {}
    current: dict | None = None
    last_desc = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        desc = re.match(r"--\s*\d+\.\s*(\w+)\s*——\s*(.+)", stripped)
        if desc:
            last_desc = desc.group(2).strip()
            continue

        create = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", stripped)
        if create:
            current = {"name": create.group(1), "description": last_desc, "columns": []}
            tables[current["name"]] = current
            continue

        if current is not None:
            if stripped.startswith(")"):
                current = None
                continue
            col = re.match(r"([a-z_][a-z0-9_]*)\s+([A-Z]+)", stripped)
            if col and col.group(1) not in {"CHECK", "UNIQUE"}:
                comment = ""
                if "--" in line:
                    comment = line.split("--", 1)[1].strip()
                current["columns"].append(
                    {"name": col.group(1), "type": col.group(2), "comment": comment}
                )
    return tables


def unit_of(col: str) -> str:
    if col.endswith("_kwh"):
        return "kWh"
    if col.endswith("_kw"):
        return "kW"
    if col.endswith("_ratio") or col in {"coverage", "load_factor", "mom_rate", "yoy_rate"}:
        return "比率"
    return {
        "voltage": "V", "avg_voltage": "V",
        "intensity": "A", "avg_intensity": "A",
        "expected_minutes": "分钟", "active_minutes": "分钟",
        "peak_hour": "整点(0-23)", "weekday": "0=周一",
        "days_in_month": "天",
    }.get(col, "—")


def is_summable(col: str) -> bool | None:
    """可否 SUM：电量类可以；功率/比率/维度列不可以（None 表示非度量维度）。"""
    if col.endswith("_kwh"):
        return True
    if col.endswith("_kw") or col.endswith("_ratio"):
        return False
    if col in {"coverage", "load_factor", "mom_rate", "yoy_rate",
               "active_minutes", "expected_minutes", "days_in_month",
               "peak_hour", "is_anomaly", "is_partial_period", "is_missing",
               "id", "year", "month", "day", "hour", "weekday", "is_weekend"}:
        return False
    if col in {"structure_closed", "time_closed"}:
        return False
    return None


def stats_for(table: str, column: str, df: pd.DataFrame | None) -> dict:
    """取示例值/范围/缺失数（分钟表从质量报告取，避免读 207 万行 CSV）。"""
    info: dict = {}
    if df is not None and column in df.columns:
        series = df[column]
        info["null_count"] = int(series.isna().sum())
        if pd.api.types.is_numeric_dtype(series):
            info["min"] = round(float(series.min()), 6) if series.notna().any() else None
            info["max"] = round(float(series.max()), 6) if series.notna().any() else None
        sample = series.dropna().head(3).tolist()
        info["sample"] = sample
    return info


def build_table_schema(table: str, ddl: dict, df: pd.DataFrame | None, quality: dict) -> dict:
    columns = []
    for col in ddl["columns"]:
        name = col["name"]
        if name == "id":
            continue
        columns.append(
            {
                "name": name,
                "type": col["type"],
                "unit": unit_of(name),
                "agg": "sum" if is_summable(name) is True
                else ("none" if is_summable(name) is False else "—"),
                "description": col["comment"],
                **stats_for(table, name, df),
            }
        )

    row_count = None
    time_range = None
    if df is not None:
        row_count = int(len(df))
        if "date" in df.columns:
            time_range = [str(df["date"].min()), str(df["date"].max())]
        elif "dt_hour" in df.columns:
            time_range = [str(df["dt_hour"].min()), str(df["dt_hour"].max())]
        elif "year_month" in df.columns:
            time_range = [str(df["year_month"].min()), str(df["year_month"].max())]
    else:
        row_count = quality.get("cleaned_rows")
        time_range = [quality.get("time_start"), quality.get("time_end")]

    return {
        "table": table,
        "title": TABLE_TITLES.get(table, table),
        "description": ddl["description"],
        "caliber_version": C.CALIBER_VERSION,
        "row_count": row_count,
        "time_range": time_range,
        "columns": columns,
    }


def render_text(schemas: dict[str, dict], quality: dict) -> str:
    """生成注入提示词的 Schema 文本。"""
    lines: list[str] = []
    lines.append("# 智电析报 · 报表 Schema 说明（供分析助手使用）")
    lines.append("")
    lines.append(f"- 口径版本：`{C.CALIBER_VERSION}`")
    lines.append(f"- 数据来源：UCI Individual Household Electric Power Consumption（#235），"
                 f"{quality.get('time_start')} → {quality.get('time_end')}")
    lines.append(f"- 数据规模：{quality.get('cleaned_rows'):,} 分钟行；"
                 f"缺失 {quality.get('missing_rows'):,} 行（{quality.get('missing_ratio', 0) * 100:.2f}%），"
                 f"时间轴连续（缺口 {quality.get('time_axis_gap')} 分钟）")
    lines.append(f"- 时段口径：谷 00:00–07:59 / 平 08:00–17:59 / 峰 18:00–23:59（左闭右开）")
    lines.append("")

    lines.append("## 一、可用报表（表）")
    lines.append("")
    for table, schema in schemas.items():
        lines.append(f"### {table} — {schema['title']}")
        lines.append(f"- 说明：{schema['description']}")
        lines.append(f"- 行数：{schema['row_count']:,}" if schema["row_count"] else "- 行数：—")
        if schema.get("time_range"):
            lines.append(f"- 时间范围：{schema['time_range'][0]} → {schema['time_range'][1]}")
        lines.append("")
        lines.append("| 字段 | 类型 | 单位 | 可否 SUM | 说明 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for col in schema["columns"]:
            agg = {"sum": "是", "none": "**否**"}.get(col["agg"], "—")
            lines.append(
                f"| `{col['name']}` | {col['type']} | {col['unit']} | {agg} | {col['description']} |"
            )
        lines.append("")

    lines.append("## 二、口径红线（必须遵守，违反即答案错误）")
    lines.append("")
    for code, text in RED_LINES:
        lines.append(f"- **{code}**：{text}")
    lines.append("")

    lines.append("## 三、SQLite 可用函数（生成 SQL 时只能用这些）")
    lines.append("")
    for fn in SQLITE_FUNCTIONS:
        lines.append(f"- {fn}")
    lines.append("")

    lines.append("## 四、常用问题示例")
    lines.append("")
    for q in SAMPLE_QUESTIONS:
        lines.append(f"- {q}")
    lines.append("")
    lines.append("> 生成回答时请：① 明确使用哪张表与哪个时段口径；② 数值带单位；"
                 "③ 跨周期汇总比率须重算（R6）；④ 首末周期不完整需提示（`is_partial_period`）。")
    return "\n".join(lines)


def main() -> int:
    logger = C.setup_logger("02_build_schema")
    if not SQL_FILE.exists():
        logger.error("找不到 DDL 文件：%s", SQL_FILE)
        return 1

    quality_file = C.QUALITY_DIR / "quality_report.json"
    if not quality_file.exists():
        logger.error("缺少质量报告，请先运行 01_build_reports.py")
        return 1
    quality = json.loads(quality_file.read_text(encoding="utf-8"))

    ddl_tables = parse_ddl(SQL_FILE)
    logger.info("从 DDL 解析到 %d 张表：%s", len(ddl_tables), ", ".join(ddl_tables))

    schemas: dict[str, dict] = {}
    for table in [C.TABLE_MINUTE, C.TABLE_HOURLY, C.TABLE_DAILY, C.TABLE_MONTHLY, C.TABLE_STRUCTURE]:
        if table not in ddl_tables:
            logger.warning("DDL 中缺少表 %s，跳过", table)
            continue
        df = None
        csv_name = CSV_FILES.get(table)
        if csv_name and (C.REPORTS_DIR / csv_name).exists():
            df = pd.read_csv(C.REPORTS_DIR / csv_name, encoding=C.CSV_ENCODING,
                             dtype={c: "float64" for c in ["consumption_kwh"]} )
            logger.info("读取 %s（%d 行）用于统计", csv_name, len(df))
        schemas[table] = build_table_schema(table, ddl_tables[table], df, quality)

    C.SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    for table, schema in schemas.items():
        (C.SCHEMA_DIR / f"{table}.schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    all_schemas = {
        "caliber_version": C.CALIBER_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "data_source": "UCI #235 Individual Household Electric Power Consumption",
        "red_lines": [{"code": c, "rule": t} for c, t in RED_LINES],
        "sqlite_functions": SQLITE_FUNCTIONS,
        "sample_questions": SAMPLE_QUESTIONS,
        "tables": schemas,
    }
    (C.SCHEMA_DIR / "all_schemas.json").write_text(
        json.dumps(all_schemas, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = render_text(schemas, quality)
    (C.SCHEMA_DIR / "schema_text.md").write_text(text, encoding="utf-8")

    logger.info("已生成 %d 份 schema.json + all_schemas.json + schema_text.md（%d 字符）",
                len(schemas), len(text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
