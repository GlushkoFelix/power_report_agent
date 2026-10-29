# -*- coding: utf-8 -*-
"""03_init_db.py —— 建库并灌数（reports.db 5 张表 + power_agent.db 10 张表）

    reports.db      ：执行 sql/reports.sql → 灌入分钟明细（207 万行）与 4 张报表
    power_agent.db  ：执行 sql/power_agent.sql → 预置 5 张报表元数据（uploaded_reports）

为保证"库 CSV 一致"，本脚本**直接调用 01_build_reports 的聚合函数**（同一份清洗逻辑），
而非读回已舍入的 CSV —— 因此库内数值为未舍入的精确值，三级总量严格相等。

用法：
    .venv\\Scripts\\python.exe 01.code\\scripts\\03_init_db.py [--force]

产物：
    02.data/reports.db · 02.data/power_agent.db · 02.data/quality/db_verification.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

import _common as C

MINUTE_DB_COLS = [
    "dt", "year", "month", "day", "hour", "weekday", "is_weekend", "period",
    "gap_kw", "gap_kwh", "sm1_kwh", "sm2_kwh", "sm3_kwh", "unmetered_kwh",
    "voltage", "intensity", "reactive_power_kw", "is_missing",
]


def load_builders():
    """按文件路径导入 01_build_reports（文件名以数字开头，无法直接 import）。"""
    path = Path(__file__).resolve().parent / "01_build_reports.py"
    spec = importlib.util.spec_from_file_location("build_reports", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def split_minute_indexes(sql_text: str) -> tuple[str, list[str]]:
    """把 reports.sql 中 report_minute 的索引语句单独取出（先不建，灌数后再建，速度更快）。"""
    index_stmts: list[str] = []
    ddl_lines: list[str] = []
    for stmt in sql_text.split(";"):
        text = stmt.strip()
        if not text:
            continue
        if "INDEX" in text.upper() and C.TABLE_MINUTE in text:
            index_stmts.append(text)
        else:
            ddl_lines.append(text)
    return ";\n".join(ddl_lines) + ";", index_stmts


#: 各报表与 A1–A5 校验项的对应关系（决策：quality_json 按表裁剪，只放与该表相关的校验）
QUALITY_CHECKS = {
    "r_minute": ("A5_row_counts",),
    "r_hourly": ("A1_levels_equal", "A5_row_counts"),
    "r_daily": ("A1_levels_equal", "A5_row_counts"),
    "r_monthly": ("A1_levels_equal", "A5_row_counts"),
    "r_structure": ("A2_structure_closed", "A3_time_closed", "A4_unmetered_share",
                    "A5_row_counts"),
}


def load_schema_json(table: str) -> str | None:
    """读取 `02.data/schema/<table>.schema.json` 并紧凑化（避免库内体积膨胀）。"""
    path = C.SCHEMA_DIR / f"{table}.schema.json"
    if not path.exists():
        return None
    return json.dumps(json.loads(path.read_text(encoding="utf-8")),
                      ensure_ascii=False, separators=(",", ":"))


def build_quality_json(code: str, quality: dict, verification: dict) -> str:
    """按表裁剪的数据集级质量报告（含该表相关的 A1–A5 校验结论）。

    说明：数据质量基于**分钟级源数据**统计，派生报表与源数据同源，故共用同一份质量事实；
    各表只挂载与自己相关的口径校验项，避免前端展示无关结论。
    """
    check_names = QUALITY_CHECKS.get(code, ("A5_row_counts",))
    payload = {
        "level": "dataset",
        "note": "质量指标基于分钟级源数据统计；派生报表与源数据同源，共用同一份质量事实。",
        "caliber_version": C.CALIBER_VERSION,
        "checked_at": verification.get("checked_at"),
        "dataset": quality,
        "checks": {name: verification[name] for name in check_names if name in verification},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description="建库并灌数（reports.db + power_agent.db）")
    parser.add_argument("--force", action="store_true", help="删除既有库文件并重建")
    args = parser.parse_args()

    logger = C.setup_logger("03_init_db")
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)

    if C.REPORTS_DB.exists() and not args.force:
        with sqlite3.connect(C.REPORTS_DB) as conn:
            n = conn.execute(f"SELECT COUNT(*) FROM {C.TABLE_MINUTE}").fetchone()[0]
        if n > 0:
            logger.info("reports.db 已存在且已灌数（%d 行分钟明细），跳过（--force 可重建）", n)
            return 0

    if args.force:
        for db in (C.REPORTS_DB, C.AGENT_DB):
            if db.exists():
                db.unlink()
                logger.info("已删除旧库：%s", db.name)

    # ---------- 1. 计算精确报表（复用 01 的聚合逻辑） ----------
    module = load_builders()
    t0 = time.time()
    minute, quality = C.parse_clean_minute(logger)
    minute = C.add_period_kwh(minute)
    hourly = module.build_hourly(minute, logger)
    daily = module.build_daily(minute, hourly, logger)
    monthly = module.build_monthly(daily, logger)
    structure = module.build_structure(daily, logger)
    logger.info("报表计算完成，用时 %.1fs", time.time() - t0)

    # ---------- 2. reports.db ----------
    sql_text = (C.SQL_DIR / "reports.sql").read_text(encoding="utf-8")
    ddl_text, minute_indexes = split_minute_indexes(sql_text)

    conn = sqlite3.connect(C.REPORTS_DB)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(ddl_text)
        logger.info("reports.db 建表完成（5 张表）")

        t1 = time.time()
        payload = minute[MINUTE_DB_COLS]
        payload.to_sql(C.TABLE_MINUTE, conn, if_exists="append", index=False, chunksize=100_000)
        logger.info("已灌入 %s：%d 行（%.1fs）", C.TABLE_MINUTE, len(payload), time.time() - t1)

        for stmt in minute_indexes:
            conn.execute(stmt)
        conn.commit()
        logger.info("已为 %s 建立 %d 个索引", C.TABLE_MINUTE, len(minute_indexes))

        for table, frame in (
            (C.TABLE_HOURLY, hourly),
            (C.TABLE_DAILY, daily),
            (C.TABLE_MONTHLY, monthly),
            (C.TABLE_STRUCTURE, structure),
        ):
            frame.to_sql(table, conn, if_exists="append", index=False)
            logger.info("已灌入 %s：%d 行", table, len(frame))
        conn.commit()

        # ---------- 3. 库内口径校验 A1–A5（结果同时供 quality_json 回填） ----------
        verification = verify(conn, quality, logger)

        # ---------- 4. power_agent.db（预置报表元数据 + Schema / 质量回填） ----------
        agent_sql = (C.SQL_DIR / "power_agent.sql").read_text(encoding="utf-8")
        agent = sqlite3.connect(C.AGENT_DB)
        try:
            agent.executescript(agent_sql)
            # LangGraph 检查点表（**手工纳管**，理由与来源见 sql/power_agent_langgraph.sql 文件头）
            agent.executescript(
                (C.SQL_DIR / "power_agent_langgraph.sql").read_text(encoding="utf-8")
            )
            logger.info("power_agent.db 已建 LangGraph 检查点表（checkpoints / writes）")
            ranges = {
                C.TABLE_MINUTE: (quality["time_start"], quality["time_end"], len(minute), len(MINUTE_DB_COLS)),
                C.TABLE_HOURLY: (str(hourly["dt_hour"].min()), str(hourly["dt_hour"].max()),
                                 len(hourly), len(hourly.columns)),
                C.TABLE_DAILY: (str(daily["date"].min()), str(daily["date"].max()),
                                len(daily), len(daily.columns)),
                C.TABLE_MONTHLY: (str(monthly["year_month"].min()), str(monthly["year_month"].max()),
                                  len(monthly), len(monthly.columns)),
                C.TABLE_STRUCTURE: ("2006", "全期", len(structure), len(structure.columns)),
            }
            for code, table, rtype, title in C.PRESET_REPORTS:
                start, end, rows, cols = ranges[table]
                schema_json = load_schema_json(table)
                quality_json = build_quality_json(code, quality, verification)
                agent.execute(
                    """
                    INSERT INTO uploaded_reports
                        (report_code, user_id, filename, table_name, report_type, row_count,
                         column_count, time_start, time_end, caliber_version, is_preset,
                         is_active, status, schema_json, quality_json)
                    VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'ready', ?, ?)
                    ON CONFLICT(report_code) DO UPDATE SET
                        row_count = excluded.row_count,
                        time_start = excluded.time_start,
                        time_end = excluded.time_end,
                        caliber_version = excluded.caliber_version,
                        schema_json = excluded.schema_json,
                        quality_json = excluded.quality_json,
                        updated_at = datetime('now', 'localtime')
                    """,
                    (code, f"{table}.csv" if table != C.TABLE_MINUTE else "（仅入库）", table, rtype,
                     rows, cols, start, end, C.CALIBER_VERSION,
                     1 if code == C.ACTIVE_REPORT_CODE else 0, schema_json, quality_json),
                )
            agent.commit()
            logger.info("power_agent.db 已建表并预置 %d 张报表元数据（含 schema_json / quality_json 回填）",
                        len(C.PRESET_REPORTS))
        finally:
            agent.close()

    finally:
        conn.close()

    (C.QUALITY_DIR / "db_verification.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ok = all(v["passed"] for v in verification.values() if "passed" in v)
    logger.info("库内校验：%s", "全部通过" if ok else "存在未通过项")
    return 0 if ok else 2


def verify(conn: sqlite3.Connection, quality: dict, logger) -> dict:
    """在库上重跑 A1–A5（校验的是真正被分析的数据）。"""
    q = lambda sql: conn.execute(sql).fetchall()  # noqa: E731
    h = q(f"SELECT SUM(consumption_kwh) FROM {C.TABLE_HOURLY}")[0][0]
    d = q(f"SELECT SUM(consumption_kwh) FROM {C.TABLE_DAILY}")[0][0]
    m = q(f"SELECT SUM(consumption_kwh) FROM {C.TABLE_MONTHLY}")[0][0]
    closed = q(f"SELECT MAX(ABS(structure_closed)), MAX(ABS(time_closed)) "
               f"FROM {C.TABLE_STRUCTURE}")[0]
    unmetered, total = q(f"SELECT other_unmetered_kwh, total_kwh FROM {C.TABLE_STRUCTURE} "
                         f"WHERE is_total_row = 1")[0]
    counts = {t: q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in
              (C.TABLE_MINUTE, C.TABLE_HOURLY, C.TABLE_DAILY, C.TABLE_MONTHLY, C.TABLE_STRUCTURE)}
    level_gap = max(abs(h - d), abs(d - m))
    result = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "A1_levels_equal": {
            "hourly_total_kwh": round(h, 6), "daily_total_kwh": round(d, 6),
            "monthly_total_kwh": round(m, 6), "max_gap_kwh": level_gap,
            "passed": level_gap <= 1e-6,
        },
        "A2_structure_closed": {"max_abs_error_kwh": closed[0],
                                "passed": bool(closed[0] is None or closed[0] <= 1e-6)},
        "A3_time_closed": {"max_abs_error_kwh": closed[1],
                           "passed": bool(closed[1] is None or closed[1] <= 1e-6)},
        "A4_unmetered_share": {
            "unmetered_kwh": round(unmetered, 6), "total_kwh": round(total, 6),
            "share": round(unmetered / total, 6), "note": "仅记录，不作硬阈值",
        },
        "A5_row_counts": {**counts, "passed": counts[C.TABLE_MINUTE] == quality["cleaned_rows"]},
    }
    for name, detail in result.items():
        if name != "checked_at":
            logger.info("  %s → %s", name, json.dumps(detail, ensure_ascii=False))
    return result


if __name__ == "__main__":
    sys.exit(main())
