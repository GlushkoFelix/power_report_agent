"""智电析报 · 上传解析与规范化引擎（PRD F1.1–F1.5 的纯函数实现）。

职责（**不做任何数据库读写**，便于单测与放入线程池执行）：
1. 校验前探测：编码（UTF-8 / UTF-8-BOM / GBK）、分隔符（`,` `;` `\\t` `|`）、表头；
2. 列名规范化（本轮决策）：**内置词典翻译 + `col_n` 回退**，物理列名全 ASCII 小写下划线，
   原始列名保留在 `origin_name`；表名由文件名净化生成、冲突追加序号；
3. 时间列识别（`Date`+`Time` 双列 / 单列 datetime / 多种日期格式）→ 统一生成标准时间戳列 `dt`，
   并派生 `year`/`month`/`day`/`hour`/`weekday`/`is_weekend`（**与预置表同名同义**）；
4. 类型推断（TEXT / REAL / INTEGER）、单位与「可否 SUM」推断（比率列一律 `agg=none`）；
5. 分块全量解析（pandas `chunksize`）→ 写出**规范化中间 CSV**，同时累计质量指标（F1.5）。

内存策略：分块读入（默认 20 万行/块），只把「时间戳 int64 数组」累积在内存中用于
去重与缺口统计（200 万行 ≈ 16MB），其余指标（缺失、min/max、样例）逐块增量更新。
"""

from __future__ import annotations

import codecs
import csv
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.errors import (
    encoding_unknown,
    header_invalid,
    no_numeric_column,
    no_time_column,
    parse_error,
)

logger = logging.getLogger("zdxb.upload")

__all__ = [
    "ALLOWED_SUFFIXES",
    "ColumnSpec",
    "ParseResult",
    "build_table_name",
    "detect_delimiter",
    "detect_encoding",
    "normalize_columns",
    "parse_and_normalize",
    "read_first_line",
    "read_header",
]

#: 允许的扩展名（PRD F1.1：`.csv` 与 `.txt` 分号分隔文件）
ALLOWED_SUFFIXES = (".csv", ".txt")

#: 缺失值标记（PRD F1.2：`?`、`None`、空串等统一归一为缺失）
NA_VALUES = ["?", "??", "", "NA", "N/A", "na", "null", "NULL", "None", "none", "nan", "NaN", "-"]
_NA_SET = set(NA_VALUES)

#: 分块行数（解析阶段；灌数分批见 `Settings.ingest_batch_rows`）
PARSE_CHUNK_ROWS = 200_000

#: 列名长度上限（防超长表头）
MAX_NAME_LEN = 60

#: 派生维度列（与预置表 `report_daily` 等同名同义，PRD F1.2）
DERIVED_COLUMNS = ("year", "month", "day", "hour", "weekday", "is_weekend")

#: 标准时间戳列名（与预置 `report_minute.dt` 一致）
TIME_COLUMN = "dt"

#: 时间列候选名（词典匹配用）
TIME_NAME_CANDIDATES = ("date", "time", "datetime", "timestamp", "dt", "日期", "时间", "时间戳", "时刻")

#: ── 列名词典（中文/英文同义词 → 安全英文列名）────────────────────────────
#   匹配前统一做：NFKC 归一 → 小写 → 去空格/下划线/括号内容；
#   未命中词典的列回退为 `col_<序号>`，原始列名仍保留在 `origin_name`。
COLUMN_DICTIONARY: dict[str, str] = {
    # 时间
    "日期": "date", "时间": "time", "时间戳": "timestamp", "时刻": "timestamp",
    "datetime": "datetime", "timestamp": "timestamp",
    # 电量（可求和）
    "总用电量": "total_kwh", "用电量": "consumption_kwh", "总有功电能": "consumption_kwh",
    "有功电能": "consumption_kwh", "电量": "kwh", "电能": "kwh",
    "峰段用电量": "peak_kwh", "尖峰电量": "peak_kwh", "峰电量": "peak_kwh",
    "平段用电量": "flat_kwh", "平电量": "flat_kwh",
    "谷段用电量": "valley_kwh", "谷电量": "valley_kwh",
    "未计量用电量": "unmetered_kwh", "未计量电量": "unmetered_kwh", "未计量": "unmetered_kwh",
    "厨房用电量": "kitchen_kwh", "洗衣房用电量": "laundry_kwh",
    "空调用电量": "water_heating_ac_kwh", "热水器用电量": "water_heating_ac_kwh",
    "分表1": "sm1_kwh", "分表1用电量": "sm1_kwh", "sub_metering_1": "sm1_kwh",
    "分表2": "sm2_kwh", "分表2用电量": "sm2_kwh", "sub_metering_2": "sm2_kwh",
    "分表3": "sm3_kwh", "分表3用电量": "sm3_kwh", "sub_metering_3": "sm3_kwh",
    # 功率 / 电气量
    "功率": "power_kw", "有功功率": "active_power_kw", "无功功率": "reactive_power_kw",
    "最大功率": "max_power_kw", "最小功率": "min_power_kw", "平均功率": "avg_power_kw",
    "电压": "voltage", "电流": "current", "负荷率": "load_rate",
    # 质量 / 覆盖
    "覆盖率": "coverage", "有效分钟数": "active_minutes", "预期分钟数": "expected_minutes",
    # 维度
    "年": "year", "年份": "year", "月": "month", "月份": "month", "年月": "year_month",
    "日": "day", "小时": "hour", "星期": "weekday", "星期几": "weekday", "是否周末": "is_weekend",
    # 通用
    "序号": "seq", "备注": "remark", "说明": "note", "类别": "category", "类型": "type",
}

#: 比率 / 不可求和的列名关键词（PRD F1.2，中文与英文都判）
#   注意：**不能只写单字「率」**——「功率」「频率」不含比率语义，实测会误判有功功率为不可求和
RATIO_KEYWORDS = (
    "ratio", "rate", "factor", "coverage", "占比", "比例", "比率", "系数", "百分比",
    "百分率", "负载率", "负荷率", "合格率", "利用率", "覆盖率",
)

#: 维度 / 文本列（即便解析为数值也不可 SUM，如 year/month/hour/序号）
DIMENSION_NAMES = frozenset({
    "date", "time", "timestamp", "datetime", "dt", "year", "month", "day", "hour",
    "weekday", "is_weekend", "year_month", "seq", "id", "remark", "note", "category", "type",
})

#: 单位推断（按安全列名后缀）
_UNIT_BY_SUFFIX = (("_kwh", "kWh"), ("_kw", "kW"), ("voltage", "V"), ("current", "A"))

#: 单位推断（按原始列名关键词）
_UNIT_BY_KEYWORD = (("电量", "kWh"), ("电能", "kWh"), ("千瓦时", "kWh"),
                    ("功率", "kW"), ("千瓦", "kW"), ("电压", "V"), ("电流", "A"))

#: ── 时间格式候选（**固定格式走 C 解析器，比 `format="mixed"` 快约 17 倍**，实测
#   20 万行：fixed 0.54s vs mixed 9.23s；按顺序探测，命中即缓存复用）
#   注意：**不含 `%Y`**——它会把小整数列（1/2/3）解析成"公元 1 年"，导致误判时间列
TIME_FORMATS = (
    "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%Y/%m/%d %H:%M",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y",
    "%Y%m%d", "%Y%m",
)

#: 时间列解析成功率门槛（低于此值换下一种格式，全部失败才退 `format="mixed"`）
TIME_FORMAT_MIN_RATE = 0.95

#: 类型推断：非空值中可解析为数值的比例门槛（PRD F1.2「可完整解析为数值」）
NUMERIC_MIN_RATE = 0.95

#: 越界体检规则（PRD F1.5：电压偏离 200–260V、功率为负）—— 按列名启发式命中
VOLTAGE_RANGE = (200.0, 260.0)


# ── 实体 ────────────────────────────────────────────────────────────────────

@dataclass
class ColumnSpec:
    """一列的安全列名 + 元数据（`schema_json.columns[]` 的构造来源）。"""

    name: str                       # 物理列名（ASCII）
    origin_name: str                # 原始列名（保留原名，决策 4）
    type: str                       # TEXT / REAL / INTEGER
    unit: str = "—"
    agg: str = "none"               # sum = 可求和；none = 不可求和（比率/维度/文本）
    description: str = ""
    derived: bool = False
    null_count: int = 0
    min: float | None = None
    max: float | None = None
    sample: list[Any] = field(default_factory=list)

    def to_schema_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "origin_name": self.origin_name,
            "type": self.type,
            "unit": self.unit,
            "agg": self.agg,
            "description": self.description,
            # `derived`：系统自动生成的时间戳/维度列（源表没有），供注入文本区分"用户列"与"派生列"
            "derived": self.derived,
            "null_count": self.null_count,
        }
        if self.min is not None:
            payload["min"] = self.min
        if self.max is not None:
            payload["max"] = self.max
        payload["sample"] = self.sample
        return payload


@dataclass
class ParseResult:
    """全量解析结果（upload 阶段返回值）。"""

    columns: list[ColumnSpec]
    row_count: int
    time_column: str
    time_start: str | None
    time_end: str | None
    quality: dict[str, Any]
    preview_columns: list[dict[str, str]]
    preview_rows: list[dict[str, Any]]
    raw_column_count: int


# ── 探测 ────────────────────────────────────────────────────────────────────

def detect_encoding(path: Path) -> str:
    """编码探测：UTF-8-BOM → UTF-8 → GBK → GB18030；全部失败抛 `E_ENCODING`。

    读取窗口（64KB）末尾可能切断一个多字节字符，因此**仅当解码错误出现在窗口末端**
    （`exc.start >= len(head) - 8`）时仍认为该编码可用——否则小文件里的中文会被误判为
    "无法识别编码"。
    """
    head = path.open("rb").read(65536)
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            head.decode(encoding)
            return encoding
        except UnicodeDecodeError as exc:
            if exc.start >= len(head) - 8:
                return encoding
            continue
    raise encoding_unknown()


def read_first_line(path: Path, encoding: str, limit: int = 1_048_576) -> str:
    """读取首行（表头），用于分隔符探测与表头校验。"""
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        return handle.readline(limit).rstrip("\r\n")


def detect_delimiter(first_line: str) -> str:
    """分隔符探测：按候选字符在表头中的出现次数取最大（比 `csv.Sniffer` 稳）。"""
    counts = {delimiter: first_line.count(delimiter) for delimiter in (",", ";", "\t", "|")}
    best = max(counts, key=lambda key: counts[key])
    return best if counts[best] > 0 else ","


def read_header(first_line: str, delimiter: str) -> list[str]:
    """解析表头并校验（非空、无重复列名，PRD F1.1）。"""
    raw = next(csv.reader([first_line], delimiter=delimiter))
    names = [name.replace("\ufeff", "").strip() for name in raw]
    if not names or all(not name for name in names):
        raise header_invalid("首行为空，未找到列名")
    if len(names) == 1:
        raise header_invalid("仅识别到 1 列，分隔符可能不是逗号/分号/制表符")
    blanks = [index + 1 for index, name in enumerate(names) if not name]
    if blanks:
        raise header_invalid(f"第 {blanks[:5]} 列为空列名")
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in names:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise header_invalid("存在重复列名", duplicates=duplicates)
    return names


# ── 列名与表名规范化 ────────────────────────────────────────────────────────

def _dict_key(name: str) -> str:
    """词典匹配键：NFKC 归一 → 小写 → 去括号内容 → 去空格/下划线/横线。"""
    text = unicodedata.normalize("NFKC", str(name)).lower()
    text = re.sub(r"[（(【\[].*?[)）】\]]", "", text)
    return re.sub(r"[\s_\-/]+", "", text)


def _ascii_safe(name: str) -> str:
    """ASCII 化：非字母数字转下划线、压缩重复下划线、去首尾、限长。"""
    text = unicodedata.normalize("NFKC", str(name)).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    text = re.sub(r"_{2,}", "_", text)
    return text[:MAX_NAME_LEN]


def _lookup_dictionary(origin: str) -> str | None:
    """词典查找：精确命中优先，其次包含命中（键长 ≥2，按长短序避免误配）。"""
    key = _dict_key(origin)
    if not key:
        return None
    exact = COLUMN_DICTIONARY.get(key)
    if exact:
        return exact
    for word, english in sorted(COLUMN_DICTIONARY.items(), key=lambda item: -len(item[0])):
        if len(word) >= 2 and word in key:
            return english
    return None


def normalize_columns(raw_names: list[str]) -> tuple[list[ColumnSpec], dict[str, str]]:
    """规范化列名，返回（列规格列表, 原始名 → 安全列名 的映射）。

    顺序：词典命中 → ASCII 净化 → `col_<序号>` 回退；重名自动追加 `_2`/`_3`。
    """
    specs: list[ColumnSpec] = []
    mapping: dict[str, str] = {}
    used: set[str] = set()

    for index, origin in enumerate(raw_names):
        english = _lookup_dictionary(origin)
        candidate = _ascii_safe(english) if english else _ascii_safe(origin)
        if not candidate or candidate[0].isdigit():
            # 纯中文（净化为空）或数字开头 → col_n 回退（决策 4）
            candidate = f"col_{index + 1}"
        name = candidate
        suffix = 2
        while name in used:
            name = f"{candidate}_{suffix}"[:MAX_NAME_LEN]
            suffix += 1
        used.add(name)
        mapping[origin] = name
        specs.append(
            ColumnSpec(
                name=name,
                origin_name=origin,
                type="TEXT",  # 类型在首块解析后回填
                unit=_infer_unit(name, origin),
                agg="none",  # 只有"数值列且非维度/比率"才会在定型时升级为 sum
                description=_describe(origin, name, mapped=bool(english)),
            )
        )
    return specs, mapping


def _resolve_agg(spec: ColumnSpec) -> str:
    """「可否 SUM」（PRD F1.2）：仅数值列、且非维度列、且非比率列才可求和。"""
    if spec.type not in ("REAL", "INTEGER"):
        return "none"
    if spec.name in DIMENSION_NAMES or spec.derived:
        return "none"
    return "none" if _is_ratio_name(spec.name, spec.origin_name) else "sum"


def _is_ratio_name(name: str, origin: str) -> bool:
    """比率列判定（PRD F1.2：ratio/rate/factor/coverage 类关键词 → 不可 SUM）。"""
    haystack = f"{name} {origin}".lower()
    return any(keyword in haystack for keyword in RATIO_KEYWORDS)


def _infer_unit(name: str, origin: str) -> str:
    for suffix, unit in _UNIT_BY_SUFFIX:
        if name.endswith(suffix):
            return unit
    for keyword, unit in _UNIT_BY_KEYWORD:
        if keyword in origin:
            return unit
    return "—"


def _describe(origin: str, name: str, *, mapped: bool) -> str:
    """列说明：保留原始列名（PRD F1.3「原始中文列名作为注释保留」）。"""
    if origin == name:
        return ""
    return f"源列名：{origin}{'（词典映射）' if mapped else ''}"


def build_table_name(filename: str, taken: set[str], fallback_suffix: str) -> str:
    """表名 = 文件名净化 → 冲突追加序号 → 净化后为空回退 `uploaded_<后缀>`（决策）。"""
    base = _ascii_safe(Path(filename).stem)
    if not base or base[0].isdigit():
        base = _ascii_safe(f"uploaded_{fallback_suffix}") or f"uploaded_{fallback_suffix}"
    if not re.match(r"^[a-z_]", base):
        base = f"t_{base}"[:MAX_NAME_LEN]
    name = base
    suffix = 2
    while name in taken:
        name = f"{base}_{suffix}"[:MAX_NAME_LEN]
        suffix += 1
    return name


# ── 时间列识别 ──────────────────────────────────────────────────────────────

def sniff_time_column_name(specs: list[ColumnSpec]) -> tuple[str | None, str | None]:
    """按列名挑选时间列，返回（日期列名, 时间列名）—— 支持 `Date`+`Time` 双列。"""
    names = {spec.name: spec for spec in specs}
    if "date" in names and "time" in names:
        return "date", "time"
    for candidate in ("datetime", "timestamp", "dt"):
        if candidate in names:
            return candidate, None
    for spec in specs:
        key = _dict_key(spec.origin_name)
        if any(word in key for word in TIME_NAME_CANDIDATES):
            return spec.name, None
    return None, None


def _parse_timestamp(series: pd.Series, fmt: str | None = None) -> pd.Series:
    """解析时间列：优先用固定格式（C 解析器），`fmt=None` 时退化为 `format="mixed"`。"""
    if fmt:
        return pd.to_datetime(series, format=fmt, errors="coerce")
    return pd.to_datetime(series, format="mixed", dayfirst=True, errors="coerce")


def _pick_time_format(series: pd.Series) -> str | None:
    """在候选格式中挑第一个成功率 ≥95% 的；全不命中返回 None（走 mixed）。"""
    probe = series.dropna().head(2000)
    if probe.empty:
        return None
    for fmt in TIME_FORMATS:
        parsed = pd.to_datetime(probe, format=fmt, errors="coerce")
        if parsed.notna().mean() >= TIME_FORMAT_MIN_RATE:
            return fmt
    return None


def _build_timestamp(chunk: pd.DataFrame, date_col: str, time_col: str | None) -> pd.Series:
    text = chunk[date_col]
    if time_col and time_col in chunk.columns:
        text = text.fillna("").astype("string") + " " + chunk[time_col].fillna("").astype("string")
    return text.astype("string").str.strip()


class _TimeResolver:
    """时间列来源决策：**只在首块决策一次**（来源列 + 格式），后续块复用，避免重复探测。"""

    def __init__(self, specs: list[ColumnSpec], date_col: str | None, time_col: str | None) -> None:
        self._specs = specs
        self._pair = (date_col, time_col) if date_col else None
        self._pair_ok = bool(date_col)
        self._single: str | None = None
        self._fmt: str | None = None
        self._decided = False

    def resolve(self, chunk: pd.DataFrame) -> pd.Series | None:
        if not self._decided:
            self._decide(chunk)
        text = self._time_text(chunk)
        if text is None:
            return None
        return _parse_timestamp(text, self._fmt)

    def _time_text(self, chunk: pd.DataFrame) -> pd.Series | None:
        if self._pair_ok and self._pair and self._pair[0] in chunk.columns:
            return _build_timestamp(chunk, self._pair[0], self._pair[1])
        if self._single and self._single in chunk.columns:
            return chunk[self._single].astype("string").str.strip()
        return None

    def _decide(self, chunk: pd.DataFrame) -> None:
        """决策顺序：Date+Time 双列 → 单列时间候选名 → 任意**文本列**的成功率探测。"""
        self._decided = True
        type_of = {spec.name: spec.type for spec in self._specs}
        is_text = lambda name: type_of.get(name, "TEXT") == "TEXT"  # noqa: E731
        if self._pair_ok and self._pair and is_text(self._pair[0]):
            fmt, rate = _probe_time(_build_timestamp(chunk, self._pair[0], self._pair[1]))
            if rate >= 0.5:
                self._fmt = fmt
                return
            self._pair_ok = False
        for name in (TIME_COLUMN, "datetime", "timestamp"):
            if name in chunk.columns and is_text(name):
                fmt, rate = _probe_time(chunk[name].astype("string").str.strip())
                if rate >= 0.5:
                    self._single, self._fmt = name, fmt
                    return
        for spec in self._specs:
            # 只探测文本列：数值列本身不可能是日期（否则小整数会被 %Y 类格式误判）
            if spec.name not in chunk.columns or spec.type != "TEXT":
                continue
            fmt, rate = _probe_time(chunk[spec.name].astype("string").str.strip())
            if rate >= 0.9:
                self._single, self._fmt = spec.name, fmt
                return


def _probe_time(text: pd.Series) -> tuple[str | None, float]:
    """探测（格式, 成功率）：固定格式优先；无匹配格式时退 `mixed` 但要求成功率 ≥0.9。"""
    fmt = _pick_time_format(text)
    rate = float(_parse_timestamp(text, fmt).notna().mean())
    return fmt, rate


# ── 主流程：分块解析 + 规范化落盘 ───────────────────────────────────────────

def parse_and_normalize(
    src: Path,
    dst: Path,
    *,
    encoding: str,
    delimiter: str,
    chunk_rows: int = PARSE_CHUNK_ROWS,
    preview_rows: int = 20,
) -> ParseResult:
    """全量解析一次：写规范化中间 CSV，同时产出 Schema / 质量 / 预览。

    失败一律抛业务异常（`E_HEADER_INVALID` / `E_NO_TIME_COLUMN` / `E_NO_NUMERIC_COLUMN` / `E_PARSE`）。
    """
    raw_names = read_header(read_first_line(src, encoding), delimiter)
    specs, mapping = normalize_columns(raw_names)
    date_col, time_col = sniff_time_column_name(specs)
    resolver = _TimeResolver(specs, date_col, time_col)

    reader = pd.read_csv(
        src,
        sep=delimiter,
        encoding=encoding,
        dtype=str,
        keep_default_na=False,
        na_values=NA_VALUES,
        chunksize=chunk_rows,
        on_bad_lines="warn",
    )

    timestamps: list[np.ndarray] = []
    null_counts: dict[str, int] = {spec.name: 0 for spec in specs}
    samples: dict[str, list[Any]] = {spec.name: [] for spec in specs}
    constant_track: dict[str, set[str]] = {spec.name: set() for spec in specs}
    out_of_range = {"voltage_out_of_range_rows": 0, "negative_power_rows": 0}
    total_rows = missing_rows = partially_missing_rows = 0
    derived_specs: list[ColumnSpec] = []
    preview_rows_out: list[dict[str, Any]] = []
    time_seen = False
    time_nonnull = 0
    first_chunk = True
    handle = None
    locked: set[str] = set()
    attempts: dict[str, int] = {}

    try:
        for chunk in reader:
            chunk = _prepare_chunk(chunk, specs, raw_names)
            if chunk.empty:
                continue
            _infer_and_lock_types(chunk, specs, locked, attempts)
            if first_chunk:
                derived_specs = _make_derived_specs(specs)
                if not any(spec.type in ("REAL", "INTEGER") for spec in specs):
                    raise no_numeric_column([spec.origin_name for spec in specs])
                first_chunk = False

            stamp = resolver.resolve(chunk)
            if stamp is not None:
                valid = stamp.dropna()
                if len(valid):
                    time_seen = True
                    time_nonnull += len(valid)
                    timestamps.append(valid.dt.as_unit("ns").astype("int64").to_numpy())

            chunk = _apply_types(chunk, specs, locked)
            value_frame = chunk[[spec.name for spec in specs]]
            row_missing = value_frame.isna().all(axis=1)
            missing_rows += int(row_missing.sum())
            partially_missing_rows += int((value_frame.isna().any(axis=1) & ~row_missing).sum())
            for spec in specs:
                series = chunk[spec.name]
                null_counts[spec.name] += int(series.isna().sum())
                _accumulate(spec, series, samples, constant_track)
            _count_out_of_range(chunk, specs, out_of_range)

            normalized = _assemble(chunk, specs, derived_specs, stamp)
            if not preview_rows_out:
                preview_rows_out = _preview_records(normalized, preview_rows)
            if handle is None:
                handle = dst.open("w", encoding="utf-8", newline="")
                normalized.to_csv(handle, index=False, header=True)
            else:
                normalized.to_csv(handle, index=False, header=False)
            total_rows += len(normalized)
    except pd.errors.ParserError as exc:
        raise parse_error(str(exc).splitlines()[0]) from exc
    except UnicodeDecodeError as exc:
        raise encoding_unknown("按探测到的编码读取时仍遇到非法字节，请另存为 UTF-8") from exc
    finally:
        if handle is not None:
            handle.close()

    if first_chunk:
        raise header_invalid("文件中没有可解析的数据行")
    if not time_seen or not timestamps:
        raise no_time_column([spec.origin_name for spec in specs])

    time_start, time_end, duplicate_timestamps, time_axis_gap = _time_stats(timestamps)
    # 列顺序必须与 `_assemble` 写出的规范化 CSV 表头完全一致：源列 → dt → 派生维度列
    all_specs = [*specs, *_time_spec(specs), *derived_specs]
    derived_nulls = max(0, total_rows - time_nonnull)
    for spec in all_specs:
        spec.null_count = derived_nulls if spec.derived else null_counts.get(spec.name, 0)
        spec.sample = samples.get(spec.name, [])[:3]

    quality = _build_quality(
        total_rows=total_rows,
        specs=all_specs,
        missing_rows=missing_rows,
        partially_missing_rows=partially_missing_rows,
        null_counts=null_counts,
        constant_track=constant_track,
        duplicate_timestamps=duplicate_timestamps,
        time_axis_gap=time_axis_gap,
        out_of_range=out_of_range,
        time_start=time_start,
        time_end=time_end,
    )
    logger.info(
        "解析完成：%d 行 × %d 列（原始 %d 列，改名 %d 列），时间范围 %s ~ %s",
        total_rows, len(all_specs), len(raw_names), len(mapping), time_start, time_end,
    )
    return ParseResult(
        columns=all_specs,
        row_count=total_rows,
        time_column=TIME_COLUMN,
        time_start=time_start,
        time_end=time_end,
        quality=quality,
        preview_columns=[{"name": spec.name, "type": spec.type} for spec in all_specs],
        preview_rows=preview_rows_out,
        raw_column_count=len(raw_names),
    )


def _prepare_chunk(
    chunk: pd.DataFrame, specs: list[ColumnSpec], raw_names: list[str]
) -> pd.DataFrame:
    """列名对齐 → 值去空白 → 缺失标记归一（`?`/空串 → NA）→ 丢弃全空行。

    值清洗走**向量化**（StringDtype 的 `str.strip` + `mask`），不用逐元素 map：
    实测 20 万行 × 9 列约 0.3s，逐元素 map 约 5 倍慢。
    """
    if list(chunk.columns) == raw_names:
        chunk.columns = [spec.name for spec in specs]
    else:
        # pandas 对表头的空白/引号处理可能与我们的首行解析不同 → 按位置对齐
        if len(chunk.columns) != len(specs):
            raise parse_error(f"数据列数（{len(chunk.columns)}）与表头列数（{len(specs)}）不一致")
        chunk.columns = [spec.name for spec in specs]
    for column in chunk.columns:
        series = chunk[column]
        if series.dtype == object or isinstance(series.dtype, pd.StringDtype):
            text = series.astype("string").str.strip()
            chunk[column] = text.mask(text.isin(_NA_SET), None)
    return chunk.dropna(how="all")


def _infer_and_lock_types(
    chunk: pd.DataFrame,
    specs: list[ColumnSpec],
    locked: set[str],
    attempts: dict[str, int],
) -> None:
    """逐列定型：数值比例 ≥95% 判数值型；本块全空则**延后到下一块再判**（最多 3 块）。"""
    for spec in specs:
        if spec.name in locked:
            continue
        attempts[spec.name] = attempts.get(spec.name, 0) + 1
        inferred = _infer_type(chunk[spec.name])
        if inferred == "TEXT" and attempts[spec.name] < 3 and chunk[spec.name].notna().sum() == 0:
            continue  # 全是空值，无从判断，留给下一块
        spec.type = inferred
        spec.agg = _resolve_agg(spec)
        locked.add(spec.name)


def _infer_type(series: pd.Series) -> str:
    """类型推断（PRD F1.2「可完整解析为数值」）：非空值中数值可解析率 ≥95% 判数值型。"""
    numeric = pd.to_numeric(series, errors="coerce")
    total = int(series.notna().sum())
    if total == 0:
        return "TEXT"
    valid = numeric.dropna()
    if len(valid) / total < NUMERIC_MIN_RATE:
        return "TEXT"
    if bool((valid % 1 == 0).all()):
        return "INTEGER"
    return "REAL"


def _apply_types(chunk: pd.DataFrame, specs: list[ColumnSpec], locked: set[str]) -> pd.DataFrame:
    """按已定型类型转换数值列（转不动的置 NA，计入缺失）。"""
    for spec in specs:
        if spec.name in locked and spec.type in ("REAL", "INTEGER") and spec.name in chunk.columns:
            numeric = pd.to_numeric(chunk[spec.name], errors="coerce")
            chunk[spec.name] = numeric.round(6) if spec.type == "REAL" else numeric
    return chunk


def _time_spec(specs: list[ColumnSpec]) -> list[ColumnSpec]:
    """标准时间戳列 `dt` 的规格（源表已有同名 `dt` 列时不再另建，其值被规范化覆盖）。"""
    if any(spec.name == TIME_COLUMN for spec in specs):
        return []
    return [
        ColumnSpec(
            name=TIME_COLUMN,
            origin_name=TIME_COLUMN,
            type="TEXT",
            unit="—",
            agg="none",
            description="标准时间戳 'YYYY-MM-DD HH:MM:SS'（由识别出的时间列解析生成）",
            derived=True,
        )
    ]


def _make_derived_specs(specs: list[ColumnSpec]) -> list[ColumnSpec]:
    """派生维度列规格（与预置表同名同义；源表已有同名列则不重复派生）。"""
    existing = {spec.name for spec in specs}
    units = {"weekday": "0=周一"}
    return [
        ColumnSpec(
            name=name,
            origin_name=name,
            type="INTEGER",
            unit=units.get(name, "—"),
            agg="none",
            description="由时间列派生的维度列（不可 SUM）",
            derived=True,
        )
        for name in DERIVED_COLUMNS
        if name not in existing
    ]


def _assemble(
    chunk: pd.DataFrame,
    specs: list[ColumnSpec],
    derived_specs: list[ColumnSpec],
    stamp: pd.Series | None,
) -> pd.DataFrame:
    """组装待落盘的数据块：原始列（类型已转）+ 标准时间戳 `dt` + 派生维度列。"""
    frame = chunk[[spec.name for spec in specs]].copy()
    existing = {spec.name for spec in specs}
    if stamp is None:
        # 保留列形状：本块时间列全空时也要写出 dt 占位列，否则各块列数不一致导致 CSV 错列
        frame[TIME_COLUMN] = None
    else:
        frame[TIME_COLUMN] = stamp.dt.strftime("%Y-%m-%d %H:%M:%S")
        derived_values = {
            "year": stamp.dt.year,
            "month": stamp.dt.month,
            "day": stamp.dt.day,
            "hour": stamp.dt.hour,
            "weekday": stamp.dt.weekday,
            "is_weekend": stamp.dt.weekday.isin([5, 6]).astype("int64"),
        }
        for name, values in derived_values.items():
            if name not in existing:  # 源表已有同名列则保留原值，不覆盖
                frame[name] = values
    for spec in derived_specs:
        if spec.name not in frame.columns:
            frame[spec.name] = None
    ordered = list(
        dict.fromkeys(
            [spec.name for spec in specs] + [TIME_COLUMN] + [spec.name for spec in derived_specs]
        )
    )
    return frame[[name for name in ordered if name in frame.columns]]


def _accumulate(
    spec: ColumnSpec,
    series: pd.Series,
    samples: dict[str, list[Any]],
    constant_track: dict[str, set[str]],
) -> None:
    """逐块累计 min/max、样例值（前 3 个非空）与恒定值判定（按整块去重，最多留 2 个值）。"""
    valid = series.dropna()
    if len(valid) == 0:
        return
    if len(samples[spec.name]) < 3:
        samples[spec.name].extend(valid.head(3 - len(samples[spec.name])).tolist())
    if len(constant_track[spec.name]) < 2:
        constant_track[spec.name].update(str(value) for value in pd.unique(valid)[:2])
    if spec.type in ("REAL", "INTEGER"):
        current_min = float(valid.min())
        current_max = float(valid.max())
        spec.min = current_min if spec.min is None else min(spec.min, current_min)
        spec.max = current_max if spec.max is None else max(spec.max, current_max)


def _count_out_of_range(
    chunk: pd.DataFrame, specs: list[ColumnSpec], counter: dict[str, int]
) -> None:
    """F1.5 越界体检：电压偏离 200–260V、功率为负。"""
    for spec in specs:
        if spec.type not in ("REAL", "INTEGER") or spec.name not in chunk.columns:
            continue
        series = chunk[spec.name]
        if "voltage" in spec.name or "电压" in spec.origin_name:
            out = ((series < VOLTAGE_RANGE[0]) | (series > VOLTAGE_RANGE[1])).sum()
            counter["voltage_out_of_range_rows"] += int(out)
        elif "power" in spec.name or "功率" in spec.origin_name:
            counter["negative_power_rows"] += int((series < 0).sum())


def _preview_records(frame: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    """预览记录：NaN → None（JSON 友好）。"""
    return [
        {key: (None if _is_na(value) else value) for key, value in record.items()}
        for record in frame.head(limit).to_dict(orient="records")
    ]


def _is_na(value: Any) -> bool:
    """None / NaN / NaT / pd.NA 统一判为缺失（预览与入库时需转成 JSON/SQL 的 NULL）。"""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _time_stats(timestamps: list[np.ndarray]) -> tuple[str | None, str | None, int, int]:
    """时间戳统计：起止、重复数、时间轴缺口（按最小间隔推算理论行数）。"""
    stacked = np.concatenate(timestamps) if timestamps else np.array([], dtype="int64")
    if stacked.size == 0:
        return None, None, 0, 0
    unique = np.unique(stacked)
    duplicates = int(stacked.size - unique.size)
    deltas = np.diff(unique)
    positive = deltas[deltas > 0]
    granularity = int(np.median(positive)) if positive.size else 0
    gap = 0
    if granularity > 0:
        expected = int((unique[-1] - unique[0]) // granularity) + 1
        gap = max(0, expected - int(unique.size))
    to_text = lambda value: pd.Timestamp(int(value), unit="ns").strftime("%Y-%m-%d %H:%M:%S")
    return to_text(unique[0]), to_text(unique[-1]), duplicates, gap


def _build_quality(
    *,
    total_rows: int,
    specs: list[ColumnSpec],
    missing_rows: int,
    partially_missing_rows: int,
    null_counts: dict[str, int],
    constant_track: dict[str, set[str]],
    duplicate_timestamps: int,
    time_axis_gap: int,
    out_of_range: dict[str, int],
    time_start: str | None,
    time_end: str | None,
) -> dict[str, Any]:
    """数据集级质量报告（PRD F1.5；`checks` 留空——A1–A5 为预置数据口径）。"""
    columns = [spec for spec in specs if not spec.derived]
    constant_columns = [
        spec.name
        for spec in columns
        if len(constant_track.get(spec.name, set())) == 1 and spec.sample
    ]
    expected_rows = total_rows + time_axis_gap
    return {
        "level": "dataset",
        "note": "上传数据集通用质量体检；A1–A5 口径校验仅适用于系统预置报表",
        "caliber_version": None,
        "checked_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": {
            "total_rows": total_rows,
            "cleaned_rows": total_rows,
            "time_start": time_start,
            "time_end": time_end,
            "missing_rows": missing_rows,
            "missing_ratio": round(missing_rows / total_rows, 6) if total_rows else 0.0,
            "partially_missing_rows": partially_missing_rows,
            "missing_by_column": {spec.name: null_counts.get(spec.name, 0) for spec in columns},
            "duplicate_timestamps": duplicate_timestamps,
            "time_axis_gap": time_axis_gap,
            "coverage": round(total_rows / expected_rows, 6) if expected_rows else 0.0,
            "constant_columns": constant_columns,
            **out_of_range,
        },
        "checks": {},
    }
