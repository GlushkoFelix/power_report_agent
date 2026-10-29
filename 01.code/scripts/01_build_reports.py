# -*- coding: utf-8 -*-
"""01_build_reports.py —— 生成 4 张报表 CSV + 质量报告 + manifest（含 A1–A5 口径校验）

产物：
    02.data/reports/report_hourly.csv
    02.data/reports/report_daily.csv
    02.data/reports/report_monthly.csv
    02.data/reports/report_energy_structure.csv
    02.data/reports/manifest.json          （口径版本、行数、校验锚点、配置）
    02.data/quality/quality_report.json    （数据质量与清洗报告）

用法：
    .venv\\Scripts\\python.exe 01.code\\scripts\\01_build_reports.py [--force]

说明：
    - 默认幂等：产物齐全且未加 --force 时直接跳过；
    - A1–A3 校验基于**未舍入**的聚合结果（报表 CSV 按口径精度舍入，仅影响可读性）。
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import _common as C

# 报表列顺序（与 sql/reports.sql 一致）
HOURLY_COLS = [
    "dt_hour", "year", "month", "day", "hour", "weekday", "is_weekend", "period",
    "consumption_kwh", "sm1_kwh", "sm2_kwh", "sm3_kwh", "unmetered_kwh",
    "peak_kwh", "flat_kwh", "valley_kwh",
    "active_minutes", "expected_minutes", "coverage",
    "avg_power_kw", "max_power_kw", "min_power_kw", "avg_voltage", "avg_intensity",
]
DAILY_COLS = [
    "date", "year", "month", "day", "weekday", "is_weekend",
    "consumption_kwh", "sm1_kwh", "sm2_kwh", "sm3_kwh", "unmetered_kwh",
    "peak_kwh", "flat_kwh", "valley_kwh",
    "active_minutes", "expected_minutes", "coverage",
    "avg_power_kw", "max_power_kw", "min_power_kw", "avg_voltage", "avg_intensity",
    "daily_avg_kwh", "load_factor",
    "sm1_ratio", "sm2_ratio", "sm3_ratio", "unmetered_ratio",
    "peak_ratio", "flat_ratio", "valley_ratio",
    "peak_hour", "is_anomaly", "anomaly_reason", "is_partial_period", "partial_reason",
]
MONTHLY_COLS = [
    "year_month", "year", "month", "days_in_month",
    "consumption_kwh", "sm1_kwh", "sm2_kwh", "sm3_kwh", "unmetered_kwh",
    "peak_kwh", "flat_kwh", "valley_kwh",
    "active_minutes", "expected_minutes", "coverage",
    "avg_power_kw", "max_power_kw", "min_power_kw", "daily_avg_kwh",
    "mom_rate", "yoy_rate", "is_anomaly", "anomaly_reason",
    "is_partial_period", "partial_reason",
]
STRUCTURE_COLS = [
    "period", "is_total_row", "year",
    "kitchen_kwh", "laundry_kwh", "water_heating_ac_kwh", "other_unmetered_kwh",
    "kitchen_ratio", "laundry_ratio", "water_heating_ac_ratio", "other_unmetered_ratio",
    "peak_kwh", "flat_kwh", "valley_kwh",
    "peak_ratio", "flat_ratio", "valley_ratio",
    "total_kwh", "structure_closed", "time_closed",
]

# 输出精度映射（列名关键字 → 精度类别）
PRECISION_MAP: dict[str, str] = {}
for col in set(HOURLY_COLS + DAILY_COLS + MONTHLY_COLS + STRUCTURE_COLS):
    if col.endswith("_kwh"):
        PRECISION_MAP[col] = "kwh"
    elif col.endswith("_ratio") or col in {"coverage", "load_factor", "mom_rate", "yoy_rate",
                                           "structure_closed", "time_closed",
                                           "active_minutes_ratio"}:
        PRECISION_MAP[col] = "ratio"
    elif col.endswith("_kw"):
        PRECISION_MAP[col] = "power"
    elif col in {"avg_voltage", "voltage"}:
        PRECISION_MAP[col] = "voltage"
    elif col in {"avg_intensity", "intensity"}:
        PRECISION_MAP[col] = "intensity"

KEY_DAILY = ["year", "month", "day"]
KEY_MONTHLY = ["year", "month"]


# --------------------------------------------------------------------------------------
def build_hourly(minute: pd.DataFrame, logger) -> pd.DataFrame:
    """小时报：由分钟明细直接聚合（expected_minutes 恒 60）。"""
    g = minute.groupby(["year", "month", "day", "hour"], observed=True, sort=True)
    hourly = pd.DataFrame(
        {
            "weekday": g["weekday"].first(),
            "is_weekend": g["is_weekend"].first(),
            "consumption_kwh": g["gap_kwh"].sum(min_count=1),
            "sm1_kwh": g["sm1_kwh"].sum(min_count=1),
            "sm2_kwh": g["sm2_kwh"].sum(min_count=1),
            "sm3_kwh": g["sm3_kwh"].sum(min_count=1),
            "unmetered_kwh": g["unmetered_kwh"].sum(min_count=1),
            "peak_kwh": g["peak_kwh"].sum(min_count=1),
            "flat_kwh": g["flat_kwh"].sum(min_count=1),
            "valley_kwh": g["valley_kwh"].sum(min_count=1),
            "active_minutes": g["gap_kwh"].count(),
            "avg_power_kw": g["gap_kw"].mean(),
            "max_power_kw": g["gap_kw"].max(),
            "min_power_kw": g["gap_kw"].min(),
            "avg_voltage": g["voltage"].mean(),
            "avg_intensity": g["intensity"].mean(),
        }
    ).reset_index()

    hourly["dt_hour"] = (
        hourly["year"].astype(str) + "-" + hourly["month"].astype(str).str.zfill(2) + "-"
        + hourly["day"].astype(str).str.zfill(2) + " " + hourly["hour"].astype(str).str.zfill(2) + ":00:00"
    )
    hourly["period"] = C.period_of_hours(hourly["hour"].to_numpy())
    hourly["expected_minutes"] = 60
    hourly["coverage"] = C.ratio(hourly["active_minutes"], hourly["expected_minutes"])
    logger.info("小时报：%d 行", len(hourly))
    return hourly[HOURLY_COLS]


def build_daily(minute: pd.DataFrame, hourly: pd.DataFrame, logger) -> pd.DataFrame:
    """日报：由分钟明细直接聚合（expected_minutes 恒 1440），含占比、异常与完整性标记。"""
    g = minute.groupby(KEY_DAILY, observed=True, sort=True)
    daily = pd.DataFrame(
        {
            "weekday": g["weekday"].first(),
            "is_weekend": g["is_weekend"].first(),
            "consumption_kwh": g["gap_kwh"].sum(min_count=1),
            "sm1_kwh": g["sm1_kwh"].sum(min_count=1),
            "sm2_kwh": g["sm2_kwh"].sum(min_count=1),
            "sm3_kwh": g["sm3_kwh"].sum(min_count=1),
            "unmetered_kwh": g["unmetered_kwh"].sum(min_count=1),
            "peak_kwh": g["peak_kwh"].sum(min_count=1),
            "flat_kwh": g["flat_kwh"].sum(min_count=1),
            "valley_kwh": g["valley_kwh"].sum(min_count=1),
            "active_minutes": g["gap_kwh"].count(),
            "avg_power_kw": g["gap_kw"].mean(),
            "max_power_kw": g["gap_kw"].max(),
            "min_power_kw": g["gap_kw"].min(),
            "avg_voltage": g["voltage"].mean(),
            "avg_intensity": g["intensity"].mean(),
        }
    ).reset_index()
    daily["date"] = pd.to_datetime(
        dict(year=daily["year"], month=daily["month"], day=daily["day"])
    ).dt.strftime("%Y-%m-%d")
    daily["expected_minutes"] = 1440
    daily["coverage"] = C.ratio(daily["active_minutes"], daily["expected_minutes"])

    # 派生指标
    daily["daily_avg_kwh"] = C.daily_avg_kwh(daily["consumption_kwh"], daily["active_minutes"])
    daily["load_factor"] = C.ratio(daily["avg_power_kw"], daily["max_power_kw"])
    # 占比（R6：不可 SUM）
    for src, dst in [("sm1_kwh", "sm1_ratio"), ("sm2_kwh", "sm2_ratio"), ("sm3_kwh", "sm3_ratio"),
                     ("unmetered_kwh", "unmetered_ratio"), ("peak_kwh", "peak_ratio"),
                     ("flat_kwh", "flat_ratio"), ("valley_kwh", "valley_ratio")]:
        daily[dst] = C.ratio(daily[src], daily["consumption_kwh"])

    # 当日峰值整点（0–23，空值表示该日无有效数据）
    valid = hourly.dropna(subset=["consumption_kwh"])
    if len(valid):
        idx = valid.groupby(KEY_DAILY, observed=True)["consumption_kwh"].idxmax()
        peak = valid.loc[idx, KEY_DAILY + ["hour"]].rename(columns={"hour": "peak_hour"})
        daily = daily.merge(peak, on=KEY_DAILY, how="left")
    else:
        daily["peak_hour"] = np.nan

    # 逐日电压越界分钟数（硬规则用，不删除数据）
    minute_v = minute.assign(
        oob=((minute["voltage"] < C.VOLTAGE_MIN) | (minute["voltage"] > C.VOLTAGE_MAX)).astype("int8")
    )
    oob = minute_v.groupby(KEY_DAILY, observed=True)["oob"].sum().rename("voltage_oob_minutes").reset_index()
    daily = daily.merge(oob, on=KEY_DAILY, how="left")

    daily = _mark_anomaly_daily(daily)
    daily = _mark_partial_daily(daily, minute)
    daily = daily.drop(columns=["voltage_oob_minutes"])
    logger.info("日报：%d 行（异常 %d 天，不完整 %d 天）",
                len(daily), int(daily["is_anomaly"].sum()), int(daily["is_partial_period"].sum()))
    return daily[DAILY_COLS]


def _mark_anomaly_daily(daily: pd.DataFrame) -> pd.DataFrame:
    """近 30 天滚动均值 ±3σ + 4 条硬规则（PRD F2.5）。"""
    daily = daily.sort_values("date").reset_index(drop=True)
    prev = daily["consumption_kwh"].shift(1)
    roll_mean = prev.rolling(C.ANOMALY_WINDOW_DAYS, min_periods=C.ANOMALY_MIN_PERIODS).mean()
    roll_std = prev.rolling(C.ANOMALY_WINDOW_DAYS, min_periods=C.ANOMALY_MIN_PERIODS).std()
    hi = roll_mean + C.ANOMALY_SIGMA * roll_std
    lo = roll_mean - C.ANOMALY_SIGMA * roll_std

    prev_peak = daily["peak_ratio"].shift(1)
    peak_mean = prev_peak.rolling(C.ANOMALY_WINDOW_DAYS, min_periods=C.ANOMALY_MIN_PERIODS).mean()
    peak_std = prev_peak.rolling(C.ANOMALY_WINDOW_DAYS, min_periods=C.ANOMALY_MIN_PERIODS).std()

    reasons = []
    for i in range(len(daily)):
        hits = []
        cons = daily.at[i, "consumption_kwh"]
        if pd.notna(cons) and pd.notna(hi.iloc[i]):
            if cons > hi.iloc[i]:
                hits.append("rolling3sigma_high")
            elif cons < lo.iloc[i]:
                hits.append("rolling3sigma_low")
        if daily.at[i, "active_minutes"] == 0:
            hits.append("no_active_data")
        avg_p, max_p = daily.at[i, "avg_power_kw"], daily.at[i, "max_power_kw"]
        if (pd.notna(avg_p) and avg_p <= 0) or (pd.notna(max_p) and max_p <= 0):
            hits.append("zero_or_negative_power")
        oob = daily.at[i, "voltage_oob_minutes"]
        if pd.notna(oob) and oob >= C.VOLTAGE_OOB_MIN_MINUTES:
            hits.append("voltage_out_of_range")
        pr = daily.at[i, "peak_ratio"]
        if pd.notna(pr) and pd.notna(peak_mean.iloc[i]) and pd.notna(peak_std.iloc[i]):
            if abs(pr - peak_mean.iloc[i]) > C.ANOMALY_SIGMA * peak_std.iloc[i]:
                hits.append("peak_ratio_anomaly")
        reasons.append("|".join(hits))
    daily["anomaly_reason"] = reasons
    daily["is_anomaly"] = (daily["anomaly_reason"] != "").astype("int8")
    return daily


def _mark_partial_daily(daily: pd.DataFrame, minute: pd.DataFrame) -> pd.DataFrame:
    """标记首末日等不完整自然日（PRD 8.9.3）。"""
    t_min = pd.to_datetime(minute["dt"]).min()
    t_max = pd.to_datetime(minute["dt"]).max()
    dates = pd.to_datetime(daily["date"])
    is_first = dates == t_min.normalize()
    is_last = dates == t_max.normalize()

    reason = pd.Series("", index=daily.index, dtype="object")
    if t_min.time() != datetime.min.time():
        reason = reason.mask(is_first, f"数据自 {t_min:%H:%M} 开始")
    if t_max.time() != datetime(2000, 1, 1, 23, 59).time():
        reason = reason.mask(is_last, f"数据至 {t_max:%H:%M} 结束")
    daily["partial_reason"] = reason.values
    daily["is_partial_period"] = (daily["partial_reason"] != "").astype("int8")
    return daily


def build_monthly(daily: pd.DataFrame, logger) -> pd.DataFrame:
    """月报：由日报聚合（三级总量必然一致）。"""
    g = daily.groupby(KEY_MONTHLY, observed=True, sort=True)
    agg = g.agg(
        consumption_kwh=("consumption_kwh", "sum"),
        sm1_kwh=("sm1_kwh", "sum"),
        sm2_kwh=("sm2_kwh", "sum"),
        sm3_kwh=("sm3_kwh", "sum"),
        unmetered_kwh=("unmetered_kwh", "sum"),
        peak_kwh=("peak_kwh", "sum"),
        flat_kwh=("flat_kwh", "sum"),
        valley_kwh=("valley_kwh", "sum"),
        active_minutes=("active_minutes", "sum"),
        max_power_kw=("max_power_kw", "max"),
        min_power_kw=("min_power_kw", "min"),
        daily_avg_kwh_mean=("daily_avg_kwh", "mean"),
        voltage_oob_minutes=("is_partial_period", "size"),
    ).reset_index()

    # 加权平均（等价于分钟级均值）
    daily = daily.copy()
    daily["_p"] = daily["avg_power_kw"] * daily["active_minutes"]
    daily["_v"] = daily["avg_voltage"] * daily["active_minutes"]
    w = daily.groupby(KEY_MONTHLY, observed=True).agg(
        _p=("_p", "sum"), _v=("_v", "sum"), _am=("active_minutes", "sum")
    ).reset_index()
    agg = agg.merge(w, on=KEY_MONTHLY, how="left")
    agg["avg_power_kw"] = C.ratio(agg["_p"], agg["_am"].where(agg["_am"] > 0))
    agg["avg_voltage"] = C.ratio(agg["_v"], agg["_am"].where(agg["_am"] > 0))

    agg["year_month"] = agg["year"].astype(str) + "-" + agg["month"].astype(str).str.zfill(2)
    agg["days_in_month"] = [
        calendar.monthrange(int(y), int(m))[1] for y, m in zip(agg["year"], agg["month"])
    ]
    agg["expected_minutes"] = agg["days_in_month"] * 1440        # R4
    agg["coverage"] = C.ratio(agg["active_minutes"], agg["expected_minutes"])
    agg["daily_avg_kwh"] = C.daily_avg_kwh(agg["consumption_kwh"], agg["active_minutes"])

    # 同比 / 环比（比率，不可 SUM）
    agg = agg.sort_values(["year", "month"]).reset_index(drop=True)
    prev_month = agg["consumption_kwh"].shift(1)
    prev_year = agg["consumption_kwh"].shift(12)
    agg["mom_rate"] = C.ratio(agg["consumption_kwh"] - prev_month, prev_month)
    agg["yoy_rate"] = C.ratio(agg["consumption_kwh"] - prev_year, prev_year)

    agg = _mark_anomaly_monthly(agg)
    agg = _mark_partial_monthly(agg, daily)
    agg = agg.drop(columns=["_p", "_v", "_am", "daily_avg_kwh_mean", "voltage_oob_minutes"])
    logger.info("月报：%d 行（异常 %d 月，不完整 %d 月）",
                len(agg), int(agg["is_anomaly"].sum()), int(agg["is_partial_period"].sum()))
    return agg[MONTHLY_COLS]


def _mark_anomaly_monthly(monthly: pd.DataFrame) -> pd.DataFrame:
    prev = monthly["consumption_kwh"].shift(1)
    roll_mean = prev.rolling(C.ANOMALY_WINDOW_MONTHS, min_periods=C.ANOMALY_MIN_MONTHS).mean()
    roll_std = prev.rolling(C.ANOMALY_WINDOW_MONTHS, min_periods=C.ANOMALY_MIN_MONTHS).std()
    hi = roll_mean + C.ANOMALY_SIGMA * roll_std
    lo = roll_mean - C.ANOMALY_SIGMA * roll_std
    reasons = []
    for i in range(len(monthly)):
        hits = []
        cons = monthly.at[i, "consumption_kwh"]
        if pd.notna(cons) and pd.notna(hi.iloc[i]):
            if cons > hi.iloc[i]:
                hits.append("rolling3sigma_high")
            elif cons < lo.iloc[i]:
                hits.append("rolling3sigma_low")
        if monthly.at[i, "active_minutes"] == 0:
            hits.append("no_active_data")
        avg_p = monthly.at[i, "avg_power_kw"]
        if pd.notna(avg_p) and avg_p <= 0:
            hits.append("zero_or_negative_power")
        reasons.append("|".join(hits))
    monthly["anomaly_reason"] = reasons
    monthly["is_anomaly"] = (monthly["anomaly_reason"] != "").astype("int8")
    return monthly


def _mark_partial_monthly(monthly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    partial = daily[daily["is_partial_period"] == 1][KEY_MONTHLY + ["partial_reason"]]
    monthly = monthly.merge(partial, on=KEY_MONTHLY, how="left")
    monthly["partial_reason"] = monthly["partial_reason"].fillna("")
    monthly["is_partial_period"] = (monthly["partial_reason"] != "").astype("int8")
    return monthly


def build_structure(daily: pd.DataFrame, logger) -> pd.DataFrame:
    """用电结构报表：年份行 + 全期合计行（R3 两套正交拆分、R7 全期行标记）。"""
    g = daily.groupby("year", observed=True, sort=True)
    df = pd.DataFrame(
        {
            "kitchen_kwh": g["sm1_kwh"].sum(),                     # 用途：SM1 厨房
            "laundry_kwh": g["sm2_kwh"].sum(),                     # 用途：SM2 洗衣房
            "water_heating_ac_kwh": g["sm3_kwh"].sum(),            # 用途：SM3 热水器与空调
            "other_unmetered_kwh": g["unmetered_kwh"].sum(),       # 用途：未计量（其他电器）
            "peak_kwh": g["peak_kwh"].sum(),                       # 时段：峰
            "flat_kwh": g["flat_kwh"].sum(),                       # 时段：平
            "valley_kwh": g["valley_kwh"].sum(),                   # 时段：谷
            "total_kwh": g["consumption_kwh"].sum(),
        }
    ).reset_index()
    df["period"] = df["year"].astype(str)
    df["is_total_row"] = 0

    for prefix, cols in (
        ("kitchen", ["kitchen_kwh"]), ("laundry", ["laundry_kwh"]),
        ("water_heating_ac", ["water_heating_ac_kwh"]),
        ("other_unmetered", ["other_unmetered_kwh"]),
        ("peak", ["peak_kwh"]), ("flat", ["flat_kwh"]), ("valley", ["valley_kwh"]),
    ):
        df[f"{prefix}_ratio"] = C.ratio(df[cols[0]], df["total_kwh"])

    # 全期行 = 年份行合计（因此天然与年份行相加相等，R7 用 is_total_row 区分）
    total_row = {
        "period": "全期", "is_total_row": 1, "year": np.nan,
        **{c: df[c].sum() for c in ["kitchen_kwh", "laundry_kwh", "water_heating_ac_kwh",
                                    "other_unmetered_kwh", "peak_kwh", "flat_kwh",
                                    "valley_kwh", "total_kwh"]},
    }
    total_row.update({
        "kitchen_ratio": total_row["kitchen_kwh"] / total_row["total_kwh"],
        "laundry_ratio": total_row["laundry_kwh"] / total_row["total_kwh"],
        "water_heating_ac_ratio": total_row["water_heating_ac_kwh"] / total_row["total_kwh"],
        "other_unmetered_ratio": total_row["other_unmetered_kwh"] / total_row["total_kwh"],
        "peak_ratio": total_row["peak_kwh"] / total_row["total_kwh"],
        "flat_ratio": total_row["flat_kwh"] / total_row["total_kwh"],
        "valley_ratio": total_row["valley_kwh"] / total_row["total_kwh"],
    })
    df = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)
    df["year"] = df["year"].astype("Int16")

    # 闭合校验列（A2 / A3）
    df["structure_closed"] = (df["kitchen_kwh"] + df["laundry_kwh"]
                              + df["water_heating_ac_kwh"] + df["other_unmetered_kwh"]) - df["total_kwh"]
    df["time_closed"] = (df["peak_kwh"] + df["flat_kwh"] + df["valley_kwh"]) - df["total_kwh"]
    logger.info("结构报表：%d 行（年份 %d + 全期 1）", len(df), len(df) - 1)
    return df[STRUCTURE_COLS]


def run_checks(hourly: pd.DataFrame, daily: pd.DataFrame, monthly: pd.DataFrame,
               structure: pd.DataFrame, quality: dict, logger) -> dict:
    """A1–A5 口径校验（基于未舍入值）。"""
    h_sum = float(hourly["consumption_kwh"].sum())
    d_sum = float(daily["consumption_kwh"].sum())
    m_sum = float(monthly["consumption_kwh"].sum())
    level_gap = max(abs(h_sum - d_sum), abs(d_sum - m_sum))

    total_row = structure[structure["is_total_row"] == 1].iloc[0]
    checks = {
        "A1_levels_equal": {
            "hourly_total_kwh": round(h_sum, 6),
            "daily_total_kwh": round(d_sum, 6),
            "monthly_total_kwh": round(m_sum, 6),
            "max_gap_kwh": level_gap,
            "passed": level_gap <= 1e-6,
        },
        "A2_structure_closed": {
            "max_abs_error_kwh": float(structure["structure_closed"].abs().max()),
            "passed": bool(structure["structure_closed"].abs().max() <= 1e-6),
        },
        "A3_time_closed": {
            "max_abs_error_kwh": float(structure["time_closed"].abs().max()),
            "passed": bool(structure["time_closed"].abs().max() <= 1e-6),
        },
        "A4_unmetered_share": {
            "unmetered_kwh": round(float(total_row["other_unmetered_kwh"]), 6),
            "total_kwh": round(float(total_row["total_kwh"]), 6),
            "share": round(float(total_row["other_unmetered_ratio"]), 6),
            "note": "仅记录，不作硬阈值（PRD 8.11）",
        },
        "A5_row_counts": {
            "minute_rows": quality["cleaned_rows"],
            "minute_rows_expected": quality["source_rows"],
            "hourly_rows": int(len(hourly)),
            "daily_rows": int(len(daily)),
            "monthly_rows": int(len(monthly)),
            "structure_rows": int(len(structure)),
            "passed": quality["cleaned_rows"] == quality["source_rows"],
        },
    }
    ok = all(v.get("passed", True) for v in checks.values())
    log = logger.info if ok else logger.error
    log("口径校验 A1–A5：%s", "全部通过" if ok else "存在未通过项")
    for name, detail in checks.items():
        logger.info("  %s → %s", name, json.dumps(detail, ensure_ascii=False))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 4 张报表 CSV、质量报告与 manifest")
    parser.add_argument("--force", action="store_true", help="强制重算（覆盖已存在产物）")
    args = parser.parse_args()

    logger = C.setup_logger("01_build_reports")
    outputs = [
        C.REPORTS_DIR / "report_hourly.csv",
        C.REPORTS_DIR / "report_daily.csv",
        C.REPORTS_DIR / "report_monthly.csv",
        C.REPORTS_DIR / "report_energy_structure.csv",
        C.REPORTS_DIR / "manifest.json",
        C.QUALITY_DIR / "quality_report.json",
    ]
    if all(p.exists() for p in outputs) and not args.force:
        logger.info("产物已存在，跳过（如需重算请加 --force）")
        return 0

    minute, quality = C.parse_clean_minute(logger)
    minute = C.add_period_kwh(minute)                    # 拆出 peak/flat/valley_kwh 三列

    hourly = build_hourly(minute, logger)
    daily = build_daily(minute, hourly, logger)
    monthly = build_monthly(daily, logger)
    structure = build_structure(daily, logger)
    checks = run_checks(hourly, daily, monthly, structure, quality, logger)

    # 落盘（按口径精度舍入，仅影响可读性）
    for frame, name, cols in (
        (hourly, "report_hourly", HOURLY_COLS),
        (daily, "report_daily", DAILY_COLS),
        (monthly, "report_monthly", MONTHLY_COLS),
        (structure, "report_energy_structure", STRUCTURE_COLS),
    ):
        rounded = C.round_for_output(frame, {c: PRECISION_MAP[c] for c in cols if c in PRECISION_MAP})
        C.write_csv(rounded, C.REPORTS_DIR / f"{name}.csv")
        logger.info("已写出 %s.csv（%d 行）", name, len(frame))

    # 质量报告（补充日级覆盖率与异常分布）
    quality.update(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "caliber_version": C.CALIBER_VERSION,
            "daily": {
                "rows": int(len(daily)),
                "coverage_min": round(float(daily["coverage"].min()), 6),
                "coverage_mean": round(float(daily["coverage"].mean()), 6),
                "days_full_coverage": int((daily["coverage"] >= 1).sum()),
                "days_with_zero_active": int((daily["active_minutes"] == 0).sum()),
                "anomaly_days": int(daily["is_anomaly"].sum()),
                "partial_days": int(daily["is_partial_period"].sum()),
                "anomaly_rule_counts": {
                    rule: int(daily["anomaly_reason"].str.contains(rule, regex=False).sum())
                    for rule in ["rolling3sigma_high", "rolling3sigma_low", "no_active_data",
                                 "zero_or_negative_power", "voltage_out_of_range",
                                 "peak_ratio_anomaly"]
                },
            },
            "monthly": {
                "rows": int(len(monthly)),
                "anomaly_months": int(monthly["is_anomaly"].sum()),
                "partial_months": int(monthly["is_partial_period"].sum()),
            },
        }
    )
    C.QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    (C.QUALITY_DIR / "quality_report.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    manifest = {
        "caliber_version": C.CALIBER_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator": "01.code/scripts/01_build_reports.py",
        "source_file": str(C.RAW_FILE.name),
        "config": {
            "period_hours": C.PERIOD_HOURS,
            "anomaly": {
                "window_days": C.ANOMALY_WINDOW_DAYS,
                "min_periods": C.ANOMALY_MIN_PERIODS,
                "sigma": C.ANOMALY_SIGMA,
                "window_months": C.ANOMALY_WINDOW_MONTHS,
                "voltage_range": [C.VOLTAGE_MIN, C.VOLTAGE_MAX],
                "rules": ["rolling3sigma", "no_active_data", "zero_or_negative_power",
                          "voltage_out_of_range", "peak_ratio_anomaly"],
            },
            "precision": C.PRECISION,
            "csv_encoding": C.CSV_ENCODING,
        },
        "row_counts": {
            "minute": int(len(minute)),
            "hourly": int(len(hourly)),
            "daily": int(len(daily)),
            "monthly": int(len(monthly)),
            "structure": int(len(structure)),
        },
        "time_range": {"start": quality["time_start"], "end": quality["time_end"]},
        "outputs": [p.name for p in outputs if p.suffix == ".csv"],
        "checks": checks,
        "notes": [
            "A1–A3 校验基于未舍入的聚合结果；CSV 按 PRD 8.10 精度舍入，仅影响可读性。",
            "分钟级明细仅入库 reports.db，不导出 CSV（PRD 7.3）。",
            "三级总量相等由同一分钟级数据源聚合保证。",
        ],
    }
    C.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (C.REPORTS_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info("全部产物已生成，口径版本 %s", C.CALIBER_VERSION)
    return 0 if checks["A1_levels_equal"]["passed"] and checks["A2_structure_closed"]["passed"] \
        and checks["A3_time_closed"]["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
