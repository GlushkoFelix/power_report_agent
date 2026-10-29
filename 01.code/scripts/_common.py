# -*- coding: utf-8 -*-
"""智电析报 · 数据层公共模块（口径单一来源）

被 `01_build_reports.py` 与 `03_init_db.py` 共用，保证"报表生成"与"入库"使用**同一套解析与清洗逻辑**。

本模块固化以下口径（对应 PRD v1.4.0 第 8 章「数据口径与报表规范」）：
    R1 量纲：GAP(kW) → kWh 用 /60；Sub_metering(Wh) → kWh 用 /1000
    R2 未计量用电 = gap_kwh − sm1_kwh − sm2_kwh − sm3_kwh
    R3 两套正交拆分（按用途 4 项 / 按时段 3 项）各自求和 = 总量，禁止混加
    R4 expected_minutes 为自然时间长度（小时 60 / 日 1440 / 月 天数×1440）
    R5 daily_avg_kwh = 电量 × 1440 / active_minutes，active_minutes=0 时为 NULL
    R6 比率型字段不可 SUM
    R7 结构报表「全期」行是年份行合计，不可与年份行一起 SUM
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------------
# 路径常量
# --------------------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]                 # e:\power_report_agent
DATA_DIR = ROOT / "02.data"
RAW_FILE = ROOT / "household_power_consumption.txt"        # 原始数据集（用户下载）
REPORTS_DIR = DATA_DIR / "reports"
QUALITY_DIR = DATA_DIR / "quality"
SCHEMA_DIR = DATA_DIR / "schema"
LOGS_DIR = DATA_DIR / "logs"
SQL_DIR = ROOT / "01.code" / "scripts" / "sql"

REPORTS_DB = DATA_DIR / "reports.db"
AGENT_DB = DATA_DIR / "power_agent.db"

# --------------------------------------------------------------------------------------
# 口径版本与规则常量
# --------------------------------------------------------------------------------------
CALIBER_VERSION = "caliber-v1.0"

# 峰平谷时段（左闭右开；谷 00:00–07:59 / 平 08:00–17:59 / 峰 18:00–23:59）
PERIOD_HOURS = {
    "valley": (0, 7),
    "flat": (8, 17),
    "peak": (18, 23),
}

# 异常判定（PRD F2.5 / 12.4 Q4）
ANOMALY_WINDOW_DAYS = 30          # 近 30 天滚动窗口
ANOMALY_MIN_PERIODS = 7           # 滚动窗口最小样本（避免开头几天误判）
ANOMALY_SIGMA = 3.0               # 3σ
ANOMALY_WINDOW_MONTHS = 3         # 月报滚动窗口（月）
ANOMALY_MIN_MONTHS = 3
# 电压越界阈值：按实测分布（min 223.2 / P0.1% 228.6 / 均值 240.8 / P99.9% 250.6 / max 254.2）
# 收紧到 210–250V —— 原 200–260V 在全期 204.9 万个有效分钟中命中 0 次（规则失效）
VOLTAGE_MIN = 210.0               # 电压下限 V
VOLTAGE_MAX = 250.0               # 电压上限 V
# 当日累计越界分钟门槛：实测"越界 ≥1 分钟"命中 110 天（噪声大）、"≥30 分钟"命中 34 天，
# 取 30 分钟（约一天 2%）作为"当日电压异常"的判定门槛
VOLTAGE_OOB_MIN_MINUTES = 30

# 输出精度（PRD 8.10）
PRECISION = {"kwh": 6, "ratio": 6, "power": 3, "voltage": 3, "intensity": 3}
CSV_ENCODING = "utf-8-sig"

# 原始列名
RAW_COLUMNS = [
    "Date",
    "Time",
    "Global_active_power",
    "Global_reactive_power",
    "Voltage",
    "Global_intensity",
    "Sub_metering_1",
    "Sub_metering_2",
    "Sub_metering_3",
]
RAW_NUMERIC = RAW_COLUMNS[2:]

# 报表名（与 sql/reports.sql 一致）
TABLE_MINUTE = "report_minute"
TABLE_HOURLY = "report_hourly"
TABLE_DAILY = "report_daily"
TABLE_MONTHLY = "report_monthly"
TABLE_STRUCTURE = "report_energy_structure"

# 5 张报表的元数据（供 03_init_db 预置 uploaded_reports）
PRESET_REPORTS = [
    ("r_minute", TABLE_MINUTE, "minute", "分钟级明细"),
    ("r_hourly", TABLE_HOURLY, "hourly", "小时报"),
    ("r_daily", TABLE_DAILY, "daily", "日报"),
    ("r_monthly", TABLE_MONTHLY, "monthly", "月报"),
    ("r_structure", TABLE_STRUCTURE, "structure", "用电结构报表"),
]
ACTIVE_REPORT_CODE = "r_daily"     # 默认活跃报表


# --------------------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------------------
def setup_logger(name: str) -> logging.Logger:
    """创建同时输出到控制台与 02.data/logs/ 的日志器。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:                       # 避免重复添加 handler
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    log_file = LOGS_DIR / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.info("日志文件：%s", log_file)
    return logger


# --------------------------------------------------------------------------------------
# 解析与清洗
# --------------------------------------------------------------------------------------
def parse_clean_minute(logger: logging.Logger | None = None) -> tuple[pd.DataFrame, dict]:
    """读取原始 txt 并清洗为分钟级明细表。

    返回 (DataFrame, 质量统计 dict)。DataFrame 列与 `report_minute` 表结构逐一对应。
    """
    if logger is None:
        logger = setup_logger("parse")

    if not RAW_FILE.exists():
        raise FileNotFoundError(f"原始数据文件不存在：{RAW_FILE}")

    logger.info("读取原始数据：%s（%.1f MB）", RAW_FILE.name, RAW_FILE.stat().st_size / 1024 / 1024)
    raw = pd.read_csv(
        RAW_FILE,
        sep=";",
        usecols=RAW_COLUMNS,
        dtype={c: "float64" for c in RAW_NUMERIC} | {"Date": "string", "Time": "string"},
        na_values=["?"],
        keep_default_na=True,
        encoding="utf-8",
    )
    logger.info("读取完成：%d 行 × %d 列", len(raw), len(raw.columns))

    quality: dict = {"source_rows": int(len(raw))}

    # ① 缺失标记（原始整行为 '?' 的记录）
    is_missing = raw[RAW_NUMERIC].isna().all(axis=1)
    quality["missing_rows"] = int(is_missing.sum())
    quality["missing_ratio"] = round(float(is_missing.mean()), 6)
    quality["missing_by_column"] = {
        col: int(raw[col].isna().sum()) for col in RAW_NUMERIC
    }
    quality["partially_missing_rows"] = int(
        (raw[RAW_NUMERIC].isna().any(axis=1) & ~is_missing).sum()
    )

    # ② 负值处理：电量/功率类出现负值 → 视为缺失（PRD 8.10）
    negative_masked: dict[str, int] = {}
    for col in ["Global_active_power", "Global_reactive_power", "Sub_metering_1",
                "Sub_metering_2", "Sub_metering_3"]:
        mask = raw[col] < 0
        negative_masked[col] = int(mask.sum())
        if mask.any():
            raw.loc[mask, col] = np.nan
    quality["negative_masked"] = negative_masked

    # ③ 时间解析（显式格式，兼容 '16/12/2006' 不补零写法）
    dt = pd.to_datetime(
        raw["Date"].str.strip() + " " + raw["Time"].str.strip(),
        format="%d/%m/%Y %H:%M:%S",
        errors="coerce",
    )
    bad_time = int(dt.isna().sum())
    quality["unparsable_datetime"] = bad_time
    if bad_time:
        logger.warning("无法解析的时间戳 %d 条，已剔除", bad_time)
        keep = dt.notna()
        raw, dt, is_missing = raw[keep], dt[keep], is_missing[keep]

    # ④ 构造明细表
    df = pd.DataFrame({"dt": dt.dt.strftime("%Y-%m-%d %H:%M:%S")})
    df["year"] = dt.dt.year.astype("int16")
    df["month"] = dt.dt.month.astype("int8")
    df["day"] = dt.dt.day.astype("int8")
    df["hour"] = dt.dt.hour.astype("int8")
    df["weekday"] = dt.dt.weekday.astype("int8")            # 0=周一 … 6=周日
    df["is_weekend"] = (df["weekday"] >= 5).astype("int8")
    df["period"] = period_of_hours(df["hour"].to_numpy())

    df["gap_kw"] = raw["Global_active_power"].to_numpy()
    df["gap_kwh"] = df["gap_kw"] / 60.0                      # R1：kW → kWh
    df["sm1_kwh"] = (raw["Sub_metering_1"] / 1000.0).to_numpy()
    df["sm2_kwh"] = (raw["Sub_metering_2"] / 1000.0).to_numpy()
    df["sm3_kwh"] = (raw["Sub_metering_3"] / 1000.0).to_numpy()
    df["unmetered_kwh"] = (                            # R2：未计量用电
        df["gap_kwh"] - df["sm1_kwh"] - df["sm2_kwh"] - df["sm3_kwh"]
    )
    df["voltage"] = raw["Voltage"].to_numpy()
    df["intensity"] = raw["Global_intensity"].to_numpy()
    df["reactive_power_kw"] = raw["Global_reactive_power"].to_numpy()
    df["is_missing"] = is_missing.to_numpy().astype("int8")

    # ⑤ 质量统计（时序连续性、越界、重复）
    n = len(df)
    span_minutes = (dt.max() - dt.min()).total_seconds() / 60 if n else 0
    quality.update(
        {
            "cleaned_rows": int(n),
            "time_start": str(dt.min()),
            "time_end": str(dt.max()),
            "expected_minutes_by_span": int(round(span_minutes)) + 1,
            "time_axis_gap": int(round(span_minutes)) + 1 - n,
            "duplicate_timestamps": int(dt.duplicated().sum()),
            "voltage_out_of_range_minutes": int(
                ((df["voltage"] < VOLTAGE_MIN) | (df["voltage"] > VOLTAGE_MAX)).sum()
            ),
            "active_minutes": int(df["gap_kwh"].notna().sum()),
            "active_minutes_ratio": round(float(df["gap_kwh"].notna().mean()), 6),
            "numeric_range": {
                col: {
                    "min": round(float(df[col].min()), 6),
                    "max": round(float(df[col].max()), 6),
                    "mean": round(float(df[col].mean()), 6),
                }
                for col in ["gap_kw", "gap_kwh", "sm1_kwh", "sm2_kwh", "sm3_kwh",
                            "unmetered_kwh", "voltage", "intensity", "reactive_power_kw"]
            },
        }
    )
    logger.info(
        "清洗完成：%d 行，缺失 %d 行（%.4f%%），时间 %s → %s，时间轴缺口 %d",
        n, quality["missing_rows"], quality["missing_ratio"] * 100,
        quality["time_start"], quality["time_end"], quality["time_axis_gap"],
    )
    return df, quality


def period_of_hours(hours: np.ndarray) -> np.ndarray:
    """按小时数组返回时段（peak / flat / valley）。"""
    period = np.full(len(hours), "flat", dtype=object)
    period[(hours >= PERIOD_HOURS["valley"][0]) & (hours <= PERIOD_HOURS["valley"][1])] = "valley"
    period[(hours >= PERIOD_HOURS["peak"][0]) & (hours <= PERIOD_HOURS["peak"][1])] = "peak"
    return period


def add_period_kwh(df: pd.DataFrame, value_col: str = "gap_kwh") -> pd.DataFrame:
    """把总量按峰/平/谷拆成三列（便于分组求和，R3 的按时段一侧）。"""
    for name in ("peak", "flat", "valley"):
        df[f"{name}_kwh"] = df[value_col].where(df["period"] == name)
    return df


def ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """安全的比率计算：分母为 0 或缺失时返回 NaN（比率不可 SUM，R6）。"""
    denom = denominator.where(denominator != 0)
    return numerator / denom


def daily_avg_kwh(consumption: pd.Series, active_minutes: pd.Series) -> pd.Series:
    """R5：日均用电按有效数据折算；active_minutes=0 时为 NULL。"""
    return ratio(consumption * 1440.0, active_minutes.where(active_minutes > 0))


def round_for_output(df: pd.DataFrame, columns: dict[str, str]) -> pd.DataFrame:
    """按输出精度舍入口径列（仅用于落盘可读性；校验使用未舍入值）。"""
    out = df.copy()
    for col, kind in columns.items():
        if col in out.columns:
            out[col] = out[col].round(PRECISION[kind])
    return out


def write_csv(df: pd.DataFrame, path: Path) -> None:
    """统一以 utf-8-sig 写出 CSV（Excel 直接打开不乱码）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding=CSV_ENCODING)
