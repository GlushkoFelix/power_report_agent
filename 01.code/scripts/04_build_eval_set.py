# -*- coding: utf-8 -*-
"""智电析报 · 评测题库构建（交付物 8 / M5，PRD F5.1）。

**题库是脚本产物，不是手写数据**：题面按 6 类题型（`stat`/`agg`/`compare`/`anomaly`/`structure`/
`multi_turn`）× 3 难度批量生成，每题的 `gold_sql` 由本脚本写定并**对真实 `reports.db` 只读执行**，
把结果集存 `gold_answer_json`、关键数值存 `key_numbers_json`。

为什么"真库反算 gold"：手写 gold 会出错，而 gold 一错就会把正确回答判成错误、污染正确率统计
（且极难发现）。脚本化后口径改了只需重跑本脚本即完成回归。

产物：
1. `02.data/eval/eval_questions.json` —— 题库快照（可入档、可 diff、评审证据）；
2. `power_agent.db::eval_questions` —— 题库入库（按 `question_code` 全量重建，幂等）。

题目约定：
- 题量 120 题，覆盖 6 类题型，陷阱题 18 题（15%，覆盖红线 R1/R3/R6/R7）；
- **多轮题**：`question` 内两轮以换行分隔（第一轮在前），跑批时顺序提问、**判分只看最后一轮**；
- `report_table` 显式登记目标报表（跑批时按它激活会话活跃报表，不做 SQL 反解）。

用法：
    python 04_build_eval_set.py            # 构建 + 写 JSON + 入库
    python 04_build_eval_set.py --check    # 只校验分布与 gold 可执行性（不入库）
    python 04_build_eval_set.py --limit 10 # 只构建前 10 题（调试用）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _common as C  # noqa: E402  —— 复用数据层路径与报表常量（单一来源）

EVAL_DIR = C.DATA_DIR / "eval"
QUESTIONS_JSON = EVAL_DIR / "eval_questions.json"

TARGET_TOTAL = 120
YEARS = (2006, 2007, 2008, 2009, 2010)

DAILY = C.TABLE_DAILY
MONTHLY = C.TABLE_MONTHLY
HOURLY = C.TABLE_HOURLY
STRUCTURE = C.TABLE_STRUCTURE


def _q(
    qtype: str,
    difficulty: str,
    question: str,
    table: str,
    sql: str,
    keys: list[dict],
    *,
    trap: str | None = None,
    tags: str = "",
) -> dict:
    """一条题目定义（`keys` 为关键数值抽取规则，见 `extract_key_numbers`）。"""
    return {
        "qtype": qtype,
        "difficulty": difficulty,
        "question": question,
        "report_table": table,
        "gold_sql": sql,
        "key_specs": keys,
        "is_trap": 1 if trap else 0,
        "trap_rule": trap,
        "tags": tags,
    }


# ══════════════════════════════════════════════════════════════════════════
#  ① stat —— 单值 / 极值查询（25 题；陷阱 5：R1×3、R3×1、R6×1）
# ══════════════════════════════════════════════════════════════════════════

def _stat() -> list[dict]:
    items = [
        _q("stat", "easy", "全期总用电量是多少？", DAILY,
           "SELECT SUM(consumption_kwh) AS total_kwh FROM report_daily",
           [{"kind": "value", "column": "total_kwh", "label": "全期总用电量", "unit": "kWh"}]),
        _q("stat", "easy", "全期平均每天用多少电？", DAILY,
           "SELECT AVG(consumption_kwh) AS avg_kwh FROM report_daily",
           [{"kind": "value", "column": "avg_kwh", "label": "平均日用电量", "unit": "kWh"}]),
        _q("stat", "medium", "全期用电量最高的那一天是哪天，用了多少电？", DAILY,
           "SELECT date, consumption_kwh FROM report_daily ORDER BY consumption_kwh DESC LIMIT 1",
           [{"kind": "value", "column": "date", "label": "最高日日期", "unit": ""},
            {"kind": "value", "column": "consumption_kwh", "label": "最高日电量", "unit": "kWh"}]),
        _q("stat", "medium", "全期用电量最低的那一天是哪天，用了多少电？", DAILY,
           # 极值题必须排除 NULL：SQLite 下 ASC 时 NULL 排最前，"最低"会选中缺失日（2026-09-18 修订）
           "SELECT date, consumption_kwh FROM report_daily WHERE consumption_kwh IS NOT NULL "
           "ORDER BY consumption_kwh ASC LIMIT 1",
           [{"kind": "value", "column": "date", "label": "最低日日期", "unit": ""},
            {"kind": "value", "column": "consumption_kwh", "label": "最低日电量", "unit": "kWh"}]),
        _q("stat", "easy", "日报里一共记录了多少天？", DAILY,
           "SELECT COUNT(*) AS days FROM report_daily",
           [{"kind": "value", "column": "days", "label": "日报天数", "unit": "天"}]),
        _q("stat", "easy", "月报里一共记录了多少个月？", MONTHLY,
           "SELECT COUNT(*) AS months FROM report_monthly",
           [{"kind": "value", "column": "months", "label": "月报月数", "unit": "月"}]),
        _q("stat", "easy", "全期未计量用电量合计是多少？", DAILY,
           "SELECT SUM(unmetered_kwh) AS unmetered_kwh FROM report_daily",
           [{"kind": "value", "column": "unmetered_kwh", "label": "未计量用电量", "unit": "kWh"}]),
        _q("stat", "easy", "全期峰段用电量合计是多少？", DAILY,
           "SELECT SUM(peak_kwh) AS peak_kwh FROM report_daily",
           [{"kind": "value", "column": "peak_kwh", "label": "峰段用电量", "unit": "kWh"}]),
        _q("stat", "medium", "全期日均用电量（按有效数据折算）是多少？", DAILY,
           "SELECT AVG(daily_avg_kwh) AS daily_avg_kwh FROM report_daily",
           [{"kind": "value", "column": "daily_avg_kwh", "label": "折算日均用电量", "unit": "kWh"}]),
        _q("stat", "medium", "全期平均电压是多少伏？", DAILY,
           "SELECT AVG(avg_voltage) AS avg_voltage FROM report_daily",
           [{"kind": "value", "column": "avg_voltage", "label": "平均电压", "unit": "V"}]),
        # ── 陷阱：量纲（R1）──────────────────────────────────────────────
        _q("stat", "medium", "全期总用电量和平均功率分别是多少？", DAILY,
           "SELECT SUM(consumption_kwh) AS total_kwh, AVG(avg_power_kw) AS avg_power_kw FROM report_daily",
           [{"kind": "value", "column": "total_kwh", "label": "总用电量", "unit": "kWh"},
            {"kind": "value", "column": "avg_power_kw", "label": "平均功率", "unit": "kW"}],
           trap="R1", tags="量纲"),
        _q("stat", "medium", "全期总用电量与最大功率分别是多少？", DAILY,
           "SELECT SUM(consumption_kwh) AS total_kwh, MAX(max_power_kw) AS max_power_kw FROM report_daily",
           [{"kind": "value", "column": "total_kwh", "label": "总用电量", "unit": "kWh"},
            {"kind": "value", "column": "max_power_kw", "label": "最大功率", "unit": "kW"}],
           trap="R1", tags="量纲"),
        _q("stat", "medium", "平均每天用电量和平均电压分别是多少？", DAILY,
           "SELECT AVG(consumption_kwh) AS avg_kwh, AVG(avg_voltage) AS avg_voltage FROM report_daily",
           [{"kind": "value", "column": "avg_kwh", "label": "平均日用电量", "unit": "kWh"},
            {"kind": "value", "column": "avg_voltage", "label": "平均电压", "unit": "V"}],
           trap="R1", tags="量纲"),
        # ── 陷阱：两套正交拆分（R3）──────────────────────────────────────
        _q("stat", "hard", "全期厨房用电量和峰段用电量分别是多少？", DAILY,
           "SELECT SUM(sm1_kwh) AS kitchen_kwh, SUM(peak_kwh) AS peak_kwh FROM report_daily",
           [{"kind": "value", "column": "kitchen_kwh", "label": "厨房用电量", "unit": "kWh"},
            {"kind": "value", "column": "peak_kwh", "label": "峰段用电量", "unit": "kWh"}],
           trap="R3", tags="正交拆分"),
        # ── 陷阱：比率不可直接 SUM（R6）──────────────────────────────────
        _q("stat", "hard", "全期峰段用电量占总用电量的比例是多少？", DAILY,
           "SELECT SUM(peak_kwh) * 1.0 / SUM(consumption_kwh) AS peak_ratio FROM report_daily",
           [{"kind": "value", "column": "peak_ratio", "label": "峰段占比", "unit": "比率"}],
           trap="R6", tags="比率重算"),
    ]
    for year in YEARS:
        items.append(
            _q("stat", "easy", f"{year} 年的总用电量是多少？", DAILY,
               f"SELECT SUM(consumption_kwh) AS total_kwh FROM report_daily WHERE year = {year}",
               [{"kind": "value", "column": "total_kwh", "label": f"{year} 年总用电量", "unit": "kWh"}])
        )
    for year in YEARS:
        items.append(
            _q("stat", "medium", f"{year} 年平均每天用多少电？", DAILY,
               f"SELECT AVG(consumption_kwh) AS avg_kwh FROM report_daily WHERE year = {year}",
               [{"kind": "value", "column": "avg_kwh", "label": f"{year} 年平均日用电量", "unit": "kWh"}])
        )
    return items


# ══════════════════════════════════════════════════════════════════════════
#  ② agg —— 分组聚合 / 排行（25 题；陷阱 4：R1×1、R3×2、R6×1）
# ══════════════════════════════════════════════════════════════════════════

def _agg() -> list[dict]:
    items = [
        _q("agg", "easy", "按年份汇总总用电量，看看逐年变化。", DAILY,
           "SELECT year, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "consumption_kwh", "label": "年度最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "year",
             "label": "用电量最高的年份", "unit": ""}]),
        _q("agg", "easy", "按月份汇总全期的总用电量。", DAILY,
           "SELECT month, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "GROUP BY month ORDER BY month",
           [{"kind": "max", "column": "consumption_kwh", "label": "月份最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "month",
             "label": "用电量最高的月份", "unit": "月"}]),
        _q("agg", "easy", "按星期几汇总总用电量。", DAILY,
           "SELECT weekday, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "GROUP BY weekday ORDER BY weekday",
           [{"kind": "max", "column": "consumption_kwh", "label": "星期最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "weekday",
             "label": "用电量最高的星期", "unit": "(0=周一)"}]),
        _q("agg", "medium", "按小时汇总总用电量（小时报）。", HOURLY,
           "SELECT hour, SUM(consumption_kwh) AS consumption_kwh FROM report_hourly "
           "GROUP BY hour ORDER BY hour",
           [{"kind": "max", "column": "consumption_kwh", "label": "小时最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "hour",
             "label": "用电量最高的整点", "unit": "时"}]),
        _q("agg", "easy", "按峰、平、谷时段汇总总用电量（小时报）。", HOURLY,
           "SELECT period, SUM(consumption_kwh) AS consumption_kwh FROM report_hourly "
           "GROUP BY period ORDER BY period",
           [{"kind": "max", "column": "consumption_kwh", "label": "时段最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "period",
             "label": "用电量最高的时段", "unit": ""}]),
        _q("agg", "medium", "全期用电量最高的 5 天是哪几天？", DAILY,
           "SELECT date, consumption_kwh FROM report_daily ORDER BY consumption_kwh DESC LIMIT 5",
           [{"kind": "value", "column": "date", "label": "第一名的日期", "unit": ""},
            {"kind": "value", "column": "consumption_kwh", "label": "第一名的用电量", "unit": "kWh"}]),
        _q("agg", "medium", "用电量最高的 3 个月是哪几个月？", MONTHLY,
           "SELECT year_month, consumption_kwh FROM report_monthly "
           "ORDER BY consumption_kwh DESC LIMIT 3",
           [{"kind": "value", "column": "year_month", "label": "第一名的月份", "unit": ""},
            {"kind": "value", "column": "consumption_kwh", "label": "第一名的用电量", "unit": "kWh"}]),
        _q("agg", "medium", "按月汇总全期的有效采集分钟数。", DAILY,
           "SELECT month, SUM(active_minutes) AS active_minutes FROM report_daily "
           "GROUP BY month ORDER BY month",
           [{"kind": "max", "column": "active_minutes", "label": "月份最高有效分钟数", "unit": "分钟"},
            {"kind": "argmax", "column": "active_minutes", "label_column": "month",
             "label": "有效分钟数最高的月份", "unit": "月"}]),
        _q("agg", "hard", "按星期几看平均每天用电量。", DAILY,
           "SELECT weekday, AVG(consumption_kwh) AS avg_kwh FROM report_daily "
           "GROUP BY weekday ORDER BY weekday",
           [{"kind": "max", "column": "avg_kwh", "label": "星期最高日均用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "avg_kwh", "label_column": "weekday",
             "label": "日均用电量最高的星期", "unit": "(0=周一)"}]),
        _q("agg", "hard", "按小时看平均功率（小时报）。", HOURLY,
           "SELECT hour, AVG(avg_power_kw) AS avg_power_kw FROM report_hourly "
           "GROUP BY hour ORDER BY hour",
           [{"kind": "max", "column": "avg_power_kw", "label": "小时最高平均功率", "unit": "kW"},
            {"kind": "argmax", "column": "avg_power_kw", "label_column": "hour",
             "label": "平均功率最高的整点", "unit": "时"}]),
        # ── 陷阱 ────────────────────────────────────────────────────────
        _q("agg", "hard", "各年度的总用电量与平均功率分别是多少？", DAILY,
           "SELECT year, SUM(consumption_kwh) AS consumption_kwh, AVG(avg_power_kw) AS avg_power_kw "
           "FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "consumption_kwh", "label": "年度最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "year",
             "label": "用电量最高的年份", "unit": ""}],
           trap="R1", tags="量纲"),
        _q("agg", "hard", "各年度的厨房用电量与峰段用电量分别是多少？", DAILY,
           "SELECT year, SUM(sm1_kwh) AS kitchen_kwh, SUM(peak_kwh) AS peak_kwh FROM report_daily "
           "GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "kitchen_kwh", "label": "厨房用电量最高年度值", "unit": "kWh"},
            {"kind": "max", "column": "peak_kwh", "label": "峰段用电量最高年度值", "unit": "kWh"}],
           trap="R3", tags="正交拆分"),
        _q("agg", "hard", "各年度的厨房、洗衣房用电量与峰段用电量分别是多少？", DAILY,
           "SELECT year, SUM(sm1_kwh) AS kitchen_kwh, SUM(sm2_kwh) AS laundry_kwh, "
           "SUM(peak_kwh) AS peak_kwh FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "kitchen_kwh", "label": "厨房最高年度值", "unit": "kWh"},
            {"kind": "max", "column": "peak_kwh", "label": "峰段最高年度值", "unit": "kWh"}],
           trap="R3", tags="正交拆分"),
        _q("agg", "hard", "各年度未计量用电量占总用电量的比例是多少？", DAILY,
           "SELECT year, SUM(unmetered_kwh) * 1.0 / SUM(consumption_kwh) AS unmetered_ratio "
           "FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "unmetered_ratio", "label": "未计量占比最高值", "unit": "比率"},
            {"kind": "argmax", "column": "unmetered_ratio", "label_column": "year",
             "label": "未计量占比最高的年份", "unit": ""}],
           trap="R6", tags="比率重算"),
    ]
    for year in YEARS:
        items.append(
            _q("agg", "medium", f"{year} 年每个月的总用电量分别是多少？", DAILY,
               f"SELECT month, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
               f"WHERE year = {year} GROUP BY month ORDER BY month",
               [{"kind": "max", "column": "consumption_kwh", "label": f"{year} 年月度最高用电量", "unit": "kWh"},
                {"kind": "argmax", "column": "consumption_kwh", "label_column": "month",
                 "label": f"{year} 年用电量最高的月份", "unit": "月"}])
        )
    for column, label in (("peak_kwh", "峰段"), ("flat_kwh", "平段"), ("valley_kwh", "谷段")):
        items.append(
            _q("agg", "medium", f"各年度的{label}用电量分别是多少？", DAILY,
               f"SELECT year, SUM({column}) AS {column} FROM report_daily GROUP BY year ORDER BY year",
               [{"kind": "max", "column": column, "label": f"{label}用电量最高年度值", "unit": "kWh"},
                {"kind": "argmax", "column": column, "label_column": "year",
                 "label": f"{label}用电量最高的年份", "unit": ""}])
        )
    for column, label in (("sm1_kwh", "厨房"), ("sm2_kwh", "洗衣房"), ("sm3_kwh", "热水器与空调")):
        items.append(
            _q("agg", "medium", f"各年度的{label}用电量分别是多少？", DAILY,
               f"SELECT year, SUM({column}) AS {column} FROM report_daily GROUP BY year ORDER BY year",
               [{"kind": "max", "column": column, "label": f"{label}用电量最高年度值", "unit": "kWh"},
                {"kind": "argmax", "column": column, "label_column": "year",
                 "label": f"{label}用电量最高的年份", "unit": ""}])
        )
    return items


# ══════════════════════════════════════════════════════════════════════════
#  ③ compare —— 对比（20 题；陷阱 5：R1×1、R3×2、R6×2）
# ══════════════════════════════════════════════════════════════════════════

def _compare() -> list[dict]:
    items = [
        _q("compare", "easy", "周末和工作日的平均日用电量分别是多少？", DAILY,
           "SELECT is_weekend, AVG(consumption_kwh) AS avg_kwh FROM report_daily "
           "GROUP BY is_weekend ORDER BY is_weekend",
           [{"kind": "max", "column": "avg_kwh", "label": "较高的一类日均用电量", "unit": "kWh"}]),
        _q("compare", "easy", "峰段、平段、谷段的用电量分别是多少？", DAILY,
           "SELECT SUM(peak_kwh) AS peak_kwh, SUM(flat_kwh) AS flat_kwh, "
           "SUM(valley_kwh) AS valley_kwh FROM report_daily",
           [{"kind": "value", "column": "peak_kwh", "label": "峰段用电量", "unit": "kWh"},
            {"kind": "value", "column": "flat_kwh", "label": "平段用电量", "unit": "kWh"},
            {"kind": "value", "column": "valley_kwh", "label": "谷段用电量", "unit": "kWh"}]),
        _q("compare", "easy", "厨房、洗衣房、热水器与空调的用电量分别是多少？", DAILY,
           "SELECT SUM(sm1_kwh) AS kitchen_kwh, SUM(sm2_kwh) AS laundry_kwh, "
           "SUM(sm3_kwh) AS water_heating_ac_kwh FROM report_daily",
           [{"kind": "value", "column": "kitchen_kwh", "label": "厨房用电量", "unit": "kWh"},
            {"kind": "value", "column": "laundry_kwh", "label": "洗衣房用电量", "unit": "kWh"},
            {"kind": "value", "column": "water_heating_ac_kwh", "label": "热水器与空调用电量", "unit": "kWh"}]),
        # ── 陷阱 ────────────────────────────────────────────────────────
        _q("compare", "medium", "全期最大功率和总用电量分别是多少？", DAILY,
           "SELECT MAX(max_power_kw) AS max_power_kw, SUM(consumption_kwh) AS total_kwh FROM report_daily",
           [{"kind": "value", "column": "max_power_kw", "label": "最大功率", "unit": "kW"},
            {"kind": "value", "column": "total_kwh", "label": "总用电量", "unit": "kWh"}],
           trap="R1", tags="量纲"),
        _q("compare", "medium", "洗衣房用电量和谷段用电量分别是多少？", DAILY,
           "SELECT SUM(sm2_kwh) AS laundry_kwh, SUM(valley_kwh) AS valley_kwh FROM report_daily",
           [{"kind": "value", "column": "laundry_kwh", "label": "洗衣房用电量", "unit": "kWh"},
            {"kind": "value", "column": "valley_kwh", "label": "谷段用电量", "unit": "kWh"}],
           trap="R3", tags="正交拆分"),
        _q("compare", "medium", "热水器与空调用电量和平段用电量分别是多少？", DAILY,
           "SELECT SUM(sm3_kwh) AS water_heating_ac_kwh, SUM(flat_kwh) AS flat_kwh FROM report_daily",
           [{"kind": "value", "column": "water_heating_ac_kwh", "label": "热水器与空调用电量", "unit": "kWh"},
            {"kind": "value", "column": "flat_kwh", "label": "平段用电量", "unit": "kWh"}],
           trap="R3", tags="正交拆分"),
        _q("compare", "hard", "各年度峰段用电量占当年总用电量的比例是多少？", DAILY,
           "SELECT year, SUM(peak_kwh) * 1.0 / SUM(consumption_kwh) AS peak_ratio "
           "FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "peak_ratio", "label": "峰段占比最高值", "unit": "比率"},
            {"kind": "argmax", "column": "peak_ratio", "label_column": "year",
             "label": "峰段占比最高的年份", "unit": ""}],
           trap="R6", tags="比率重算"),
        _q("compare", "hard", "各年度的有效数据覆盖率分别是多少？", DAILY,
           "SELECT year, SUM(active_minutes) * 1.0 / SUM(expected_minutes) AS coverage "
           "FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "coverage", "label": "覆盖率最高值", "unit": "比率"},
            {"kind": "argmax", "column": "coverage", "label_column": "year",
             "label": "覆盖率最高的年份", "unit": ""}],
           trap="R6", tags="比率重算"),
        _q("compare", "medium", "各年度的平均电压分别是多少？", DAILY,
           "SELECT year, AVG(avg_voltage) AS avg_voltage FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "avg_voltage", "label": "平均电压最高值", "unit": "V"},
            {"kind": "argmax", "column": "avg_voltage", "label_column": "year",
             "label": "平均电压最高的年份", "unit": ""}]),
        _q("compare", "medium", "各年度的日均用电量分别是多少？", DAILY,
           "SELECT year, AVG(daily_avg_kwh) AS daily_avg_kwh FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "daily_avg_kwh", "label": "日均用电量最高值", "unit": "kWh"},
            {"kind": "argmax", "column": "daily_avg_kwh", "label_column": "year",
             "label": "日均用电量最高的年份", "unit": ""}]),
        _q("compare", "medium", "各年度的峰值功率分别是多少？", DAILY,
           "SELECT year, MAX(max_power_kw) AS max_power_kw FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "max_power_kw", "label": "峰值功率最高值", "unit": "kW"},
            {"kind": "argmax", "column": "max_power_kw", "label_column": "year",
             "label": "峰值功率最高的年份", "unit": ""}]),
        _q("compare", "hard", "各年度的平均负载率分别是多少？", DAILY,
           "SELECT year, AVG(load_factor) AS load_factor FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "load_factor", "label": "平均负载率最高值", "unit": "比率"},
            {"kind": "argmax", "column": "load_factor", "label_column": "year",
             "label": "平均负载率最高的年份", "unit": ""}]),
        _q("compare", "medium", "各年度被判定为异常的天数分别是多少？", DAILY,
           "SELECT year, SUM(is_anomaly) AS anomaly_days FROM report_daily GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "anomaly_days", "label": "异常天数最高值", "unit": "天"},
            {"kind": "argmax", "column": "anomaly_days", "label_column": "year",
             "label": "异常天数最多的年份", "unit": ""}]),
        _q("compare", "medium", "2008 年和 2010 年的全年总用电量分别是多少？", DAILY,
           "SELECT year, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "WHERE year IN (2008, 2010) GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "consumption_kwh", "label": "其中较高的一年用电量", "unit": "kWh"}]),
    ]
    for year in (2007, 2008, 2009, 2010):
        items.append(
            _q("compare", "medium", f"{year} 年和 {year - 1} 年的总用电量分别是多少？", DAILY,
               f"SELECT year, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
               f"WHERE year IN ({year - 1}, {year}) GROUP BY year ORDER BY year",
               [{"kind": "max", "column": "consumption_kwh", "label": "较高的一年用电量", "unit": "kWh"}])
        )
    for year in (2008, 2009):
        items.append(
            _q("compare", "medium",
               f"{year} 年和 {year - 1} 年单日最高用电量分别是多少？", DAILY,
               f"SELECT year, MAX(consumption_kwh) AS max_kwh FROM report_daily "
               f"WHERE year IN ({year - 1}, {year}) GROUP BY year ORDER BY year",
               [{"kind": "max", "column": "max_kwh", "label": "两年中较高的单日用电量", "unit": "kWh"}])
        )
    return items


# ══════════════════════════════════════════════════════════════════════════
#  ④ anomaly —— 异常与数据质量（20 题；无陷阱，作为对照题组）
# ══════════════════════════════════════════════════════════════════════════

def _anomaly() -> list[dict]:
    items = [
        _q("anomaly", "easy", "全期有多少天被判定为用电异常？", DAILY,
           "SELECT COUNT(*) AS anomaly_days FROM report_daily WHERE is_anomaly = 1",
           [{"kind": "value", "column": "anomaly_days", "label": "异常天数", "unit": "天"}]),
        _q("anomaly", "medium", "异常日的日期和当天用电量分别是多少？", DAILY,
           "SELECT date, consumption_kwh FROM report_daily WHERE is_anomaly = 1 ORDER BY date",
           [{"kind": "count", "column": "", "label": "异常日行数", "unit": "天"},
            {"kind": "max", "column": "consumption_kwh", "label": "异常日最高用电量", "unit": "kWh"}]),
        _q("anomaly", "medium", "全期有效采集时间不足自然时长的天数有多少？", DAILY,
           "SELECT COUNT(*) AS partial_days FROM report_daily WHERE active_minutes < expected_minutes",
           [{"kind": "value", "column": "partial_days", "label": "采集不足天数", "unit": "天"}]),
        _q("anomaly", "medium", "全期用电量为 0 的天数有多少？", DAILY,
           "SELECT COUNT(*) AS zero_days FROM report_daily WHERE consumption_kwh = 0",
           [{"kind": "value", "column": "zero_days", "label": "零用电天数", "unit": "天"}]),
        _q("anomaly", "medium", "全期日均电压最低的那天是哪天，电压是多少？", DAILY,
           # 同上：NULL 表示无电压数据，不是"最低电压"（2026-09-18 修订）
           "SELECT date, avg_voltage FROM report_daily WHERE avg_voltage IS NOT NULL "
           "ORDER BY avg_voltage ASC LIMIT 1",
           [{"kind": "value", "column": "date", "label": "电压最低日", "unit": ""},
            {"kind": "value", "column": "avg_voltage", "label": "最低日均电压", "unit": "V"}]),
        _q("anomaly", "medium", "全期日均电压最高的那天是哪天，电压是多少？", DAILY,
           "SELECT date, avg_voltage FROM report_daily ORDER BY avg_voltage DESC LIMIT 1",
           [{"kind": "value", "column": "date", "label": "电压最高日", "unit": ""},
            {"kind": "value", "column": "avg_voltage", "label": "最高日均电压", "unit": "V"}]),
        _q("anomaly", "medium", "异常日的总用电量是多少？", DAILY,
           "SELECT SUM(consumption_kwh) AS total_kwh FROM report_daily WHERE is_anomaly = 1",
           [{"kind": "value", "column": "total_kwh", "label": "异常日总用电量", "unit": "kWh"}]),
        _q("anomaly", "medium", "异常日的平均用电量是多少？", DAILY,
           "SELECT AVG(consumption_kwh) AS avg_kwh FROM report_daily WHERE is_anomaly = 1",
           [{"kind": "value", "column": "avg_kwh", "label": "异常日平均用电量", "unit": "kWh"}]),
        _q("anomaly", "hard", "异常原因分别出现了多少次？", DAILY,
           "SELECT anomaly_reason, COUNT(*) AS days FROM report_daily WHERE is_anomaly = 1 "
           "GROUP BY anomaly_reason ORDER BY days DESC",
           [{"kind": "max", "column": "days", "label": "单一原因最多出现次数", "unit": "天"},
            {"kind": "argmax", "column": "days", "label_column": "anomaly_reason",
             "label": "出现最多的异常原因", "unit": ""}]),
        _q("anomaly", "hard", "全期一共缺采了多少分钟？", DAILY,
           "SELECT SUM(expected_minutes - active_minutes) AS missing_minutes FROM report_daily",
           [{"kind": "value", "column": "missing_minutes", "label": "缺采分钟数", "unit": "分钟"}]),
    ]
    for year in YEARS:
        items.append(
            _q("anomaly", "medium", f"{year} 年有多少天被判定为用电异常？", DAILY,
               f"SELECT COUNT(*) AS anomaly_days FROM report_daily WHERE is_anomaly = 1 AND year = {year}",
               [{"kind": "value", "column": "anomaly_days", "label": f"{year} 年异常天数", "unit": "天"}])
        )
    for year in YEARS:
        items.append(
            _q("anomaly", "hard", f"{year} 年异常日的用电量合计是多少？", DAILY,
               f"SELECT SUM(consumption_kwh) AS total_kwh FROM report_daily "
               f"WHERE is_anomaly = 1 AND year = {year}",
               [{"kind": "value", "column": "total_kwh", "label": f"{year} 年异常日用电量", "unit": "kWh"}])
        )
    return items


# ══════════════════════════════════════════════════════════════════════════
#  ⑤ structure —— 用电结构（15 题；陷阱 4：R7×4）
# ══════════════════════════════════════════════════════════════════════════

def _structure() -> list[dict]:
    items = [
        _q("structure", "easy", "全期厨房、洗衣房、热水器与空调、其他未计量的用电量分别是多少？", STRUCTURE,
           "SELECT kitchen_kwh, laundry_kwh, water_heating_ac_kwh, other_unmetered_kwh "
           "FROM report_energy_structure WHERE is_total_row = 1",
           [{"kind": "value", "column": "kitchen_kwh", "label": "厨房用电量", "unit": "kWh"},
            {"kind": "value", "column": "other_unmetered_kwh", "label": "其他未计量用电量", "unit": "kWh"}]),
        _q("structure", "easy", "全期各用途用电量的占比分别是多少？", STRUCTURE,
           "SELECT kitchen_ratio, laundry_ratio, water_heating_ac_ratio, other_unmetered_ratio "
           "FROM report_energy_structure WHERE is_total_row = 1",
           [{"kind": "value", "column": "other_unmetered_ratio", "label": "其他未计量占比", "unit": "比率"}]),
        _q("structure", "easy", "全期总用电量是多少？（结构报表口径）", STRUCTURE,
           "SELECT total_kwh FROM report_energy_structure WHERE is_total_row = 1",
           [{"kind": "value", "column": "total_kwh", "label": "全期总用电量", "unit": "kWh"}]),
        _q("structure", "easy", "全期峰、平、谷三个时段的电量分别是多少？（结构报表口径）", STRUCTURE,
           "SELECT peak_kwh, flat_kwh, valley_kwh FROM report_energy_structure WHERE is_total_row = 1",
           [{"kind": "value", "column": "peak_kwh", "label": "峰段电量", "unit": "kWh"},
            {"kind": "value", "column": "valley_kwh", "label": "谷段电量", "unit": "kWh"}]),
        _q("structure", "hard", "结构报表里用途四项之和与总电量的误差、时段三项之和与总电量的误差分别是多少？",
           STRUCTURE,
           "SELECT structure_closed, time_closed FROM report_energy_structure WHERE is_total_row = 1",
           [{"kind": "value", "column": "structure_closed", "label": "用途闭合误差", "unit": "kWh"},
            {"kind": "value", "column": "time_closed", "label": "时段闭合误差", "unit": "kWh"}]),
        _q("structure", "easy", "结构报表一共有多少行（含全期合计行）？", STRUCTURE,
           "SELECT COUNT(*) AS rows_cnt FROM report_energy_structure",
           [{"kind": "value", "column": "rows_cnt", "label": "结构报表行数", "unit": "行"}]),
        _q("structure", "medium", "全期其他未计量用电的占比是多少？", STRUCTURE,
           "SELECT other_unmetered_ratio FROM report_energy_structure WHERE is_total_row = 1",
           [{"kind": "value", "column": "other_unmetered_ratio", "label": "其他未计量占比", "unit": "比率"}]),
        # ── 陷阱：全期行不可与年份行混加（R7）───────────────────────────
        _q("structure", "medium", "各年份的用途用电量分别是多少？（不含全期合计行）", STRUCTURE,
           "SELECT period, kitchen_kwh, laundry_kwh, water_heating_ac_kwh, other_unmetered_kwh "
           "FROM report_energy_structure WHERE is_total_row = 0 ORDER BY period",
           [{"kind": "max", "column": "other_unmetered_kwh", "label": "年度最高其他未计量用电量", "unit": "kWh"}],
           trap="R7", tags="全期行"),
        _q("structure", "medium", "各年份的总用电量分别是多少？（不含全期合计行）", STRUCTURE,
           "SELECT period, total_kwh FROM report_energy_structure WHERE is_total_row = 0 ORDER BY period",
           [{"kind": "max", "column": "total_kwh", "label": "年度最高总用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "total_kwh", "label_column": "period",
             "label": "用电量最高的年份", "unit": ""}],
           trap="R7", tags="全期行"),
        _q("structure", "medium", "全期各用途用电量的占比分别是多少？（取全期合计行）", STRUCTURE,
           "SELECT kitchen_ratio, laundry_ratio, water_heating_ac_ratio, other_unmetered_ratio "
           "FROM report_energy_structure WHERE is_total_row = 1",
           [{"kind": "value", "column": "kitchen_ratio", "label": "厨房占比", "unit": "比率"}],
           trap="R7", tags="全期行"),
        _q("structure", "hard", "各年份峰段电量占当年总电量的比例分别是多少？（不含全期合计行）", STRUCTURE,
           "SELECT period, peak_kwh * 1.0 / total_kwh AS peak_ratio FROM report_energy_structure "
           "WHERE is_total_row = 0 ORDER BY period",
           [{"kind": "max", "column": "peak_ratio", "label": "年度最高峰段占比", "unit": "比率"}],
           trap="R7", tags="全期行"),
    ]
    for year in ("2007", "2008", "2009", "2010"):
        items.append(
            _q("structure", "medium", f"{year} 年各用途用电量的占比分别是多少？", STRUCTURE,
               f"SELECT kitchen_ratio, laundry_ratio, water_heating_ac_ratio, other_unmetered_ratio "
               f"FROM report_energy_structure WHERE period = '{year}'",
               [{"kind": "value", "column": "other_unmetered_ratio",
                 "label": f"{year} 年其他未计量占比", "unit": "比率"}])
        )
    return items


# ══════════════════════════════════════════════════════════════════════════
#  ⑥ multi_turn —— 多轮追问（15 题；question 内两轮以换行分隔，判分看最后一轮）
# ══════════════════════════════════════════════════════════════════════════

def _multi_turn() -> list[dict]:
    return [
        _q("multi_turn", "medium",
           "按月看看 2007 年的用电情况。\n那 2008 年呢？", DAILY,
           "SELECT month, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "WHERE year = 2008 GROUP BY month ORDER BY month",
           [{"kind": "max", "column": "consumption_kwh", "label": "2008 年月度最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "month",
             "label": "2008 年用电量最高的月份", "unit": "月"}],
           tags="继承粒度"),
        _q("multi_turn", "medium",
           "按月看看 2008 年的用电情况。\n那 2009 年呢？", DAILY,
           "SELECT month, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "WHERE year = 2009 GROUP BY month ORDER BY month",
           [{"kind": "max", "column": "consumption_kwh", "label": "2009 年月度最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "month",
             "label": "2009 年用电量最高的月份", "unit": "月"}],
           tags="继承粒度"),
        _q("multi_turn", "medium",
           "按月看看 2009 年的用电情况。\n那 2010 年呢？", DAILY,
           "SELECT month, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "WHERE year = 2010 GROUP BY month ORDER BY month",
           [{"kind": "max", "column": "consumption_kwh", "label": "2010 年月度最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "month",
             "label": "2010 年用电量最高的月份", "unit": "月"}],
           tags="继承粒度"),
        _q("multi_turn", "easy",
           "全期总用电量是多少？\n按年拆开看看。", DAILY,
           "SELECT year, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "consumption_kwh", "label": "年度最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "year",
             "label": "用电量最高的年份", "unit": ""}],
           tags="变更粒度"),
        _q("multi_turn", "medium",
           "各年份总用电量分别是多少？\n只要 2009 和 2010 年。", DAILY,
           "SELECT year, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "WHERE year IN (2009, 2010) GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "consumption_kwh", "label": "两年中较高的一年用电量", "unit": "kWh"}],
           tags="追加过滤"),
        _q("multi_turn", "medium",
           "峰平谷时段的用电量分别是多少？\n只看峰段，按月汇总。", MONTHLY,
           "SELECT year_month, peak_kwh FROM report_monthly ORDER BY year_month",
           [{"kind": "max", "column": "peak_kwh", "label": "月度峰段最高电量", "unit": "kWh"},
            {"kind": "argmax", "column": "peak_kwh", "label_column": "year_month",
             "label": "峰段电量最高的月份", "unit": ""}],
           tags="切换指标"),
        _q("multi_turn", "easy",
           "厨房用电占比是多少？\n那洗衣房呢？", STRUCTURE,
           "SELECT laundry_ratio FROM report_energy_structure WHERE is_total_row = 1",
           [{"kind": "value", "column": "laundry_ratio", "label": "洗衣房占比", "unit": "比率"}],
           tags="切换维度"),
        _q("multi_turn", "easy",
           "每天的用电量趋势怎么样？\n改成按月汇总。", MONTHLY,
           "SELECT year_month, consumption_kwh FROM report_monthly ORDER BY year_month",
           [{"kind": "max", "column": "consumption_kwh", "label": "月度最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "year_month",
             "label": "用电量最高的月份", "unit": ""}],
           tags="变更粒度"),
        _q("multi_turn", "hard",
           "全期有多少天被判定为用电异常？\n这些异常日主要分布在哪些年份？", DAILY,
           "SELECT year, COUNT(*) AS anomaly_days FROM report_daily WHERE is_anomaly = 1 "
           "GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "anomaly_days", "label": "异常天数最多的年份值", "unit": "天"},
            {"kind": "argmax", "column": "anomaly_days", "label_column": "year",
             "label": "异常天数最多的年份", "unit": ""}],
           tags="继承过滤"),
        _q("multi_turn", "hard",
           "全期各用途用电量的占比是多少？\n按时段再看一遍。", STRUCTURE,
           "SELECT peak_ratio, flat_ratio, valley_ratio FROM report_energy_structure "
           "WHERE is_total_row = 1",
           [{"kind": "value", "column": "peak_ratio", "label": "峰段占比", "unit": "比率"},
            {"kind": "value", "column": "valley_ratio", "label": "谷段占比", "unit": "比率"}],
           tags="切换维度"),
        _q("multi_turn", "medium",
           "2009 年的总用电量是多少？\n比 2008 年变化多少？", DAILY,
           "SELECT year, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "WHERE year IN (2008, 2009) GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "consumption_kwh", "label": "较高的一年用电量", "unit": "kWh"}],
           tags="对比追问"),
        _q("multi_turn", "medium",
           "全期平均电压是多少？\n按月看电压变化。", DAILY,
           "SELECT substr(date, 1, 7) AS year_month, AVG(avg_voltage) AS avg_voltage "
           "FROM report_daily GROUP BY year_month ORDER BY year_month",
           [{"kind": "max", "column": "avg_voltage", "label": "月度最高平均电压", "unit": "V"},
            {"kind": "argmax", "column": "avg_voltage", "label_column": "year_month",
             "label": "平均电压最高的月份", "unit": ""}],
           tags="变更粒度"),
        _q("multi_turn", "medium",
           "2007 年用电量最高的那天用了多少电？\n那最低的那天呢？", DAILY,
           # 同上：极值题排除 NULL（2026-09-18 修订）
           "SELECT date, consumption_kwh FROM report_daily WHERE year = 2007"
           " AND consumption_kwh IS NOT NULL "
           "ORDER BY consumption_kwh ASC LIMIT 1",
           [{"kind": "value", "column": "date", "label": "2007 年最低日", "unit": ""},
            {"kind": "value", "column": "consumption_kwh", "label": "2007 年最低日用电量", "unit": "kWh"}],
           tags="切换极值方向"),
        _q("multi_turn", "medium",
           "按星期几汇总总用电量。\n只看工作日。", DAILY,
           "SELECT weekday, SUM(consumption_kwh) AS consumption_kwh FROM report_daily "
           "WHERE is_weekend = 0 GROUP BY weekday ORDER BY weekday",
           [{"kind": "max", "column": "consumption_kwh", "label": "工作日最高用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "consumption_kwh", "label_column": "weekday",
             "label": "工作日用电量最高的星期", "unit": "(0=周一)"}],
           tags="追加过滤"),
        _q("multi_turn", "medium",
           "全期未计量用电总量是多少？\n按年拆开看看。", DAILY,
           "SELECT year, SUM(unmetered_kwh) AS unmetered_kwh FROM report_daily "
           "GROUP BY year ORDER BY year",
           [{"kind": "max", "column": "unmetered_kwh", "label": "年度最高未计量用电量", "unit": "kWh"},
            {"kind": "argmax", "column": "unmetered_kwh", "label_column": "year",
             "label": "未计量用电量最高的年份", "unit": ""}],
           tags="变更粒度"),
    ]


# ══════════════════════════════════════════════════════════════════════════
#  gold 反算与关键数值抽取
# ══════════════════════════════════════════════════════════════════════════

def _json_safe(value):
    if isinstance(value, float):
        return round(value, 10)
    return value


def run_gold(conn: sqlite3.Connection, sql: str) -> tuple[list[dict], list[str]]:
    """对真实库执行 gold_sql，返回（行列表, 列名列表）。"""
    cursor = conn.execute(sql)
    columns = [item[0] for item in cursor.description or []]
    rows = [
        {column: _json_safe(value) for column, value in zip(columns, record)}
        for record in cursor.fetchall()
    ]
    return rows, columns


def _numeric(values) -> list[float]:
    out: list[float] = []
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out


def extract_key_numbers(specs: list[dict], rows: list[dict], columns: list[str]) -> list[dict]:
    """按 `key_specs` 从 gold 结果里抽出结论判分用的关键数值/标签。

    kind：`value`（单行取该列值）/ `sum` / `max` / `min` / `argmax` / `argmin` / `count`。
    `argmax`/`argmin` 取"该列极值所在行的 `label_column` 值"——解读里通常会出现这个标签
    （如"用电量最高的月份是 7 月"），因此标签也要纳入命中判定。
    """
    keys: list[dict] = []
    for spec in specs:
        kind = spec["kind"]
        column = spec.get("column") or ""
        label = spec.get("label") or column
        unit = spec.get("unit") or ""
        if kind == "count":
            keys.append({"label": label, "value": len(rows), "unit": unit, "kind": "number"})
            continue
        if column not in columns:
            continue
        values = [row.get(column) for row in rows]
        if kind == "value":
            if not rows:
                continue
            value = rows[0].get(column)
            if value is None:
                continue
            keys.append({
                "label": label, "value": value, "unit": unit,
                "kind": "number" if isinstance(value, (int, float)) else "label",
            })
            continue
        numbers = _numeric(values)
        if not numbers:
            continue
        if kind == "sum":
            keys.append({"label": label, "value": round(sum(numbers), 10), "unit": unit, "kind": "number"})
        elif kind == "max":
            keys.append({"label": label, "value": max(numbers), "unit": unit, "kind": "number"})
        elif kind == "min":
            keys.append({"label": label, "value": min(numbers), "unit": unit, "kind": "number"})
        elif kind in ("argmax", "argmin"):
            pick = max if kind == "argmax" else min
            row = pick(rows, key=lambda item: item.get(column) if isinstance(item.get(column), (int, float)) else float("-inf"))
            label_value = row.get(spec.get("label_column") or columns[0])
            if label_value is not None:
                keys.append({"label": label, "value": str(label_value), "unit": unit, "kind": "label"})
    return keys


# ══════════════════════════════════════════════════════════════════════════
#  构建与入库
# ══════════════════════════════════════════════════════════════════════════

def build_definitions() -> list[dict]:
    definitions = [
        *_stat(), *_agg(), *_compare(), *_anomaly(), *_structure(), *_multi_turn(),
    ]
    for index, item in enumerate(definitions, start=1):
        item["question_code"] = f"q{index:03d}"
        item["sort_no"] = index
    return definitions


def validate(definitions: list[dict]) -> list[str]:
    """题库分布校验（G4 验收口径）：总量 / 题型 / 陷阱题占比 / 必填字段。"""
    problems: list[str] = []
    if len(definitions) != TARGET_TOTAL:
        problems.append(f"题量 {len(definitions)} ≠ 目标 {TARGET_TOTAL}")
    by_type: dict[str, int] = {}
    traps = 0
    for item in definitions:
        by_type[item["qtype"]] = by_type.get(item["qtype"], 0) + 1
        traps += item["is_trap"]
        if not item["gold_sql"] and not item.get("gold_code"):
            problems.append(f"{item['question_code']} 缺 gold_sql/gold_code")
        if not item["report_table"]:
            problems.append(f"{item['question_code']} 缺 report_table")
        if item["is_trap"] and item["trap_rule"] not in ("R1", "R3", "R6", "R7"):
            problems.append(f"{item['question_code']} 陷阱题缺少合法 trap_rule")
    for qtype in ("stat", "agg", "compare", "anomaly", "structure", "multi_turn"):
        if by_type.get(qtype, 0) == 0:
            problems.append(f"题型 {qtype} 没有题目")
    ratio = traps / max(len(definitions), 1)
    if not 0.12 <= ratio <= 0.20:
        problems.append(f"陷阱题占比 {ratio:.1%} 不在 12%–20% 之间（PRD F5.1 约 15%）")
    return problems


def build_payload(definitions: list[dict], conn: sqlite3.Connection) -> tuple[list[dict], list[str]]:
    """执行 gold_sql 反算答案与关键数值；返回（题目载荷, 失败清单）。"""
    payload: list[dict] = []
    failures: list[str] = []
    for item in definitions:
        try:
            rows, columns = run_gold(conn, item["gold_sql"])
        except sqlite3.Error as exc:
            failures.append(f"{item['question_code']} gold_sql 执行失败：{exc}")
            continue
        if not rows:
            failures.append(f"{item['question_code']} gold 结果为空（题目写错或数据缺失）")
            continue
        payload.append(
            {
                "question_code": item["question_code"],
                "question": item["question"],
                "qtype": item["qtype"],
                "difficulty": item["difficulty"],
                "report_table": item["report_table"],
                "gold_sql": item["gold_sql"],
                "gold_code": None,
                "gold_answer_json": json.dumps(
                    {"columns": columns, "rows": rows, "row_count": len(rows)}, ensure_ascii=False
                ),
                "key_numbers_json": json.dumps(
                    extract_key_numbers(item["key_specs"], rows, columns), ensure_ascii=False
                ),
                "is_trap": item["is_trap"],
                "trap_rule": item["trap_rule"],
                "tags": item["tags"] or None,
                "sort_no": item["sort_no"],
            }
        )
    return payload, failures


def write_json(payload: list[dict]) -> Path:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    QUESTIONS_JSON.write_text(
        json.dumps({"total": len(payload), "questions": payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return QUESTIONS_JSON


def upsert_db(payload: list[dict], conn: sqlite3.Connection) -> None:
    """按 `question_code` 全量重建题库（删掉不在本次快照里的旧题，保证幂等）。"""
    codes = [item["question_code"] for item in payload]
    placeholders = ",".join("?" for _ in codes)
    conn.execute(f"DELETE FROM eval_questions WHERE question_code NOT IN ({placeholders})", codes)
    for item in payload:
        conn.execute(
            """
            INSERT INTO eval_questions
                (question_code, question, qtype, difficulty, report_table, gold_sql, gold_code,
                 gold_answer_json, key_numbers_json, is_trap, trap_rule, tags, sort_no)
            VALUES (:question_code, :question, :qtype, :difficulty, :report_table, :gold_sql,
                    :gold_code, :gold_answer_json, :key_numbers_json, :is_trap, :trap_rule,
                    :tags, :sort_no)
            ON CONFLICT (question_code) DO UPDATE SET
                question = excluded.question, qtype = excluded.qtype,
                difficulty = excluded.difficulty, report_table = excluded.report_table,
                gold_sql = excluded.gold_sql, gold_code = excluded.gold_code,
                gold_answer_json = excluded.gold_answer_json,
                key_numbers_json = excluded.key_numbers_json, is_trap = excluded.is_trap,
                trap_rule = excluded.trap_rule, tags = excluded.tags, sort_no = excluded.sort_no
            """,
            item,
        )
    conn.commit()


def summarize(payload: list[dict]) -> str:
    lines = [f"题库合计 {len(payload)} 题"]
    for qtype in ("stat", "agg", "compare", "anomaly", "structure", "multi_turn"):
        subset = [item for item in payload if item["qtype"] == qtype]
        traps = sum(item["is_trap"] for item in subset)
        by_difficulty = {
            level: sum(1 for item in subset if item["difficulty"] == level)
            for level in ("easy", "medium", "hard")
        }
        lines.append(
            f"  {qtype:<10} {len(subset):>3} 题（陷阱 {traps}）；"
            f"难度 easy/medium/hard = {by_difficulty['easy']}/{by_difficulty['medium']}/{by_difficulty['hard']}"
        )
    lines.append(f"  陷阱题合计 {sum(item['is_trap'] for item in payload)} 题")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建智电析报评测题库（120 题）")
    parser.add_argument("--check", action="store_true", help="只校验分布与 gold 可执行性，不写文件/不入库")
    parser.add_argument("--limit", type=int, default=0, help="只构建前 N 题（调试用）")
    args = parser.parse_args(argv)

    logger = C.setup_logger("eval_set")
    definitions = build_definitions()
    if args.limit:
        definitions = definitions[: args.limit]

    problems = validate(definitions) if not args.limit else []
    reports_conn = sqlite3.connect(f"file:{C.REPORTS_DB.as_posix()}?mode=ro", uri=True)
    try:
        payload, failures = build_payload(definitions, reports_conn)
    finally:
        reports_conn.close()

    for problem in problems:
        logger.error("题库校验不通过：%s", problem)
    for failure in failures:
        logger.error("gold 反算失败：%s", failure)

    print(summarize(payload))
    if problems or failures:
        print(f"\n校验失败：分布问题 {len(problems)} 项、gold 失败 {len(failures)} 项")
        return 1

    if args.check:
        print("\n--check：校验通过（未写文件、未入库）")
        return 0

    path = write_json(payload)
    agent_conn = sqlite3.connect(C.AGENT_DB)
    try:
        upsert_db(payload, agent_conn)
        total = agent_conn.execute("SELECT COUNT(*) FROM eval_questions").fetchone()[0]
    finally:
        agent_conn.close()
    print(f"\n题库快照：{path}")
    print(f"入库完成：eval_questions 现有 {total} 题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
