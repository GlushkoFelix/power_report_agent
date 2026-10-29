"""智电析报 · 图表服务（PRD F3.1–F3.4 / UIUX 7.1–7.2，交付物 4 第三批）。

职责：把**已经执行完成的结果表**（PRD F3.1：表格与图表使用**同一份结果数据**）
转成一份 Plotly figure JSON。本模块只做纯计算，不调 LLM。

本轮 grilling 定案（11 项）中由本模块落地 8 项：

| 决策 | 落地位置 |
| --- | --- |
| ③ 类型选择：`chart_hint` 优先 + 形状兜底 | `_choose_chart_type()` / `_fallback_chart_type()` |
| ④ 语义色板 + 红线 R3 | `PERIOD_COLORS` / `PURPOSE_COLORS` / `_apply_r3()` |
| ⑥ TopN=12 合并「其他」+ 抽稀 500 点 | `_merge_others()` / `_downsample_indices()` |
| ⑦ 缺采断开：相邻间隔 > 1.5×粒度 | `_apply_gap_breaks()` |
| ⑧ 图表画不出时降级（`figure=null` + `reason`） | `_degrade()` / `build_chart()` 的兜底 except |
| ⑨ 多指标列：同量纲多系列，跨量纲只画主指标 | `_apply_unit_filter()` |
| ⑪ 主题边界：只写语义色与文案，不写底色/字体色 | `_base_layout()`（`template` 一并剔除） |

三条实现要点：

1. **`fig.to_json()` 里的 `template` 必须剔除**：plotly 会带上内置默认模板（数十 KB），
   既撑大 `analyses.chart_json`，又与决策 ⑪「主题交前端」冲突；
2. **降级不是异常**：主链路里图表是增值项，画不出来时返回 `figure=None` + `reason`
   让前端提示「该结果不适合可视化」，**不让整条分析失败**（P4 失败不静默）。
   唯一会抛异常的情况是 `strict_type=True`（`rerender` 显式指定的类型画不出来）；
3. **红线 R3 的图表层落地**：一张图只讲**一种拆分**——指标列里同时出现用途类
   （`sm1/sm2/sm3/unmetered` 等）与时段类（`peak/flat/valley_kwh`）时只保留一套
   （否则饼图各扇区之和 ≈ 2× 实际电量，属口径错误）。分类轴不受影响。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

import plotly.graph_objects as go

from app.core.errors import BizError, chart_unsupported
from app.db.session import control_session

logger = logging.getLogger("zdxb.chart")

__all__ = [
    "CHART_TYPES",
    "TYPE_LABELS",
    "DEFAULT_TOP_N",
    "DOWNSAMPLE_LIMIT",
    "ChartOutcome",
    "build_chart",
    "rerender_chart",
]

# ── 图表类型（PRD F3.2 六种；UIUX 5.1 的控件先暴露折线/柱状/饼图/表格四种）──
CHART_TYPES: tuple[str, ...] = ("line", "bar", "area", "pie", "scatter", "table")
TYPE_LABELS: dict[str, str] = {
    "line": "折线", "bar": "柱状", "area": "面积",
    "pie": "饼图", "scatter": "散点", "table": "表格",
}
HINT_TO_TYPE: dict[str, str] = {label: code for code, label in TYPE_LABELS.items()}

#: 分类类图的 TopN（决策 ⑥：类别数 > 12 时取前 12，余项合并为「其他」一项）
DEFAULT_TOP_N = 12
MIN_TOP_N, MAX_TOP_N = 3, 50

#: 折线/面积/散点的抽稀上限（决策 ⑥）
DOWNSAMPLE_LIMIT = 500

#: 多系列上限：超过则截断（避免宽表把 `chart_json` 撑爆）
MAX_SERIES = 8

#: 缺采判定：相邻两点时间间隔 > 粒度基准 × 1.5 视为缺口（决策 ⑦）
GAP_FACTOR = 1.5

#: `chart_json` 体积上限（与 `result_rows_json` 的 200KB 口径一致）
CHART_JSON_LIMIT = 200 * 1024

# ── 语义色（UIUX 4.2：全站统一、严禁混用）──────────────────────────────────
COLOR_PEAK = "#E8590C"       # 峰段
COLOR_FLAT = "#1D4ED8"       # 平段（也是主趋势线颜色）
COLOR_VALLEY = "#0CA678"     # 谷段
COLOR_UNMETERED = "#7048E8"  # 未计量
COLOR_ANOMALY = "#E03131"    # 异常
COLOR_MISSING = "#ADB5BD"    # 缺采

#: 用途色板（UIUX 4.2；与时段语义色**互不共用**，对应红线 R3 的视觉侧）
COLOR_KITCHEN = "#0B7285"
COLOR_LAUNDRY = "#15AABF"
COLOR_WATER_AC = "#4C6EF5"

PERIOD_COLORS: dict[str, str] = {"peak": COLOR_PEAK, "flat": COLOR_FLAT, "valley": COLOR_VALLEY}
PURPOSE_COLORS: dict[str, str] = {
    "kitchen": COLOR_KITCHEN, "laundry": COLOR_LAUNDRY,
    "water_heating_ac": COLOR_WATER_AC, "unmetered": COLOR_UNMETERED,
}

#: 无单一语义归属的多系列色板：**青灰色阶**，刻意避开全部 6 个语义色与 4 个用途色。
#: 为什么不能用蓝色系：`#1D4ED8` 是「平段」语义色，若同时又有「峰段橙红」出现，
#: 读者会把「总用电量」当作「平段」（与红线 R3 的视觉侧同源）。
#: 单系列时仍用 `COLOR_FLAT`（UIUX 7.2：主趋势线用平段蓝），只有多系列才需要区分度。
DEFAULT_SERIES_COLORS: tuple[str, ...] = (
    "#5B6B7F", "#8C9AAB", "#3E4C5E", "#A9B4C2", "#6E7F94", "#4A5A6B", "#BBC4D0", "#2F3B49",
)

# ── 列语义（依据 PRD 8.3 两套正交拆分 / 8.9 各报表字段规格）────────────────
#: 时间列 → 该列自身的最细粒度
TIME_COLUMNS: dict[str, str] = {
    "dt": "minute", "timestamp": "minute", "dt_hour": "hour",
    "date": "day", "year_month": "month",
}
#: 时间派生维度列（值本身是离散数字，只能作分类轴，不当连续时间轴）
TIME_PART_COLUMNS: tuple[str, ...] = ("year", "month", "day", "hour", "weekday")

#: 时段拆分指标列（PRD 8.9）→ 语义色
PERIOD_SERIES: dict[str, str] = {"peak_kwh": "peak", "flat_kwh": "flat", "valley_kwh": "valley"}
#: 用途拆分指标列（PRD 8.3 / 8.9）→ 用途色板
PURPOSE_SERIES: dict[str, str] = {
    "sm1_kwh": "kitchen", "sm2_kwh": "laundry", "sm3_kwh": "water_heating_ac",
    "kitchen_kwh": "kitchen", "laundry_kwh": "laundry",
    "water_heating_ac_kwh": "water_heating_ac",
    "unmetered_kwh": "unmetered", "other_unmetered_kwh": "unmetered",
}

#: 维度列**取值** → 拆分归属（结果按 `period` 分组或按用途分组时识别用）
PERIOD_VALUES: dict[str, str] = {
    "peak": "peak", "flat": "flat", "valley": "valley", "峰": "peak", "平": "flat", "谷": "valley",
}
PURPOSE_VALUES: dict[str, str] = {
    "kitchen": "kitchen", "厨房": "kitchen",
    "laundry": "laundry", "洗衣房": "laundry",
    "water_heating_ac": "water_heating_ac", "热水器与空调": "water_heating_ac", "热水空调": "water_heating_ac",
    "unmetered": "unmetered", "未计量": "unmetered", "其他": "unmetered", "其他电器": "unmetered",
}

#: 比率型列（PRD 红线 R6：不可 SUM，也不作图表主指标）
RATIO_KEYWORDS = ("ratio", "rate", "factor", "coverage", "share", "closed")
#: 标志/属性型数值列（0/1 或编码值，不能当指标画）
FLAG_NAMES = frozenset({
    "is_weekend", "is_anomaly", "is_missing", "is_partial_period", "is_total_row",
})
NON_METRIC_NUMERIC = frozenset({"peak_hour", "days_in_month", "expected_minutes"})
#: 这些列名即使取值"看起来是数字"也不是指标（如结构表的 `period` 存 '2006'/'全期'）：
#: 它们是分组维度，被当成指标会把年份画成一条"趋势线"
DIMENSION_LIKE_NAMES = frozenset(TIME_COLUMNS) | set(TIME_PART_COLUMNS) | {"period", "time"}

#: 「合计」型列（饼图里与构成列同时出现会重复计数，需剔除）
TOTAL_SERIES = frozenset({"consumption_kwh", "total_kwh", "gap_kwh", "kwh", "consumption"})

#: 列名 → 中文展示名（schema 无 `origin_name` 时的兜底标签）
COLUMN_LABELS: dict[str, str] = {
    "consumption_kwh": "总用电量", "total_kwh": "总电量", "gap_kwh": "分钟电量",
    "peak_kwh": "峰段电量", "flat_kwh": "平段电量", "valley_kwh": "谷段电量",
    "sm1_kwh": "厨房", "sm2_kwh": "洗衣房", "sm3_kwh": "热水器与空调",
    "kitchen_kwh": "厨房", "laundry_kwh": "洗衣房", "water_heating_ac_kwh": "热水器与空调",
    "unmetered_kwh": "未计量", "other_unmetered_kwh": "未计量",
    "gap_kw": "总有功功率", "avg_power_kw": "平均功率", "max_power_kw": "最大功率",
    "min_power_kw": "最小功率", "reactive_power_kw": "无功功率",
    "avg_voltage": "平均电压", "voltage": "电压",
    "avg_intensity": "平均电流", "intensity": "电流",
    "active_minutes": "有效分钟数", "coverage": "数据覆盖率",
    "daily_avg_kwh": "日均用电", "load_factor": "负荷率",
    "mom_rate": "环比", "yoy_rate": "同比",
}

#: 时间解析格式（按粒度）
_TIME_FORMATS: dict[str, tuple[str, ...]] = {
    "minute": ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"),
    "hour": ("%Y-%m-%d %H", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"),
    "day": ("%Y-%m-%d",),
    "month": ("%Y-%m",),
}
#: 粒度基准间隔（秒；月按 30 天估计）
_GAP_STEP_SECONDS: dict[str, float] = {
    "minute": 60.0, "hour": 3600.0, "day": 86400.0, "month": 30.0 * 86400.0,
}

#: 粒度（`rerender` 的重聚合）→ 时间截断格式；rank 用于判断"能否再拆细"
GRANULARITY_FORMATS: dict[str, str | None] = {
    "小时": "%Y-%m-%d %H", "日": "%Y-%m-%d", "月": "%Y-%m", "年": "%Y", "全期": None,
}
GRANULARITY_RANK: dict[str, int] = {"小时": 1, "日": 2, "月": 3, "年": 4, "全期": 5}

#: 量纲 → 跨周期聚合方式（口径：电量可加、功率/电压/电流取均值、比率列不参与）
UNIT_AGG: dict[str, str] = {
    "kWh": "sum", "分钟": "sum",
    "kW": "mean", "V": "mean", "A": "mean", "天": "mean", "整点(0-23)": "mean",
}

#: 指标中文名 → 候选物理列（`slot.metric` 是中文标签，需映射回列名）
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "总用电量": ("consumption_kwh", "total_kwh", "gap_kwh"),
    "总电量": ("consumption_kwh", "total_kwh"),
    "峰段用电量": ("peak_kwh",), "峰段电量": ("peak_kwh",),
    "平段用电量": ("flat_kwh",), "平段电量": ("flat_kwh",),
    "谷段用电量": ("valley_kwh",), "谷段电量": ("valley_kwh",),
    "峰平谷": ("peak_kwh", "flat_kwh", "valley_kwh"),
    "未计量用电量": ("unmetered_kwh", "other_unmetered_kwh"), "未计量": ("unmetered_kwh",),
    "厨房": ("sm1_kwh", "kitchen_kwh"), "洗衣房": ("sm2_kwh", "laundry_kwh"),
    "热水器与空调": ("sm3_kwh", "water_heating_ac_kwh"), "空调": ("sm3_kwh", "water_heating_ac_kwh"),
    "最大功率": ("max_power_kw",), "平均功率": ("avg_power_kw",), "最小功率": ("min_power_kw",),
    "功率": ("max_power_kw", "avg_power_kw", "gap_kw"),
    "电压": ("avg_voltage", "voltage"), "电流": ("avg_intensity", "intensity"),
    "覆盖率": ("coverage",), "日均用电": ("daily_avg_kwh",), "有效分钟": ("active_minutes",),
}

#: `slot.dimension` 关键词 → 拆分归属（红线 R3 收敛时优先保留哪一套）
_DIMENSION_PERIOD_WORDS = ("时段", "峰平谷", "峰段", "平段", "谷段")
_DIMENSION_PURPOSE_WORDS = ("用途", "设备", "分表", "电器", "厨房", "洗衣房", "热水", "空调")


# ══════════════════════════════════════════════════════════════════════════
#  输出结构
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class ChartOutcome:
    """一次图表生成的完整结果（SSE `chart` 事件与 `POST /charts/rerender` 共用）。

    `figure=None` 表示**降级**：此时 `fallback="table"` 与 `reason` 必有值，
    前端据此提示「该结果不适合可视化」而**不视为失败**（决策 ⑧）。
    """

    figure: dict[str, Any] | None
    chart_type: str
    fallback: str | None = None
    reason: str | None = None
    unit: str | None = None
    x_axis: str | None = None
    series: list[str] = field(default_factory=list)
    #: 红线 R3 是否发生过收敛（用途 + 时段两套拆分同现 → 只留一套）
    r3_adjusted: bool = False
    r3_note: str | None = None
    dropped_columns: list[str] = field(default_factory=list)
    excluded_columns: list[str] = field(default_factory=list)
    top_n: int | None = None
    merged_count: int = 0
    downsampled_from: int | None = None
    gap_count: int = 0

    def to_payload(self) -> dict[str, Any]:
        """转成可 JSON 序列化的载荷（即 PRD 9.2 的 `chart` 事件 data）。"""
        return {
            "figure": self.figure,
            "chart_type": self.chart_type,
            "fallback": self.fallback,
            "reason": self.reason,
            "unit": self.unit,
            "x_axis": self.x_axis,
            "series": self.series,
            "r3_adjusted": self.r3_adjusted,
            "r3_note": self.r3_note,
            "dropped_columns": self.dropped_columns,
            "excluded_columns": self.excluded_columns,
            "top_n": self.top_n,
            "merged_count": self.merged_count,
            "downsampled_from": self.downsampled_from,
            "gap_count": self.gap_count,
        }


@dataclass
class ColumnInfo:
    """结果列 + 语义画像（图表选型与配色的唯一依据）。"""

    name: str
    label: str
    kind: str                    # time / time_part / metric / ratio / dimension / flag
    unit: str | None
    values: list[Any]
    numeric: bool = False
    #: 拆分归属（仅 metric 有意义：period / purpose）
    partition: str | None = None
    #: 拆分内的身份（peak / flat / valley / kitchen / ...）
    series: str | None = None
    #: 命中的 schema 物理列名（用于取 unit）
    physical: str | None = None

    def distinct(self) -> list[Any]:
        """去重后的非空取值（保持出现顺序）。"""
        seen: list[Any] = []
        for value in self.values:
            if value is None or value == "":
                continue
            if value not in seen:
                seen.append(value)
        return seen


def _degrade(reason: str, **extra: Any) -> ChartOutcome:
    """降级结果：`figure=None` + `fallback="table"` + 原因（前端提示用）。"""
    outcome = ChartOutcome(figure=None, chart_type="table", fallback="table", reason=reason)
    for key, value in extra.items():
        setattr(outcome, key, value)
    return outcome


# ══════════════════════════════════════════════════════════════════════════
#  通用小工具
# ══════════════════════════════════════════════════════════════════════════


def _to_number(value: Any) -> float | None:
    """把结果值转成数字；字符串数字（上传表的 TEXT 列）也要认得。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if math.isnan(value) or math.isinf(value) else float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        return None if math.isnan(number) or math.isinf(number) else number
    return None


def _is_numeric(values: Sequence[Any]) -> bool:
    filled = [value for value in values if value is not None and value != ""]
    return bool(filled) and all(_to_number(value) is not None for value in filled)


def _is_ratio(name: str, unit: str | None = None) -> bool:
    haystack = f"{name} {unit or ''}".lower()
    return any(keyword in haystack for keyword in RATIO_KEYWORDS)


def _clean_unit(unit: str | None) -> str | None:
    if not unit or not unit.strip() or unit.strip() == "—":
        return None
    return unit.strip()


def _fmt_number(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _parse_time(value: Any, granularity: str) -> datetime | None:
    """按粒度解析时间列取值（结果快照里的时间是 TEXT）。"""
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in _TIME_FORMATS.get(granularity, ()):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _format_time(value: datetime, granularity: str) -> str:
    return value.strftime(_TIME_FORMATS[granularity][0])


# ══════════════════════════════════════════════════════════════════════════
#  ① 列语义识别
# ══════════════════════════════════════════════════════════════════════════


def _schema_index(schema_columns: Iterable[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """schema 列索引：物理名与原始中文名都能命中。"""
    index: dict[str, dict[str, Any]] = {}
    for column in schema_columns or []:
        name = str(column.get("name") or "").strip()
        if name:
            index[name] = column
        origin = str(column.get("origin_name") or "").strip()
        if origin and origin not in index:
            index[origin] = column
    return index


def _semantic_of_name(name: str) -> tuple[str | None, str | None]:
    """按列名判定拆分归属：返回 `(partition, series)`。"""
    if name in PERIOD_SERIES:
        return "period", PERIOD_SERIES[name]
    if name in PURPOSE_SERIES:
        return "purpose", PURPOSE_SERIES[name]
    return None, None


def _semantic_of_values(values: Sequence[Any]) -> tuple[str | None, str | None]:
    """按**取值**判定维度列的拆分归属（如 `period` 列取值为 peak/flat/valley）。"""
    filled = [str(value).strip().lower() for value in values if value not in (None, "")]
    if len(filled) < 2:
        return None, None
    distinct = set(filled)
    if distinct and distinct <= set(PERIOD_VALUES):
        return "period", None
    if distinct and distinct <= set(PURPOSE_VALUES):
        return "purpose", None
    return None, None


def _label_of(name: str, schema_entry: dict[str, Any] | None) -> str:
    """展示名：schema 的原始中文名（上传表）→ 内置中文词典 → 物理列名。"""
    if schema_entry:
        origin = str(schema_entry.get("origin_name") or "").strip()
        if origin and origin != name:
            return origin
        description = str(schema_entry.get("description") or "").strip()
        # 只有"短、且不像公式/带单位括号"的说明才当标签用，否则退回词典
        if (
            description
            and len(description) <= 12
            and not description.endswith(")")
            and not any(char in description for char in "=（，,.=")
        ):
            return description
    return COLUMN_LABELS.get(name, name)


def _column_names(columns: Iterable[Any]) -> list[str]:
    """结果列可能来自 `[{"name":..,"type":..}]` 或 `["a", "b"]`（快照/直调两种形态）。"""
    names: list[str] = []
    for column in columns or []:
        if isinstance(column, dict):
            name = str(column.get("name") or "").strip()
        else:
            name = str(column).strip()
        if name:
            names.append(name)
    return names


def _profile_columns(
    columns: Iterable[Any],
    rows: Sequence[dict[str, Any]],
    schema_columns: Iterable[dict[str, Any]] | None,
) -> list[ColumnInfo]:
    """识别每一列的语义：时间 / 时间维度 / 指标 / 比率 / 维度 / 标志。

    判定顺序（先命中的先算）：时间列 → 时间维度 → 标志列 → 比率列 →
    拆分指标列（`peak_kwh` 等，直接带语义色）→ schema 标记可求和 → 取值全为数字 → 维度列。
    """
    index = _schema_index(schema_columns)
    infos: list[ColumnInfo] = []
    for name in _column_names(columns):
        entry = index.get(name)
        values = [row.get(name) for row in rows]
        numeric = _is_numeric(values)
        unit = _clean_unit((entry or {}).get("unit"))
        agg = str((entry or {}).get("agg") or "")
        physical = str((entry or {}).get("name") or "") or None

        partition, series = _semantic_of_name(name)
        if partition is None and physical and physical != name:
            partition, series = _semantic_of_name(physical)
        # 维度列的拆分归属靠取值判定（如 `period` 列）
        if partition is None:
            partition, series = _semantic_of_values(values)

        if name in TIME_COLUMNS:
            kind = "time"
        elif name in TIME_PART_COLUMNS:
            kind = "time_part"
        elif name in FLAG_NAMES or name in NON_METRIC_NUMERIC:
            kind = "flag"
        elif _is_ratio(name, unit):
            kind = "ratio"
        elif name in PERIOD_SERIES or name in PURPOSE_SERIES:
            kind = "metric"
        elif agg == "sum":
            kind = "metric"
        elif numeric and name not in DIMENSION_LIKE_NAMES:
            kind = "metric"
        else:
            kind = "dimension"

        infos.append(
            ColumnInfo(
                name=name, label=_label_of(name, entry), kind=kind, unit=unit,
                values=list(values), numeric=numeric, partition=partition,
                series=series, physical=physical,
            )
        )
    return infos


# ══════════════════════════════════════════════════════════════════════════
#  ② 轴与指标的角色分配
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class _Shape:
    """结果表的"可绘制形状"：时间轴 / 分类轴 / 指标列（已过 R3 与量纲筛选）。"""

    time_axis: ColumnInfo | None
    category_axis: ColumnInfo | None
    metrics: list[ColumnInfo]
    unit: str | None
    rows: Sequence[dict[str, Any]]
    #: **未做量纲筛选**的指标列：散点图专用于两个不同量纲的关系（如电量 vs 功率），
    #: 若也按量纲筛掉就永远画不出散点（决策 ⑨ 的例外，见 `_scatter_source`）
    metrics_raw: list[ColumnInfo] = field(default_factory=list)

    @property
    def n_points(self) -> int:
        return len(self.rows)


def _pick_time_axis(infos: Sequence[ColumnInfo]) -> ColumnInfo | None:
    """连续时间轴：优先 `dt`，其次 `dt_hour` / `date` / `year_month`（能解析出时间才认）。"""
    for info in infos:
        if info.kind != "time":
            continue
        granularity = TIME_COLUMNS[info.name]
        if any(_parse_time(value, granularity) is not None for value in info.values):
            return info
    return None


def _pick_category_axis(
    infos: Sequence[ColumnInfo], time_axis: ColumnInfo | None
) -> ColumnInfo | None:
    """分类轴：语义拆分维度（`period`）> 低基数维度列 > 时间维度列 > 标志列。"""
    candidates = [
        info for info in infos
        if info.kind in ("dimension", "time_part", "flag")
        and info is not time_axis
        and 2 <= len(info.distinct()) <= 200
    ]
    if not candidates:
        return None

    def sort_key(info: ColumnInfo) -> tuple[int, int]:
        semantic = 0 if info.partition else 1        # 语义维度最优先
        kind_rank = {"dimension": 0, "time_part": 1, "flag": 2}.get(info.kind, 3)
        return (semantic, kind_rank, len(info.distinct()))

    return sorted(candidates, key=sort_key)[0]


def _partition_group(metrics: Sequence[ColumnInfo], partition: str) -> list[ColumnInfo]:
    return [metric for metric in metrics if metric.partition == partition]


def _preferred_partition(slot: dict[str, Any]) -> str:
    """红线 R3 收敛方向：`slot.dimension` → `slot.metric` → **默认保留「按用途」**。

    默认取用途的理由：用途四项（厨房 + 洗衣房 + 热水空调 + 未计量）**闭合为总电量**，
    信息量与可解释性最强；时段三项同样闭合，但"按时段"更适合用户显式指定。
    """
    dimension = str(slot.get("dimension") or "")
    if any(word in dimension for word in _DIMENSION_PERIOD_WORDS):
        return "period"
    if any(word in dimension for word in _DIMENSION_PURPOSE_WORDS):
        return "purpose"
    metric = str(slot.get("metric") or "")
    if any(keyword in metric for keyword in ("峰", "平", "谷", "时段")):
        return "period"
    if any(keyword in metric for keyword in ("厨房", "洗衣房", "热水", "空调", "未计量", "分表")):
        return "purpose"
    return "purpose"


def _apply_r3(metrics: Sequence[ColumnInfo], slot: dict[str, Any]) -> tuple[list[ColumnInfo], ChartOutcome | None]:
    """红线 R3 的**图表层**落地：一张图只讲一种拆分。

    命中条件：指标列里**同时**出现用途类与时段类（例：`SELECT kitchen_kwh, …, peak_kwh, …`）。
    不命中（合法）的情况：只有一套拆分 + 合计列（"总量与构成"同图是允许的，
    因为"峰 + 平 + 谷 = 总电量"成立）；分类轴本身是 `period` 或用途维度时也不收敛
    （那是"某一路的时段分布"，属单一拆分）。
    """
    period = _partition_group(metrics, "period")
    purpose = _partition_group(metrics, "purpose")
    if not period or not purpose:
        return list(metrics), None

    keep = _preferred_partition(slot)
    dropped = [metric for metric in (period if keep == "purpose" else purpose)]
    kept = [metric for metric in metrics if metric not in dropped]
    dropped_names = [metric.name for metric in dropped]
    kept_label = "按时段" if keep == "period" else "按用途"
    return kept, ChartOutcome(
        figure=None,
        chart_type="table",
        r3_adjusted=True,
        r3_note=(
            f"结果同时含「按用途」与「按时段」两套拆分，按口径红线 R3 只展示{kept_label}一套"
            f"（已略去 {'、'.join(dropped_names)}）"
        ),
        dropped_columns=dropped_names,
    )


def _metric_candidates(label: str) -> tuple[str, ...]:
    """`slot.metric`（中文标签）→ 候选物理列名。"""
    text = str(label or "").strip()
    if not text:
        return ()
    for keyword, candidates in METRIC_ALIASES.items():
        if keyword in text:
            return candidates
    return (text,) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) else ()


def _pick_main_metric(metrics: Sequence[ColumnInfo], slot: dict[str, Any]) -> ColumnInfo | None:
    """主指标：`slot.metric` 命中 → 语义拆分组优先 → 第一个指标列。"""
    candidates = _metric_candidates(str(slot.get("metric") or ""))
    for candidate in candidates:
        for metric in metrics:
            if metric.name == candidate or metric.physical == candidate:
                return metric
    semantic = [metric for metric in metrics if metric.partition]
    if semantic:
        return semantic[0]
    return metrics[0] if metrics else None


def _apply_unit_filter(
    metrics: Sequence[ColumnInfo], slot: dict[str, Any]
) -> tuple[list[ColumnInfo], str | None, list[str]]:
    """决策 ⑨：同量纲多系列；跨量纲只画与**主指标**同单位的一组。

    返回 `(保留的指标列, Y 轴单位, 被排除的列名)`。量纲未知（schema 查不到）的列
    归入"无单位组"，与主指标同组（否则上传表的计算结果会被莫名其妙排除）。
    """
    if not metrics:
        return [], None, []
    groups: dict[str | None, list[ColumnInfo]] = {}
    for metric in metrics:
        groups.setdefault(metric.unit, []).append(metric)
    if len(groups) <= 1:
        return list(metrics), metrics[0].unit, []

    main = _pick_main_metric(metrics, slot)
    unit = None
    if main is not None:
        unit = main.unit
    # 主指标量纲未知时：取列数最多的一组（通常是真正的指标组）
    if unit not in groups:
        unit = max(groups, key=lambda key: len(groups[key]))
    kept = groups[unit]
    excluded = [metric.name for metric in metrics if metric not in kept]
    return list(kept), unit, excluded


def _limit_series(
    metrics: Sequence[ColumnInfo], excluded: list[str]
) -> tuple[list[ColumnInfo], list[str]]:
    """系列数上限（`MAX_SERIES`）：超出部分不绘制并在响应里回显。"""
    if len(metrics) <= MAX_SERIES:
        return list(metrics), excluded
    extra = [metric.name for metric in metrics[MAX_SERIES:]]
    logger.info("图表系列数超过 %d，已截断：%s", MAX_SERIES, extra)
    return list(metrics[:MAX_SERIES]), [*excluded, *extra]


# ══════════════════════════════════════════════════════════════════════════
#  ③ 类型选择（决策 ③：chart_hint 优先 + 形状兜底）
# ══════════════════════════════════════════════════════════════════════════


def _pie_pairs_semantic(shape: _Shape) -> tuple[list[tuple[str, float, str | None]], list[str]]:
    """语义拆分组（用途/时段）→ 饼图扇区：`([(标签, 值, 语义色键)], 进图的指标列名)`。

    只取**一套构成**：`SELECT kitchen_kwh, …, peak_kwh, …` 同时给两套时，
    饼图只用先命中的那一套（另一套与"合计项"都应排除，否则扇区之和 ≠ 总量）。
    """
    for partition in ("purpose", "period"):
        group = _partition_group(shape.metrics, partition)
        if len(group) < 2:
            continue
        pairs: list[tuple[str, float, str | None]] = []
        for metric in group:
            numbers = [_to_number(value) for value in metric.values]
            valid = [number for number in numbers if number is not None]
            if not valid:
                continue
            total = valid[0] if shape.n_points == 1 else sum(valid)
            pairs.append((metric.label, total, metric.series))
        if len(pairs) >= 2:
            return pairs, [metric.name for metric in group]
    return [], []


def _pie_pairs_wide(shape: _Shape) -> list[tuple[str, float, str | None]]:
    """宽表转长表：每个指标列一个扇区（多行时取各列合计）。"""
    pairs: list[tuple[str, float, str | None]] = []
    for metric in shape.metrics:
        numbers = [_to_number(value) for value in metric.values]
        valid = [number for number in numbers if number is not None]
        if not valid:
            continue
        total = valid[0] if shape.n_points == 1 else sum(valid)
        pairs.append((metric.label, total, metric.series))
    return pairs


def _pie_pairs_category(
    shape: _Shape, slot: dict[str, Any]
) -> tuple[list[tuple[str, float, str | None]], list[str]]:
    """分类轴 + 主指标 → 饼图扇区（如 `GROUP BY period` / `GROUP BY weekday`）。"""
    axis = shape.category_axis
    metric = _pick_main_metric(shape.metrics, slot)
    if axis is None or metric is None or metric.partition:
        return [], []
    numbers = [_to_number(value) for value in metric.values]
    pairs = [
        (str(label), number, metric.series)
        for label, number in zip(axis.values, numbers)
        if number is not None
    ]
    # 标签与取值都可能重复（未聚合的结果）→ 按标签合并求和
    merged: dict[str, float] = {}
    for label, value, _ in pairs:
        merged[label] = merged.get(label, 0.0) + value
    series_key = metric.series
    return [(label, value, series_key) for label, value in merged.items()], [metric.name]


@dataclass
class _PieSource:
    """饼图数据源：扇区 + **进图的指标列名**（用于回显"哪些列被排除"）。"""

    pairs: list[tuple[str, float, str | None]]
    used: list[str]


def _pie_source(shape: _Shape, slot: dict[str, Any]) -> _PieSource | None:
    """饼图数据源（优先级）：语义拆分组 → 分类轴 + 主指标 → 宽表转长表。"""
    pairs, used = _pie_pairs_semantic(shape)
    if not pairs:
        pairs, used = _pie_pairs_category(shape, slot)
    if not pairs and len(shape.metrics) >= 2:
        pairs = _pie_pairs_wide(shape)
        used = [metric.name for metric in shape.metrics]
    if not pairs or len(pairs) < 2:
        return None
    # 饼图不接受负值（占比语义下负扇区无意义）→ 交给兜底类型
    if any(value < 0 for _, value, _ in pairs):
        return None
    if sum(value for _, value, _ in pairs) <= 0:
        return None
    return _PieSource(pairs=pairs, used=used)


def _scatter_source(shape: _Shape, slot: dict[str, Any]) -> tuple[ColumnInfo, ColumnInfo] | None:
    """散点需要两个数值列。

    **刻意使用未做量纲筛选的指标列**（`metrics_raw`）：散点表达的本就是"两个不同物理量之间的关系"
    （如 `gap_kwh` vs `gap_kw`），若沿用决策 ⑨ 的跨量纲筛选会把另一个轴筛掉，散点永远画不出来。
    """
    pool = shape.metrics_raw or shape.metrics
    if len(pool) < 2:
        return None
    main = _pick_main_metric(pool, slot) or pool[0]
    others = [metric for metric in pool if metric is not main]
    if not others:
        return None
    return main, others[0]


def _axis_x_column(shape: _Shape) -> ColumnInfo | None:
    """折线/柱状/面积的 X：分类轴优先（排行/对比），否则时间轴（趋势）。"""
    return shape.category_axis or shape.time_axis


def _supported_types(shape: _Shape, slot: dict[str, Any]) -> list[str]:
    """当前结果**画得出来**的图表类型（用于显式指定类型的校验与错误提示）。"""
    supported = ["table"]
    x_column = _axis_x_column(shape)
    has_axis = x_column is not None
    x_points = len(x_column.distinct()) if has_axis else 0
    # 折线/面积必须有轴且至少两个点（单点/无轴的"趋势"没有意义）
    if has_axis and x_points >= 2 and shape.metrics:
        supported += ["line", "area", "bar"]
    elif shape.metrics and (has_axis or len(shape.metrics) >= 2):
        # 无轴时柱状仍可用：把指标列本身当分类（多指标横向对比）
        supported.append("bar")
    if _pie_source(shape, slot):
        supported.append("pie")
    if _scatter_source(shape, slot):
        supported.append("scatter")
    return sorted(set(supported), key=CHART_TYPES.index)


def _fallback_chart_type(shape: _Shape, slot: dict[str, Any], supported: Sequence[str]) -> str:
    """形状兜底（决策 ③ 的后半句）：保证**任何结果都能画出一张图**。

    顺序即优先级，三条经验规则（都由真实用例倒逼出来）：
    1. 单行多指标**有分类轴**（`GROUP BY period` 只有一行）→ 构成型对比，饼/柱；
    2. `intent=占比` → 饼；
    3. **无轴的单行多指标**（如 `SELECT SUM(peak_kwh), SUM(flat_kwh), SUM(valley_kwh)`）→
       指标列本身当分类做横向对比（柱状）。**不能落到散点**：散点需要两个数值列，
       "峰/平/谷各是多少"用散点表达会画成一个孤零零的点（前端联调时实测踩到）。
    """
    intent = str(slot.get("intent") or "")
    if shape.category_axis is not None and shape.n_points == 1 and len(shape.metrics) >= 2:
        return "pie" if "pie" in supported else "bar"
    if intent == "占比" and "pie" in supported:
        return "pie"
    if shape.category_axis is not None and "bar" in supported:
        return "bar"
    if shape.time_axis is not None and "line" in supported:
        return "line"
    if (
        shape.time_axis is None
        and shape.category_axis is None
        and len(shape.metrics) >= 2
        and "bar" in supported
    ):
        return "bar"
    if "scatter" in supported:
        return "scatter"
    if "bar" in supported:
        return "bar"
    return "table"


def _choose_chart_type(
    shape: _Shape,
    slot: dict[str, Any],
    requested: str | None,
    *,
    strict_type: bool,
) -> str:
    """定类型：显式指定（`rerender`）→ 指令；`chart_hint` → 建议；兜底始终兜底。

    `strict_type=True` 时显式指定的类型画不出来会**抛 400**（`E_CHART_UNSUPPORTED`），
    因为那是用户点击的结果、可立即纠正；`chart_hint` 是模型建议，不合法就静默兜底。
    """
    supported = _supported_types(shape, slot)
    if requested:
        if requested in supported:
            return requested
        if strict_type:
            raise chart_unsupported(
                TYPE_LABELS.get(requested, requested),
                [TYPE_LABELS[code] for code in supported],
            )
        logger.info("图表类型 %s 与数据形状不符，改为兜底类型", requested)
    return _fallback_chart_type(shape, slot, supported)


def _resolve_requested(chart_type: str | None, slot: dict[str, Any]) -> str | None:
    """请求类型 → 内部类型：显式传参优先，否则解析 `slot.chart_hint`（中文）。"""
    if chart_type:
        return chart_type
    hint = str(slot.get("chart_hint") or "").strip()
    return HINT_TO_TYPE.get(hint)


# ══════════════════════════════════════════════════════════════════════════
#  ④ 数据整形（TopN 合并 / 抽稀 / 缺采断开）
# ══════════════════════════════════════════════════════════════════════════

#: 分类类图里被合并余项的统一标签（决策 ⑥）
OTHER_LABEL = "其他"


@dataclass
class _Series:
    """一条待绘制序列（与 X 轴等长）。"""

    label: str
    y: list[float | None]
    #: 每个点对应的**结果行号**（前端「点数据点 → 表格定位」联动用，UIUX 7.1）；
    #: 缺采断点处为 None（该点不是真实数据）
    customdata: list[int | None]
    color: str | None = None


def _semantic_color(series_key: str | None) -> str | None:
    """拆分身份 → 语义色（时段用语义色板，用途用用途色板）。"""
    if not series_key:
        return None
    return PERIOD_COLORS.get(series_key) or PURPOSE_COLORS.get(series_key)


def _label_color(label: str, series_key: str | None = None) -> str | None:
    """按标签文本/列名匹配语义色（饼图扇区与柱状分类柱用）。

    匹配优先级：指标列自带的拆分身份 → 取值（`peak` / `厨房`…）→ 中文展示名反查列名。
    """
    color = _semantic_color(series_key)
    if color:
        return color
    key = str(label).strip().lower()
    if key in PERIOD_VALUES:
        return PERIOD_COLORS[PERIOD_VALUES[key]]
    if key in PURPOSE_VALUES:
        return PURPOSE_COLORS[PURPOSE_VALUES[key]]
    for name, chinese in COLUMN_LABELS.items():
        if chinese == str(label).strip():
            _, mapped = _semantic_of_name(name)
            color = _semantic_color(mapped)
            if color:
                return color
    return None


def _collapse_duplicates(
    labels: list[str], series: list[_Series], metrics: Sequence[ColumnInfo]
) -> tuple[list[str], list[_Series]]:
    """未聚合的结果（同一分类值多行）→ 按标签合并。

    合并方式按量纲：电量/分钟求和，功率/电压/电流取均值（与 `UNIT_AGG` 口径一致），
    避免把"平均功率"当电量加起来。
    """
    if len(set(labels)) == len(labels):
        return labels, series
    order: list[str] = []
    buckets: dict[str, list[int]] = {}
    for index, label in enumerate(labels):
        if label not in buckets:
            buckets[label] = []
            order.append(label)
        buckets[label].append(index)

    collapsed: list[_Series] = []
    for metric, item in zip(metrics, series):
        agg = UNIT_AGG.get(metric.unit or "", "sum")
        values: list[float | None] = []
        custom: list[int] = []
        for label in order:
            bucket = buckets[label]
            numbers = [item.y[i] for i in bucket if item.y[i] is not None]
            if not numbers:
                values.append(None)
            elif agg == "mean":
                values.append(sum(numbers) / len(numbers))
            else:
                values.append(sum(numbers))
            custom.append(bucket[0])
        collapsed.append(_Series(label=item.label, y=values, customdata=custom, color=item.color))
    return order, collapsed


def _sort_category(
    labels: list[str], series: list[_Series], sort: str | None
) -> tuple[list[str], list[_Series]]:
    """分类轴排序：`value_desc` / `value_asc` 按第一条序列的值；其他值保持结果顺序。"""
    if sort not in ("value_desc", "value_asc") or not series:
        return labels, series
    primary = series[0].y

    def key(index: int) -> tuple[bool, float]:
        value = primary[index]
        if value is None:
            return True, 0.0
        return False, -value if sort == "value_desc" else value

    order = sorted(range(len(labels)), key=key)
    return (
        [labels[i] for i in order],
        [
            _Series(label=item.label, y=[item.y[i] for i in order],
                    customdata=[item.customdata[i] for i in order], color=item.color)
            for item in series
        ],
    )


def _merge_others(
    labels: list[str], series: list[_Series], top_n: int
) -> tuple[list[str], list[_Series], int]:
    """决策 ⑥：分类数 > TopN 时取前 N 项，余项**合并为「其他」**（不是丢弃）。

    必须合并而非截断的原因：饼图各扇区之和要等于总计（否则占比失真，属口径错误）。
    """
    if top_n < MIN_TOP_N or len(labels) <= top_n:
        return labels, series, 0
    kept = list(range(top_n))
    rest = list(range(top_n, len(labels)))
    merged_labels = [labels[i] for i in kept] + [OTHER_LABEL]
    merged_series: list[_Series] = []
    for item in series:
        values: list[float | None] = [item.y[i] for i in kept]
        numbers = [item.y[i] for i in rest if item.y[i] is not None]
        values.append(sum(numbers) if numbers else None)
        custom = [item.customdata[i] for i in kept]
        custom.append(item.customdata[rest[0]] if rest else -1)
        merged_series.append(
            _Series(label=item.label, y=values, customdata=custom, color=item.color)
        )
    return merged_labels, merged_series, len(rest)


def _downsample_indices(total: int, limit: int) -> list[int]:
    """等间隔抽稀（保留首末点），决策 ⑥。"""
    if total <= limit or limit < 3:
        return list(range(total))
    step = (total - 1) / (limit - 1)
    return sorted({int(round(k * step)) for k in range(limit)} | {0, total - 1})


def _gap_boundaries(
    times: list[datetime | None], granularity: str
) -> list[tuple[datetime, datetime]]:
    """决策 ⑦：相邻两点间隔 > 粒度基准 × 1.5 即为缺口（依据**数据本身的时间轴**）。"""
    step = _GAP_STEP_SECONDS.get(granularity, 0.0)
    if step <= 0:
        return []
    boundaries: list[tuple[datetime, datetime]] = []
    for left, right in zip(times, times[1:]):
        if left is None or right is None:
            continue
        if (right - left).total_seconds() > step * GAP_FACTOR:
            boundaries.append((left, right))
    return boundaries


def _insert_breaks(
    labels: list[str],
    times: list[datetime | None],
    series: list[_Series],
    boundaries: Sequence[tuple[datetime, datetime]],
    granularity: str,
) -> tuple[list[str], int]:
    """在缺口处插入一个 `y=None` 的断点：Plotly 折线遇 `null` 自动断开、**不插值**。

    必须在抽稀**之后**做：先在完整序列上算出缺口（否则抽稀后的间隔会被误判），
    再按"保留点是否跨越该缺口"决定插点位置。
    """
    if not boundaries or not series:
        return labels, 0
    pending = list(boundaries)
    cursor = 0
    inserted = 0
    new_labels: list[str] = []
    new_values: list[list[float | None]] = [[] for _ in series]
    new_custom: list[list[int | None]] = [[] for _ in series]
    for index, label in enumerate(labels):
        new_labels.append(label)
        for position, item in enumerate(series):
            new_values[position].append(item.y[index])
            new_custom[position].append(item.customdata[index])
        # 已经完全越过的缺口不再考虑
        while cursor < len(pending) and times[index] is not None and times[index] > pending[cursor][1]:
            cursor += 1
        if cursor >= len(pending) or index + 1 >= len(labels):
            continue
        left_time, right_time = pending[cursor]
        if times[index] is None or times[index + 1] is None:
            continue
        if times[index] <= left_time and times[index + 1] >= right_time:
            middle = left_time + (right_time - left_time) / 2
            new_labels.append(_format_time(middle, granularity))
            for position in range(len(series)):
                # `y` 与 `customdata` 必须同步插入，否则两者长度不一致（Plotly 会错位）
                new_values[position].append(None)
                new_custom[position].append(None)
            inserted += 1
            cursor += 1
    for position, item in enumerate(series):
        item.y = new_values[position]
        item.customdata = new_custom[position]
    return new_labels, inserted


def _gap_connector(
    labels: list[str], values: list[float | None]
) -> tuple[list[str | None], list[float | None]] | None:
    """缺口处补一段**灰色虚线**（UIUX 7.2「缺采区间用灰色虚线断开」）。"""
    xs: list[str | None] = []
    ys: list[float | None] = []
    for index, value in enumerate(values):
        if value is not None or index == 0 or index + 1 >= len(values):
            continue
        if values[index - 1] is None or values[index + 1] is None:
            continue
        if xs:
            xs.append(None)
            ys.append(None)
        xs += [labels[index - 1], labels[index + 1]]
        ys += [values[index - 1], values[index + 1]]
    return (xs, ys) if xs else None


# ══════════════════════════════════════════════════════════════════════════
#  ⑤ Figure 构建
# ══════════════════════════════════════════════════════════════════════════


def _base_layout(*, x_title: str | None = None, y_title: str | None = None) -> dict[str, Any]:
    """布局**只写文案与交互**：不写 `paper_bgcolor`/`plot_bgcolor`/`font.color`/网格线色。

    对应决策 ⑪（主题边界）：底色与字体色由前端主题层在 `Plotly.react` 时覆盖，
    否则 UIUX 7.2「图表随主题切换、导出图片固定亮色底」需要后端重发 figure 才能实现。
    """
    layout: dict[str, Any] = {
        "margin": {"l": 56, "r": 24, "t": 36, "b": 48},
        "dragmode": "pan",       # UIUX 7.2：框选缩放默认关闭，避免误操作
        "hovermode": "x unified",
    }
    axes: dict[str, Any] = {}
    if x_title:
        axes["xaxis"] = {"title": {"text": x_title}, "automargin": True}
    if y_title:
        axes["yaxis"] = {"title": {"text": y_title}, "automargin": True}
    layout.update(axes)
    return layout


def _hover(unit: str | None) -> str:
    suffix = f" {unit}" if unit else ""
    return f"%{{x}}<br>%{{y}}{suffix}<extra>%{{fullData.name}}</extra>"


def _peak_trace(
    labels: list[str], values: list[float | None], *, unit: str | None
) -> tuple[go.Scatter, dict[str, Any]] | None:
    """折线峰值点标注（UIUX 7.2：峰值点用峰段橙红加标注）。"""
    points = [
        (label, value) for label, value in zip(labels, values)
        if value is not None and value > 0
    ]
    if len(points) < 3:
        return None
    peak_label, peak_value = max(points, key=lambda item: item[1])
    suffix = f" {unit}" if unit else ""
    trace = go.Scatter(
        x=[peak_label], y=[peak_value], mode="markers", name="峰值点",
        marker={"color": COLOR_PEAK, "size": 10, "symbol": "circle",
                "line": {"color": "#FFFFFF", "width": 1}},
        hovertemplate=f"峰值<br>%{{x}}<br>%{{y}}{suffix}<extra></extra>",
    )
    annotation = {
        "x": peak_label, "y": peak_value,
        "text": f"峰值 {_fmt_number(peak_value)}{suffix}",
        "showarrow": True, "arrowhead": 2, "ay": -30, "ax": 0,
        # 这里的颜色是**语义色**（峰段橙红），不是主题字体色
        "font": {"color": COLOR_PEAK},
    }
    return trace, annotation


def _build_axis_figure(
    chart_kind: str,
    labels: list[str],
    series: list[_Series],
    *,
    x_title: str | None,
    unit: str | None,
    gap_segments: tuple[list[str | None], list[float | None]] | None = None,
) -> go.Figure:
    """折线 / 面积 / 柱状：X 轴 + 一条或多条序列。"""
    y_title = f"单位：{unit}" if unit else None
    layout = _base_layout(x_title=x_title, y_title=y_title)
    traces: list[Any] = []
    multi = len(series) > 1
    for position, item in enumerate(series):
        if item.color:
            color = item.color
        elif multi:
            color = DEFAULT_SERIES_COLORS[position % len(DEFAULT_SERIES_COLORS)]
        else:
            color = COLOR_FLAT  # 单系列：UIUX 7.2「主趋势线用平段蓝」
        customdata = [[index] for index in item.customdata]
        if chart_kind == "bar":
            # 单系列且分类值本身带语义（峰/平/谷、厨房/洗衣房…）→ 逐柱上语义色
            if not multi:
                per_bar = [_label_color(label) for label in labels]
                marker: dict[str, Any] = (
                    {"color": per_bar} if all(per_bar) else {"color": color}
                )
            else:
                marker = {"color": color}
            traces.append(
                go.Bar(
                    x=labels, y=item.y, name=item.label, marker=marker,
                    customdata=customdata, hovertemplate=_hover(unit),
                )
            )
        else:
            traces.append(
                go.Scatter(
                    x=labels, y=item.y, name=item.label,
                    mode="lines+markers" if len(labels) <= 60 else "lines",
                    line={"color": color, "width": 2},
                    marker={"size": 5, "color": color},
                    fill="tozeroy" if chart_kind == "area" else None,
                    connectgaps=False,   # 决策 ⑦：不插值，缺口处断开
                    customdata=customdata, hovertemplate=_hover(unit),
                )
            )
    if chart_kind == "bar" and multi:
        layout["barmode"] = "group"

    layout["showlegend"] = multi
    if chart_kind in ("line", "area") and series:
        peak = _peak_trace(labels, series[0].y, unit=unit)
        if peak is not None:
            layout["annotations"] = [peak[1]]
            traces.append(peak[0])
        if gap_segments is not None:
            traces.append(
                go.Scatter(
                    x=gap_segments[0], y=gap_segments[1], mode="lines", name="缺采区间",
                    line={"color": COLOR_MISSING, "width": 1, "dash": "dot"},
                    hoverinfo="skip", showlegend=False,
                )
            )
    figure = go.Figure(data=traces, layout=layout)
    return figure


#: 饼图专用**蓝色系**（用户要求，2026-09-21；此前用语义色板）。
#: 设计：同色相深浅渐阶 + 青蓝点缀，相邻扇区刻意拉开明度差，保证 3–12 个扇区都可辨。
#: 不影响柱状/折线——它们仍用语义色板（峰/平/谷、用途四类），红 R3 的"两套色板不混用"依然成立；
#: 饼图扇区自带「标签 + 百分比」文案，因此不依赖右栏语义色图例。
PIE_BLUE_SCALE: tuple[str, ...] = (
    "#1D4ED8", "#60A5FA", "#0B7285", "#93C5FD", "#0284C7", "#7DD3FC",
    "#1E40AF", "#38BDF8", "#0EA5E9", "#A5B4FC", "#075985", "#BAE6FD",
)


def _build_pie_figure(
    pairs: Sequence[tuple[str, float, str | None]], *, unit: str | None
) -> go.Figure:
    """饼图：扇区颜色按**蓝色系渐阶**注入（用户要求；按扇区顺序循环取色）。"""
    labels = [label for label, _, _ in pairs]
    values = [value for _, value, _ in pairs]
    colors = [PIE_BLUE_SCALE[index % len(PIE_BLUE_SCALE)] for index, _ in enumerate(pairs)]
    suffix = f" {unit}" if unit else ""
    figure = go.Figure(
        data=[
            go.Pie(
                labels=labels, values=values, sort=False,
                marker={"colors": colors},
                textinfo="label+percent", textposition="inside",
                hovertemplate=f"%{{label}}<br>%{{value}}{suffix}（%{{percent}}）<extra></extra>",
            )
        ],
        layout={
            "margin": {"l": 24, "r": 24, "t": 36, "b": 24},
            "showlegend": True,
            # 饼图不设 x/y 轴标题：占比语义由扇区标签表达
        },
    )
    return figure


def _build_scatter_figure(
    x_metric: ColumnInfo, y_metric: ColumnInfo, rows: Sequence[dict[str, Any]]
) -> go.Figure:
    """散点：两个数值列的关系（PRD F3.1 的"相关性 → 散点"）。"""
    x_values = [_to_number(row.get(x_metric.name)) for row in rows]
    y_values = [_to_number(row.get(y_metric.name)) for row in rows]
    customdata = [[index] for index in range(len(rows))]
    unit_note = " / ".join(
        [f"{x_metric.label}（{x_metric.unit}）"] if x_metric.unit else [x_metric.label]
    ) + "".join([f"、{y_metric.label}（{y_metric.unit}）"] if y_metric.unit else [f"、{y_metric.label}"])
    figure = go.Figure(
        data=[
            go.Scatter(
                x=x_values, y=y_values, mode="markers", name=y_metric.label,
                marker={"size": 8, "opacity": 0.75},
                customdata=customdata,
                hovertemplate=(
                    f"%{{x}} / %{{y}}<br>{unit_note}<extra></extra>"
                ),
            )
        ],
        layout={
            **_base_layout(x_title=x_metric.label, y_title=y_metric.label),
            "showlegend": False,
            "hovermode": "closest",
        },
    )
    return figure


def _figure_meta(outcome: ChartOutcome) -> dict[str, Any]:
    """写进 `layout.meta` 的口径标记。

    为什么放进 figure 内部而不是只放在接口响应里：`analyses.chart_json` 按数据库设计只存
    "Plotly figure JSON"（保持纯 figure 契约），但详情页重新打开时同样需要知道
    "其余 8 项已合并""已按 R3 只展示一套"——Plotly 的 `layout.meta` 正好是任意自定义数据，
    于是图**自带口径说明**，不必为它加数据库列。
    """
    return {
        "chart_type": outcome.chart_type,
        "unit": outcome.unit,
        "x_axis": outcome.x_axis,
        "series": outcome.series,
        "top_n": outcome.top_n,
        "merged_count": outcome.merged_count,
        "downsampled_from": outcome.downsampled_from,
        "gap_count": outcome.gap_count,
        "r3_adjusted": outcome.r3_adjusted,
        "r3_note": outcome.r3_note,
        "dropped_columns": outcome.dropped_columns,
        "excluded_columns": outcome.excluded_columns,
    }


def _figure_to_dict(
    figure: go.Figure, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    """figure → 纯 JSON dict，剔除内置 `template`，并把口径标记写进 `layout.meta`。

    - 剔除 `template`：决策 ⑪（后端不写主题）且能让 `chart_json` 小几十 KB；
    - 空值（`None` / 空列表 / 0）不进 meta，避免每张图都挂一堆无意义字段。
    """
    payload = json.loads(figure.to_json())
    layout = payload.get("layout")
    if not isinstance(layout, dict):
        layout = {}
        payload["layout"] = layout
    layout.pop("template", None)
    if meta:
        compact = {
            key: value for key, value in meta.items()
            if value is not None and value != [] and value != 0 and value is not False
        }
        if compact:
            layout["meta"] = compact
    return payload


def _figure_bytes(figure_payload: dict[str, Any]) -> int:
    return len(json.dumps(figure_payload, ensure_ascii=False).encode("utf-8"))


# ══════════════════════════════════════════════════════════════════════════
#  ⑥ 主入口：build_chart
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class _Frame:
    """轴类图的整形结果（含 TopN / 抽稀 / 缺口等回显信息）。"""

    labels: list[str]
    series: list[_Series]
    x_title: str | None = None
    merged_count: int = 0
    downsampled_from: int | None = None
    gap_count: int = 0
    gap_segments: tuple[list[str | None], list[float | None]] | None = None


def _axis_frame(
    shape: _Shape,
    *,
    chart_kind: str,
    top_n: int,
    sort: str | None,
    point_limit: int = DOWNSAMPLE_LIMIT,
) -> _Frame:
    """把结果表整形为「X 轴 + 若干序列」：排序 → 合并重复 → TopN → 抽稀 → 缺口断开。"""
    x_column = _axis_x_column(shape)
    metrics = shape.metrics

    # ① 没有可用轴：把指标列本身当分类（各列合计），用于"多指标横向对比"
    if x_column is None:
        labels = [metric.label for metric in metrics]
        totals: list[float | None] = []
        custom: list[int] = []
        for metric in metrics:
            valid = [(i, _to_number(v)) for i, v in enumerate(metric.values)]
            valid = [(i, number) for i, number in valid if number is not None]
            if not valid:
                totals.append(None)
                custom.append(-1)
                continue
            totals.append(
                valid[0][1] if shape.n_points == 1 else sum(number for _, number in valid)
            )
            custom.append(valid[0][0])
        return _Frame(
            labels=labels,
            series=[_Series(label=shape.unit or "数值", y=totals, customdata=custom)],
            x_title=None,
        )

    is_time = x_column.kind == "time"
    granularity = TIME_COLUMNS.get(x_column.name, "day") if is_time else None
    times: list[datetime | None] = [None] * shape.n_points
    if is_time:
        for index in range(shape.n_points):
            times[index] = _parse_time(x_column.values[index], granularity or "day")
        order = sorted(
            [i for i in range(shape.n_points) if times[i] is not None],
            key=lambda i: times[i],  # type: ignore[arg-type,return-value]
        ) + [i for i in range(shape.n_points) if times[i] is None]
    else:
        order = list(range(shape.n_points))

    labels = [str(x_column.values[i]) for i in order]
    series = [
        _Series(
            label=metric.label,
            y=[_to_number(metric.values[i]) for i in order],
            customdata=order,
            color=_semantic_color(metric.series),
        )
        for metric in metrics
    ]

    # ② 未聚合的结果（同一 X 出现多行）→ 按标签合并，避免折线来回折返
    if len(set(labels)) != len(labels):
        labels, series = _collapse_duplicates(labels, series, metrics)
        if is_time:
            times = [_parse_time(label, granularity or "day") for label in labels]
        else:
            labels, series = _sort_category(labels, series, sort)
    elif not is_time:
        labels, series = _sort_category(labels, series, sort)

    # ③ 分类类图（柱状）TopN 合并「其他」
    #    **时间轴不做合并**：把"2020-01-03"归进「其他」没有意义（顺序本身带信息），
    #    时间序列走下面的抽稀（决策 ⑥ 的"分类数"指真正的分类维度）
    merged_count = 0
    if chart_kind == "bar" and not is_time:
        labels, series, merged_count = _merge_others(labels, series, top_n)

    # ④ 抽稀与缺口：**先在完整序列上算缺口**，再抽稀，最后插入断点
    #    （顺序不能反：抽稀后的相邻间隔被拉大，会被误判成"处处是缺口"）
    downsampled_from: int | None = None
    gap_count = 0
    gap_segments: tuple[list[str | None], list[float | None]] | None = None
    boundaries = (
        _gap_boundaries(times, granularity)
        if (chart_kind in ("line", "area") and is_time and granularity)
        else []
    )
    if chart_kind in ("line", "area") or (chart_kind == "bar" and is_time):
        if len(labels) > point_limit:
            downsampled_from = len(labels)
            keep = _downsample_indices(len(labels), point_limit)
            labels = [labels[i] for i in keep]
            times = [times[i] for i in keep]
            series = [
                _Series(label=item.label, y=[item.y[i] for i in keep],
                        customdata=[item.customdata[i] for i in keep], color=item.color)
                for item in series
            ]
    if boundaries:
        labels, gap_count = _insert_breaks(labels, times, series, boundaries, granularity)
        if series:
            gap_segments = _gap_connector(labels, series[0].y)

    return _Frame(
        labels=labels, series=series, x_title=x_column.label,
        merged_count=merged_count, downsampled_from=downsampled_from,
        gap_count=gap_count, gap_segments=gap_segments,
    )


def _sort_pie_pairs(
    pairs: Sequence[tuple[str, float, str | None]], sort: str | None
) -> list[tuple[str, float, str | None]]:
    """饼图扇区顺序：默认按值降序（占比图的阅读习惯），`sort="natural"` 保持原顺序。"""
    if sort == "natural":
        return list(pairs)
    if sort == "value_asc":
        return sorted(pairs, key=lambda item: item[1])
    return sorted(pairs, key=lambda item: -item[1])


def _merge_pie_pairs(
    pairs: list[tuple[str, float, str | None]], top_n: int
) -> tuple[list[tuple[str, float, str | None]], int]:
    if len(pairs) <= top_n:
        return pairs, 0
    head, rest = pairs[:top_n], pairs[top_n:]
    total = sum(value for _, value, _ in rest)
    return [*head, (OTHER_LABEL, total, None)], len(rest)


def build_chart(
    columns: Iterable[Any],
    rows: Sequence[dict[str, Any]],
    *,
    schema_columns: Iterable[dict[str, Any]] | None = None,
    slot: dict[str, Any] | None = None,
    chart_type: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    sort: str | None = None,
    point_limit: int = DOWNSAMPLE_LIMIT,
    strict_type: bool = False,
) -> ChartOutcome:
    """把结果表转成 Plotly figure JSON（**不调 LLM、纯计算**）。

    `chart_type=None` 时按 `slot.chart_hint` + 数据形状自动选型（决策 ③）；
    `strict_type=True`（`rerender` 的显式点击）时类型不符会抛 `E_CHART_UNSUPPORTED`。
    """
    try:
        return _build_chart(
            columns, rows, schema_columns=schema_columns, slot=dict(slot or {}),
            chart_type=chart_type, top_n=top_n, sort=sort,
            point_limit=point_limit, strict_type=strict_type,
        )
    except BizError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 决策 ⑧：图表异常绝不阻断主流程
        logger.exception("图表生成异常，已降级为表格")
        return _degrade(f"图表生成异常（{type(exc).__name__}），已降级为表格")


def _build_chart(
    columns: Iterable[Any],
    rows: Sequence[dict[str, Any]],
    *,
    schema_columns: Iterable[dict[str, Any]] | None,
    slot: dict[str, Any],
    chart_type: str | None,
    top_n: int,
    sort: str | None,
    point_limit: int,
    strict_type: bool,
) -> ChartOutcome:
    rows = list(rows or [])
    top_n = max(MIN_TOP_N, min(MAX_TOP_N, int(top_n or DEFAULT_TOP_N)))
    if not rows:
        return _degrade("结果为空，没有可绘制的数据")

    infos = _profile_columns(columns, rows, schema_columns)
    if not infos:
        return _degrade("结果没有可用列，无法绘图")
    metrics = [info for info in infos if info.kind == "metric"]
    if not metrics:
        return _degrade("结果不含数值指标列，无法绘图")

    # 红线 R3（图表层）：用途 + 时段两套拆分同现时只留一套
    metrics, r3_outcome = _apply_r3(metrics, slot)
    metrics_all, _ = _limit_series(metrics, [])
    # 决策 ⑨：跨量纲只画主指标那一组
    metrics, unit, excluded = _apply_unit_filter(metrics, slot)
    metrics, excluded = _limit_series(metrics, excluded)

    time_axis = _pick_time_axis(infos)
    shape = _Shape(
        time_axis=time_axis,
        category_axis=_pick_category_axis(infos, time_axis),
        metrics=metrics, unit=unit, rows=rows, metrics_raw=metrics_all,
    )
    outcome = r3_outcome or ChartOutcome(figure=None, chart_type="table")
    outcome.excluded_columns = excluded
    outcome.unit = unit

    chosen = _choose_chart_type(
        shape, slot, _resolve_requested(chart_type, slot), strict_type=strict_type
    )
    if chosen == "table":
        outcome.chart_type = "table"
        outcome.fallback = "table"
        outcome.reason = outcome.reason or "该结果不适合可视化，已用表格呈现"
        return outcome

    if chosen == "pie":
        source = _pie_source(shape, slot)
        assert source is not None  # `_supported_types` 已校验
        pairs, merged = _merge_pie_pairs(_sort_pie_pairs(source.pairs, sort), top_n)
        figure = _build_pie_figure(pairs, unit=unit)
        outcome.top_n = top_n if merged else None
        outcome.merged_count = merged
        outcome.series = [label for label, _, _ in pairs]
        outcome.x_axis = None
        # 没进图的指标列如实回显（合计项、"另一套拆分"），避免"为什么饼里没有总电量"的疑问
        outcome.excluded_columns = [
            *excluded,
            *[m.name for m in metrics if m.name not in source.used and m.name not in excluded],
        ]
    elif chosen == "scatter":
        source = _scatter_source(shape, slot)
        assert source is not None  # `_supported_types` 已校验
        figure = _build_scatter_figure(source[0], source[1], rows)
        outcome.series = [source[0].label, source[1].label]
        outcome.x_axis = source[0].label
        # 两个轴各自带单位（已写进轴标题与 tooltip），顶层不再给单一量纲
        outcome.unit = None
        # 散点刻意用**未筛量纲**的指标列，故量纲排除清单对它不成立
        outcome.excluded_columns = [
            name for name in excluded if name not in (source[0].name, source[1].name)
        ]
    else:
        frame = _axis_frame(
            shape, chart_kind=chosen, top_n=top_n, sort=sort, point_limit=point_limit
        )
        if not frame.labels:
            return _degrade("结果没有可绘制的数据点")
        figure = _build_axis_figure(
            chosen, frame.labels, frame.series,
            x_title=frame.x_title, unit=unit, gap_segments=frame.gap_segments,
        )
        outcome.top_n = top_n if frame.merged_count else None
        outcome.merged_count = frame.merged_count
        outcome.downsampled_from = frame.downsampled_from
        outcome.gap_count = frame.gap_count
        outcome.series = [item.label for item in frame.series]
        outcome.x_axis = frame.x_title

    outcome.chart_type = chosen
    payload = _figure_to_dict(figure, _figure_meta(outcome))
    size = _figure_bytes(payload)
    if size > CHART_JSON_LIMIT:
        # 兜底瘦身：再抽稀到 1/4，仍超上限则降级（不让 chart_json 拖大数据库）
        logger.warning("figure 体积 %d 字节超过上限，尝试二次抽稀", size)
        if chosen in ("line", "area", "bar"):
            frame = _axis_frame(
                shape, chart_kind=chosen, top_n=top_n, sort=sort,
                point_limit=max(20, point_limit // 4),
            )
            figure = _build_axis_figure(
                chosen, frame.labels, frame.series,
                x_title=frame.x_title, unit=unit, gap_segments=frame.gap_segments,
            )
            outcome.downsampled_from = outcome.downsampled_from or len(shape.rows)
            payload = _figure_to_dict(figure, _figure_meta(outcome))
            size = _figure_bytes(payload)
        if size > CHART_JSON_LIMIT:
            return _degrade(f"图表数据量过大（{size // 1024}KB），已降级为表格")

    outcome.figure = payload
    outcome.fallback = None
    outcome.reason = None
    return outcome


# ══════════════════════════════════════════════════════════════════════════
#  ⑦ 粒度重聚合（rerender 专用：从快照重算，不改 SQL、不调 LLM）
# ══════════════════════════════════════════════════════════════════════════

#: 源时间列自身粒度 → 可比较的 rank（只有"变粗"才允许重聚合）
SOURCE_GRANULARITY_RANK: dict[str, int] = {"minute": 0, "hour": 1, "day": 2, "month": 3}
#: 目标粒度 → 重聚合后的时间列名（保持 `build_chart` 能识别其粒度）
GRANULARITY_COLUMN: dict[str, str] = {
    "小时": "dt_hour", "日": "date", "月": "year_month", "年": "year", "全期": "all_period",
}
_SOURCE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H", "%Y-%m-%d", "%Y-%m")


def _truncate_time(value: Any, target_format: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for source_format in _SOURCE_FORMATS:
        try:
            return datetime.strptime(text, source_format).strftime(target_format)
        except ValueError:
            continue
    return None


def _reaggregate(
    columns: Iterable[Any],
    rows: Sequence[dict[str, Any]],
    schema_columns: Iterable[dict[str, Any]] | None,
    granularity: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按目标粒度从**结果快照**重聚合（决策 ②：服务端真重聚合）。

    口径要点：
    - 只能**变粗**（`GROUP BY date` 的结果无法拆回小时）→ 否则 `E_GRANULARITY_UNAVAILABLE`；
    - 聚合方式按量纲：`kWh`/`分钟` 求和，`kW`/`V`/`A` 取均值（避免把平均功率当电量相加）；
    - **比率型列一律丢弃**：PRD 红线 R6 要求"分子求和 ÷ 分母求和"重算，而快照里没有分子分母，
      拿均值顶替就是编造。
    """
    from app.core.errors import granularity_unavailable

    names = _column_names(columns)
    time_name = next((name for name in names if name in TIME_COLUMNS), None)
    if time_name is None:
        raise granularity_unavailable("当前结果不含时间列，无法按新粒度重聚合")

    source_rank = SOURCE_GRANULARITY_RANK.get(TIME_COLUMNS[time_name], 2)
    target_rank = GRANULARITY_RANK[granularity]
    if target_rank < source_rank:
        source_label = {"minute": "分钟", "hour": "小时", "day": "日", "month": "月"}[TIME_COLUMNS[time_name]]
        raise granularity_unavailable(
            f"当前结果已聚合到「{source_label}」，无法再拆分为「{granularity}」"
        )

    index = _schema_index(schema_columns)
    target_format = GRANULARITY_FORMATS[granularity] or "%Y-%m-%d"
    keep_columns: list[tuple[str, str]] = []   # (列名, 聚合方式)
    for name in names:
        if name == time_name:
            continue
        entry = index.get(name) or {}
        unit = _clean_unit(entry.get("unit"))
        agg = UNIT_AGG.get(unit or "")
        if agg is None:
            agg = "sum" if str(entry.get("agg") or "") == "sum" else None
        if agg is None or name in FLAG_NAMES or _is_ratio(name, unit):
            continue
        keep_columns.append((name, agg))
    if not keep_columns:
        raise granularity_unavailable("结果中没有可重聚合的数值列（比率型字段需分子分母重算）")

    buckets: dict[str, dict[str, list[float]]] = {}
    order: list[str] = []
    unparsed = 0
    for row in rows:
        if granularity == "全期":
            key = "全期"
        else:
            key = _truncate_time(row.get(time_name), target_format)
        if key is None:
            unparsed += 1
            continue
        if key not in buckets:
            buckets[key] = {}
            order.append(key)
        for name, _ in keep_columns:
            number = _to_number(row.get(name))
            if number is not None:
                buckets[key].setdefault(name, []).append(number)
    if not buckets:
        raise granularity_unavailable("时间列取值无法解析，重聚合失败")

    kept_names = [name for name, _ in keep_columns]
    aggregated_rows: list[dict[str, Any]] = []
    for key in sorted(order):
        payload: dict[str, Any] = {GRANULARITY_COLUMN[granularity]: key}
        for name, agg in keep_columns:
            numbers = buckets[key].get(name) or []
            if not numbers:
                payload[name] = None
            elif agg == "mean":
                payload[name] = round(sum(numbers) / len(numbers), 6)
            else:
                payload[name] = round(sum(numbers), 6)
        aggregated_rows.append(payload)

    aggregated_columns = [{"name": GRANULARITY_COLUMN[granularity], "type": "TEXT"}] + [
        {"name": name, "type": "REAL"} for name in kept_names
    ]
    logger.info(
        "重聚合 %s → %s：%d 行 → %d 组（丢弃 %d 行无法解析的时间值）",
        time_name, granularity, len(rows), len(aggregated_rows), unparsed,
    )
    return aggregated_columns, aggregated_rows


# ══════════════════════════════════════════════════════════════════════════
#  ⑧ 重绘接口（PRD 9.1 `POST /charts/rerender`，不回写时也走这里）
# ══════════════════════════════════════════════════════════════════════════


async def rerender_chart(
    user_id: int,
    record_code: str,
    *,
    chart_type: str | None = None,
    granularity: str | None = None,
    top_n: int | None = None,
    sort: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """切换图表类型 / 粒度并重绘（**不调 LLM**，PRD F3.2 / UIUX 7.1）。

    - `chart_type`：显式指定即"指令"，类型与数据形状不符 → 400 `E_CHART_UNSUPPORTED`；
    - `granularity`：从结果快照**重聚合**（决策 ②），不可重聚合 → 400 `E_GRANULARITY_UNAVAILABLE`；
    - `persist=True`（决策 ⑤）：把新 figure 回写 `analyses.chart_json`，
      使「我的分析记录 → 查看详情」看到的是切过的那张图；
    - 归属隔离：记录不存在或不属于本人一律 **404**（与其余记录接口一致）。
    """
    import json as _json

    from sqlalchemy import select as _select

    from app.core.errors import not_found, validation_error
    from app.db.models import Analysis, UploadedReport

    if chart_type and chart_type not in CHART_TYPES:
        raise validation_error(f"chart_type 只能是 {' / '.join(CHART_TYPES)}")
    if granularity and granularity not in GRANULARITY_FORMATS:
        raise validation_error(f"granularity 只能是 {' / '.join(GRANULARITY_FORMATS)}")
    if top_n is not None and not MIN_TOP_N <= top_n <= MAX_TOP_N:
        raise validation_error(f"top_n 取值范围为 {MIN_TOP_N}–{MAX_TOP_N}")
    if sort and sort not in ("natural", "value_desc", "value_asc"):
        raise validation_error("sort 只能是 natural / value_desc / value_asc")

    async with control_session() as session:
        analysis = await session.scalar(
            _select(Analysis).where(Analysis.record_code == record_code)
        )
        if analysis is None or analysis.user_id != user_id:
            raise not_found(f"分析记录不存在：{record_code}")
        report = (
            await session.get(UploadedReport, analysis.report_id)
            if analysis.report_id
            else None
        )
        schema_columns = None
        if report is not None and report.schema_json:
            try:
                schema_columns = (_json.loads(report.schema_json) or {}).get("columns")
            except _json.JSONDecodeError:  # pragma: no cover —— 脏数据兜底
                schema_columns = None
        columns = _json.loads(analysis.result_columns_json) if analysis.result_columns_json else []
        rows = _json.loads(analysis.result_rows_json) if analysis.result_rows_json else []
        slot = _json.loads(analysis.slot_json) if analysis.slot_json else {}
        snapshot_truncated = bool(analysis.result_truncated)
        if not rows:
            raise validation_error("该记录没有可绘图的结果（未执行或执行失败）")

        reaggregated = False
        if granularity:
            columns, rows = _reaggregate(columns, rows, schema_columns, granularity)
            reaggregated = True

        outcome = build_chart(
            columns, rows,
            schema_columns=schema_columns, slot=slot,
            chart_type=chart_type,
            top_n=top_n if top_n is not None else DEFAULT_TOP_N,
            sort=sort,
            strict_type=bool(chart_type),
        )
        persisted = False
        if persist and outcome.figure is not None:
            analysis.chart_json = _json.dumps(outcome.figure, ensure_ascii=False)
            await session.commit()
            persisted = True

    payload = outcome.to_payload()
    payload.update(
        {
            "record_code": record_code,
            "granularity": granularity,
            "reaggregated": reaggregated,
            "persisted": persisted,
            "snapshot_truncated": snapshot_truncated,
        }
    )
    return payload


# ══════════════════════════════════════════════════════════════════════════
#  ⑨ 服务端图片导出（PRD 9.1 `GET /analysis/records/{record_code}/chart.png`）
# ══════════════════════════════════════════════════════════════════════════

#: 导出图片的**亮色底**主题（UIUX P7 / 7.1：导出图片固定亮色底、2 倍图）
EXPORT_LIGHT_LAYOUT: dict[str, Any] = {
    "paper_bgcolor": "#FFFFFF",
    "plot_bgcolor": "#FFFFFF",
    "font": {"color": "#1F2933"},
}
EXPORT_WIDTH, EXPORT_HEIGHT, EXPORT_SCALE = 1200, 675, 2


def kaleido_available() -> bool:
    """服务端渲染器是否可用（决策 ①：本机不装 kaleido，一旦装上即自动生效）。"""
    try:
        import kaleido  # type: ignore[import-not-found]  # 可选依赖：装了即生效（决策 ①）
    except ImportError:
        return False
    return True


def _render_png(figure: dict[str, Any]) -> bytes:
    """同步渲染 PNG（`kaleido` 会拉起浏览器进程，故由调用方放进线程池）。"""
    import plotly.io as pio

    layout = {**(figure.get("layout") or {}), **EXPORT_LIGHT_LAYOUT}
    layout.pop("meta", None)  # 口径标记是给界面看的说明，不进图片
    return pio.to_image(
        go.Figure({"data": figure.get("data") or [], "layout": layout}),
        format="png",
        width=EXPORT_WIDTH,
        height=EXPORT_HEIGHT,
        scale=EXPORT_SCALE,
    )


async def export_chart_png(user_id: int, record_code: str) -> bytes:
    """把该记录的图表导出为 PNG（PRD 9.1 / F3.4）。

    **当前如实返回 501**（决策 ①）：PRD 12.4 已把 kaleido 服务端导出标为**预留能力**
    （Word/PDF 报告属 P2），本批只装 `plotly`、不引入约 200MB 的 Chrome 依赖。
    接入点是现成的——环境里出现 kaleido 时本函数自动走真实导出，**无需改任何代码**；
    kaleido 装好但缺浏览器（1.x 需要单独下载 Chrome）时会给出可执行的下一步提示。
    """
    import json as _json

    from sqlalchemy import select as _select

    from app.core.errors import not_found, not_implemented, validation_error
    from app.db.models import Analysis

    async with control_session() as session:
        analysis = await session.scalar(
            _select(Analysis).where(Analysis.record_code == record_code)
        )
        if analysis is None or analysis.user_id != user_id:
            raise not_found(f"分析记录不存在：{record_code}")  # 不泄露他人资源是否存在
        raw_chart = analysis.chart_json

    if not raw_chart:
        raise validation_error("该记录没有图表可导出（未执行、执行失败或结果不适合可视化）")
    if not kaleido_available():
        raise not_implemented(
            "服务端图表导出（kaleido）",
            "本机未安装服务端渲染器 kaleido（P2 预留能力）；"
            "请使用前端 Plotly.toImage() 导出 PNG（UIUX 7.1：2 倍图、亮色底）",
        )
    try:
        return await asyncio.to_thread(_render_png, _json.loads(raw_chart))
    except Exception as exc:  # noqa: BLE001 —— 渲染失败同样是"能力不可用"，如实回报
        raise not_implemented(
            "服务端图表导出（kaleido）",
            "kaleido 已安装但渲染失败（它需要本机 Chrome），请先执行 kaleido_get_chrome 下载浏览器",
        ) from exc
