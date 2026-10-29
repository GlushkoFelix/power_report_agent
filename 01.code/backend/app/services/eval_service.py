"""智电析报 · 评测服务（交付物 8 / M5，PRD F5.1–F5.3）。

三块职责：

1. **判分**（`score_result_sets` / `score_key_numbers`）——PRD F5.2 口径：
   列名归一化 + **列序无关、行序无关**，数值容差 1e-6（比率字段 ±0.01 个百分点）；
   三级兑底（① 列位置 → ② 列名归一 → ③ **单列格式宽松档**：日期 `2006-12-17` 与
   `2006-12-17 00:00:00`、数值 `12.5` 与 `12.50` 这类"同值不同写法"不算错），
   全不过才判错并记录 `diff_type`；
2. **批次执行**（`start_run` + 后台任务）——逐题走**完整分析链路**（`analysis_service.run_once`），
   多轮题按换行拆轮顺序提问、判分只看最后一轮；模式 `impl`：
   - `real`：必须配置了模型 Key（`LLM_API_KEY`），否则 400（**不静默回落**）；
   - `selfcheck`：用 `ScriptedLLM` 跑通链路（验证落库/判分/报告），`model_name=scripted`，
     报告置顶标注"自检模式，正确率无意义"——避免自检数字被当成验收结论（P4 同源要求）；
   - `auto`：有 Key 走 real，否则 selfcheck。
3. **指标与报告**（`_aggregate` + `_write_report`）——总体/分类正确率、结论命中率、
   平均与 P95 耗时、错误与重试分布，逐题明细存 `eval_runs.result_json`（TBD6 定案：不拆表），
   Markdown 报告落 `02.data/eval/reports/{run_code}.md`。

已知取舍（如实记录）：
- `first_token_avg_ms` 恒 NULL：当前 LLM 客户端是**单次 POST**（非流式），拿不到首字时间；
- `llm_calls.eval_run_id` 在批次结束后**批量回填**（逐题分析产生的调用按 `analysis_id` 反查），
  避免把评测 ID 穿透进分析链路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.agent import prompts
from app.core.config import DATA_DIR, get_settings
from app.core.errors import not_found, validation_error
from app.core.llm_client import ScriptedLLM, get_llm, set_llm
from app.db.models import Analysis, Conversation, EvalQuestion, EvalRun, LLMCall, UploadedReport
from app.db.session import control_session
from app.agent import dispatch
from app.services import analysis_service

logger = logging.getLogger("zdxb.eval")

__all__ = [
    "start_run",
    "list_questions",
    "list_runs",
    "get_run",
    "get_report_markdown",
    "score_result_sets",
    "score_key_numbers",
    "REPORT_DIR",
    "ATTRIBUTION_ORDER",
]

# ── 判分常量（PRD F5.2）────────────────────────────────────────────────────

#: 数值容差（绝对下限）
NUM_TOLERANCE = 1e-6
#: 比率字段容差：±0.01 个百分点 = 0.0001（比率以 0–1 存储）
PERCENT_TOLERANCE = 1e-4
#: **相对容差（v1.19.6 新增，真实模型评测暴露的判分过严问题）**：模型输出常带 `ROUND(x, 2)`
#: ——实测 gold `26.0179676902` vs 模型 `26.02`（差 0.002）、gold `79.5564333333` vs `79.56`，
#: 都被 1e-6 **绝对**容差判成 `value_mismatch`，但它们是同一结果的"展示级舍入"，不是算错。
REL_TOLERANCE = 1e-3
#: 展示级舍入容差（v1.19.6）：模型习惯 `ROUND(x, 2)`，误差上界就是**半个末位 0.005**。
#: 只放宽"相对 1e-3"对比率列不够用（0.4 的 0.1% 只有 4e-4），因此再加这个绝对下限；
#: 对 `|值| < 0.01` 的近零值**不放宽**，避免比率类小值被抬成几倍的误差。
ROUNDING_TOLERANCE = 5e-3
#: 列名里出现这些词 → 按"比率字段"放宽容差
_RATIO_HINTS = ("ratio", "rate", "coverage", "percent", "share", "load_factor")
#: 日期/时间形态识别（第三档"格式宽松"只对这类值生效）
_DATE_LIKE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
#: 「年-月」形态（v1.19.6：与整数月等价，如 `'2006-12'` ≈ `12`）
_MONTH_IN_YEAR_RE = re.compile(r"^(\d{4})[-/](\d{1,2})$")
#: 列名归一化的同义映射（模型自由起别名时的兜底）
_NAME_ALIASES = {
    "total": "total_kwh",
    "sum": "total_kwh",
    "cnt": "days",
    "count": "days",
    "rows": "days",
    "avg": "avg_kwh",
    "mean": "avg_kwh",
}

#: 评测批次专用会话的编号前缀（v1.19.12 起作为**公开标识**使用）：
#: 个人中心的「重跑」据此判定该会话是**只读的评测留痕会话**（用户不会在里面提问），
#: 从而把重跑另起新会话执行，避免污染批次留痕。
EVAL_CONVERSATION_PREFIX = "c_eval_"

#: 评测报告目录（相对路径入库：`02.data/eval/reports/xxx.md`）
REPORT_DIR = DATA_DIR / "eval" / "reports"

#: 后台批次任务（强引用，防 GC 取消任务）
_TASKS: set[asyncio.Task] = set()


# ══════════════════════════════════════════════════════════════════════════
#  ① 判分引擎
# ══════════════════════════════════════════════════════════════════════════

def _normalize_name(name: str) -> str:
    """列名归一：小写、去掉非字母数字、同义词归并（`SUM(x) AS total` ≈ `total_kwh`）。"""
    text = "".join(char for char in str(name).lower() if char.isalnum() or char == "_")
    return _NAME_ALIASES.get(text, text)


def _canonical(value: Any) -> tuple:
    """用于排序的规范化键（浮点按 9 位小数归一，避免 1.0 与 1.0000000001 排序错位）。"""
    if isinstance(value, bool):
        return ("n", float(value))
    if isinstance(value, (int, float)):
        return ("n", round(float(value), 9))
    if value is None:
        return ("z",)
    return ("s", str(value).strip().lower())


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _numbers_close(left: float, right: float, *, percent: bool = False) -> bool:
    """数值近似相等：`max(绝对下限, |两侧较大值| × 相对容差, 展示级舍入下限)`。

    - 绝对下限：普通列 1e-6 / 比率列 1e-4（PRD F5.2 原口径，浮点误差与近零值靠它兜底）；
    - 相对容差 1e-3：与结论命中率的 0.5% 同向，拦住"量级性"错误；
    - 展示级舍入 5e-3：模型 `ROUND(x, 2)` 的误差上界（仅 `|值| ≥ 0.01` 时生效）。
    """
    peak = max(abs(left), abs(right))
    base = PERCENT_TOLERANCE if percent else NUM_TOLERANCE
    rounding = ROUNDING_TOLERANCE if peak >= 0.01 else 0.0
    return abs(left - right) <= max(base, peak * REL_TOLERANCE, rounding)


def _first_number(text: str) -> float | None:
    """取文本里的**第一个**数值（`"12.5 kWh"` → 12.5；没有则 None）。"""
    match = re.search(r"-?\d+(?:\.\d+)?", text or "")
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:  # pragma: no cover - 正则已限定形态
        return None


def _month_equal(text_side: Any, number_side: Any) -> tuple[bool, bool]:
    """`'2006-12'` 与整数月（1–12）等价判断；返回 `(是否等价, 是否需要继续按类型冲突判)`。

    背景（v1.19.6，真实模型评测暴露的 6 条 `type_mismatch`）：模型偏好用
    `STRFTIME('%Y-%m', date)` 给出 `'2006-12'`，而 gold 用现成的整数列 `month = 12`——
    **值对、形态不同**。原来的"子串包含"判断还会被数值的浮点文本坑到
    （`str(12.0)` 是 `'12.0'`，于是 `'12.0' in '2006-12'` 为假）。
    """
    if not isinstance(text_side, str):
        return False, False
    number = _as_number(number_side)
    if number is None or not float(number).is_integer():
        return False, False
    month = int(number)
    if not 1 <= month <= 12:
        return False, False
    match = _MONTH_IN_YEAR_RE.match(text_side.strip())
    if not match:
        return False, False
    return int(match.group(2)) == month, True


def _cell_equal(left: Any, right: Any, *, percent: bool = False) -> tuple[bool, bool]:
    """单元格比较；返回 `(是否相等, 是否类型冲突)`。"""
    left_num, right_num = _as_number(left), _as_number(right)
    if left_num is not None and right_num is not None:
        # 容差 = max(绝对下限, |两侧较大值| × 相对容差)：兼顾浮点误差与"展示级舍入"
        return _numbers_close(left_num, right_num, percent=percent), False
    if left_num is None and right_num is None:
        return _canonical(left) == _canonical(right), False
    # 年月形态等价（v1.19.6）：`'2006-12'` ≈ `12`（值对、写法不同，不算错）
    for text_side, number_side in ((left, right), (right, left)):
        equal, handled = _month_equal(text_side, number_side)
        if handled:
            return equal, False
    # 一侧数字、另一侧文本（如 gold `12.5` vs 模型 `"12.5 kWh"`）：
    # 取文本里**第一个数值**按同一容差比较。v1.19.6 由"子串包含"改为数值比较——
    # 子串匹配既会把 `12` 与 `2006-12` 判错（浮点文本 `'12.0'`），也会把 `12` 与 `1234` 误判为相等。
    text_left, text_right = str(left).strip(), str(right).strip()
    if right_num is not None and text_left:
        found = _first_number(text_left)
        if found is None:
            return False, True  # 文本里没有数字 → 真实的"数字 vs 文本"类型冲突
        return _numbers_close(found, right_num, percent=percent), False
    if left_num is not None and text_right:
        found = _first_number(text_right)
        if found is None:
            return False, True
        return _numbers_close(found, left_num, percent=percent), False
    return False, True


def _loose_text_equal(left: Any, right: Any) -> bool:
    """格式宽松相等：**仅日期/时间形态**允许"一个是另一个的前缀"。

    为什么限定日期：`2006-12-17` 与 `2006-12-17 00:00:00` 是同一个时刻的两种写法；
    而数值字符串的前缀关系（`100.0` 与 `100.0001`）**不是**同值，一律按数值容差判。
    """
    text_left, text_right = str(left).strip(), str(right).strip()
    if not text_left or not text_right:
        return False
    if _DATE_LIKE_RE.match(text_left) and _DATE_LIKE_RE.match(text_right):
        return text_left.startswith(text_right) or text_right.startswith(text_left)
    return False


def _percent_flags(columns: list[str]) -> list[bool]:
    return [any(hint in str(column).lower() for hint in _RATIO_HINTS) for column in columns]


def _row_tuples(rows: list[dict], columns: list[str]) -> list[tuple]:
    return [tuple(row.get(column) for column in columns) for row in rows]


def _compare_tuples(
    gold_rows: list[tuple], gen_rows: list[tuple], percents: list[bool]
) -> tuple[bool, str | None]:
    """无序比对两组行；返回 `(是否全部匹配, 第一个差异类型)`。"""
    if len(gold_rows) != len(gen_rows):
        return False, "row_count_mismatch"
    gold_sorted = sorted(gold_rows, key=lambda row: tuple(_canonical(cell) for cell in row))
    gen_sorted = sorted(gen_rows, key=lambda row: tuple(_canonical(cell) for cell in row))
    diff_type: str | None = None
    all_equal = True
    for gold_row, gen_row in zip(gold_sorted, gen_sorted):
        for index, (gold_cell, gen_cell) in enumerate(zip(gold_row, gen_row)):
            equal, type_conflict = _cell_equal(
                gen_cell, gold_cell, percent=percents[index] if index < len(percents) else False
            )
            if not equal:
                all_equal = False
                diff_type = diff_type or ("type_mismatch" if type_conflict else "value_mismatch")
    return all_equal, diff_type


def _match_columns_by_name(gold_columns: list[str], gen_columns: list[str]) -> list[int] | None:
    """按归一化列名把 gen 列映射到 gold 列序；映射不全返回 None。"""
    normalized_gold = [_normalize_name(column) for column in gold_columns]
    mapping: list[int] = []
    used: set[int] = set()
    for target in normalized_gold:
        found = None
        for index, column in enumerate(gen_columns):
            if index in used:
                continue
            if _normalize_name(column) == target:
                found = index
                break
        if found is None:  # 退一步：名称互相包含（`peak` ≈ `peak_kwh`）
            for index, column in enumerate(gen_columns):
                if index in used:
                    continue
                if target and (_normalize_name(column) in target or target in _normalize_name(column)):
                    found = index
                    break
        if found is None:
            return None
        used.add(found)
        mapping.append(found)
    return mapping


def score_result_sets(gold: dict, gen: dict) -> dict:
    """结果集判分（PRD F5.2 三级兑底）。

    `gold`/`gen` 均为 `{"columns": [...], "rows": [...]}`；返回
    `{"is_correct", "tier", "diff_type", "detail"}`——`tier` 记录命中的判分档（1/2/3，None=判错）。
    """
    gold_columns = [str(column) for column in gold.get("columns") or []]
    gold_rows_raw = gold.get("rows") or []
    gen_columns = [str(column) for column in gen.get("columns") or []]
    gen_rows_raw = gen.get("rows") or []

    if not gen_columns or not gen_rows_raw:
        return {"is_correct": False, "tier": None, "diff_type": "empty_result",
                "detail": "生成结果为空（未执行成功或没有数据）"}

    # ① 第一档：列位置对齐（gold 列序即答案列序）+ 行序无关
    if len(gen_columns) == len(gold_columns):
        percents = _percent_flags(gold_columns)
        ok, diff_type = _compare_tuples(
            _row_tuples(gold_rows_raw, gold_columns), _row_tuples(gen_rows_raw, gen_columns), percents
        )
        if ok:
            return {"is_correct": True, "tier": 1, "diff_type": None,
                    "detail": "列位置与取值一致（行序无关）"}

    # ② 第二档：列名归一后重排再比
    if len(gen_columns) != len(gold_columns):
        return {"is_correct": False, "tier": None, "diff_type": "column_count_mismatch",
                "detail": f"列数不符：gold {len(gold_columns)} 列（{gold_columns}），"
                          f"生成 {len(gen_columns)} 列（{gen_columns}）"}
    mapping = _match_columns_by_name(gold_columns, gen_columns)
    if mapping is not None:
        reordered = [{column: row.get(gen_columns[mapping[index]]) for index, column in enumerate(gold_columns)}
                     for row in gen_rows_raw]
        percents = _percent_flags(gold_columns)
        ok, diff_type = _compare_tuples(
            _row_tuples(gold_rows_raw, gold_columns), _row_tuples(reordered, gold_columns), percents
        )
        if ok:
            return {"is_correct": True, "tier": 2, "diff_type": None,
                    "detail": f"列名归一后一致（生成列名：{gen_columns}）"}

    # ③ 第三档（**仅单列结果**）：格式宽松档——日期/时间与数字的"同值不同写法"不算错
    #    （如 gold `2006-12-17` vs 生成 `2006-12-17 00:00:00`、gold `12.50` vs 生成 `12.5`）。
    #    为什么限定单列：多列时"忽略列身份"会把"列搞反"误判成正确（见交付物 8 决策）。
    #    说明：单列的**数值**集合比对与第一档等价（位置即身份），因此本档只补"格式差异"这一类。
    if len(gold_columns) == 1 and len(gen_columns) == 1 and len(gold_rows_raw) == len(gen_rows_raw):
        gold_values = sorted(
            (row.get(gold_columns[0]) for row in gold_rows_raw), key=_canonical
        )
        gen_values = sorted((row.get(gen_columns[0]) for row in gen_rows_raw), key=_canonical)
        loose = 0
        for gold_cell, gen_cell in zip(gold_values, gen_values):
            equal, _ = _cell_equal(gen_cell, gold_cell, percent=_percent_flags(gold_columns)[0])
            if equal or _loose_text_equal(gold_cell, gen_cell):
                loose += 1
        if gold_values and loose == len(gold_values):
            return {"is_correct": True, "tier": 3, "diff_type": None,
                    "detail": "单列结果格式宽松匹配一致（日期/数值写法差异）"}

    # 判错：给出尽量准确的差异类型
    if len(gen_rows_raw) != len(gold_rows_raw):
        diff_type = "row_count_mismatch"
        detail = f"行数不符：gold {len(gold_rows_raw)} 行，生成 {len(gen_rows_raw)} 行"
    else:
        percents = _percent_flags(gold_columns)
        _, cell_diff = _compare_tuples(
            _row_tuples(gold_rows_raw, gold_columns), _row_tuples(gen_rows_raw, gen_columns), percents
        )
        diff_type = cell_diff or "value_mismatch"
        detail = f"取值不一致（差异类型：{diff_type}）"
    return {"is_correct": False, "tier": None, "diff_type": diff_type, "detail": detail}


_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*%?")


def _extract_numbers(text: str) -> list[float]:
    """从解读文本里抓出所有数字（含千分位与百分号）。"""
    numbers: list[float] = []
    for raw in _NUMBER_RE.findall(text or ""):
        cleaned = raw.replace(",", "").rstrip("%")
        try:
            numbers.append(float(cleaned))
        except ValueError:
            continue
    return numbers


def score_key_numbers(keys: list[dict], narrative: str) -> dict:
    """结论准确性判分：`key_numbers` 是否出现在解读里（数值按有效位容差、标签按文本包含）。

    命中率 = 命中数 / 总关键项；`kind=label` 的项（如"最高的月份是 1 月"）按文本包含判定。
    """
    if not keys:
        return {"total": 0, "hit": 0, "hit_rate": None, "missed": []}
    text = narrative or ""
    text_numbers = _extract_numbers(text)
    hit = 0
    missed: list[dict] = []
    for key in keys:
        value = key.get("value")
        if key.get("kind") == "label" or isinstance(value, str):
            target = str(value).strip()
            matched = bool(target) and target in text
        else:
            number = _as_number(value)
            matched = False
            if number is not None:
                # 解读里的数字常被四舍五入/换单位 → 用 0.5% 相对容差或整数部分匹配
                matched = any(
                    abs(candidate - number) <= max(1e-6, abs(number) * 0.005)
                    for candidate in text_numbers
                ) or (abs(number) >= 1 and f"{number:,.0f}".rstrip("0").rstrip(".") in text)
        if matched:
            hit += 1
        else:
            missed.append(key)
    return {"total": len(keys), "hit": hit, "hit_rate": round(hit / len(keys), 4), "missed": missed}


# ══════════════════════════════════════════════════════════════════════════
#  ② 批次执行
# ══════════════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


async def list_questions(
    *, qtype: str | None = None, difficulty: str | None = None,
    is_trap: bool | None = None, page: int = 1, page_size: int = 50,
) -> dict[str, Any]:
    """题库列表（PRD 9.1 `GET /eval/questions`）；题库是全局资产，不做用户隔离。"""
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    conditions = []
    if qtype:
        conditions.append(EvalQuestion.qtype == qtype)
    if difficulty:
        conditions.append(EvalQuestion.difficulty == difficulty)
    if is_trap is not None:
        conditions.append(EvalQuestion.is_trap == (1 if is_trap else 0))
    async with control_session() as session:
        total = await session.scalar(
            select(func.count()).select_from(EvalQuestion).where(*conditions)
        ) or 0
        rows = (
            await session.execute(
                select(EvalQuestion)
                .where(*conditions)
                .order_by(EvalQuestion.sort_no, EvalQuestion.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
    items = [
        {
            "question_code": row.question_code,
            "question": row.question,
            "qtype": row.qtype,
            "difficulty": row.difficulty,
            "report_table": row.report_table,
            "is_trap": bool(row.is_trap),
            "trap_rule": row.trap_rule,
            "tags": row.tags,
            "gold_sql": row.gold_sql,
            "key_numbers": json.loads(row.key_numbers_json) if row.key_numbers_json else [],
        }
        for row in rows
    ]
    return {"total": total, "page": page, "page_size": page_size, "items": items}


async def _load_question_rows(
    question_codes: list[str] | None, limit: int | None
) -> list[EvalQuestion]:
    async with control_session() as session:
        statement = select(EvalQuestion).order_by(EvalQuestion.sort_no, EvalQuestion.id)
        if question_codes:
            statement = statement.where(EvalQuestion.question_code.in_(question_codes))
        if limit:
            statement = statement.limit(max(1, limit))
        return list((await session.execute(statement)).scalars().all())


async def _next_run_code(session) -> str:
    stamp = datetime.now().strftime("%Y%m%d")
    count = await session.scalar(
        select(func.count()).select_from(EvalRun).where(EvalRun.run_code.like(f"run_{stamp}_%"))
    ) or 0
    return f"run_{stamp}_{count + 1:02d}"


async def start_run(
    user_id: int,
    *,
    impl: str = "auto",
    name: str | None = None,
    question_codes: list[str] | None = None,
    limit: int | None = None,
    include_narrative: bool = True,
) -> dict[str, Any]:
    """发起评测批次（PRD 9.1 `POST /eval/runs`）：建批次行 → 起后台任务 → 立即返回批次信息。

    120 题 × 完整链路远超单次 HTTP 超时，因此**异步执行 + 轮询**（PRD 9.1 无 SSE 进度事件）。
    """
    settings = get_settings()
    # 与厂商无关：只看"有没有配置真实模型 Key"（LLM_API_KEY，兼容 DASHSCOPE_API_KEY）
    configured = settings.llm_configured
    mode = impl
    if mode not in ("auto", "real", "selfcheck"):
        raise validation_error("impl 只能是 auto / real / selfcheck")
    if mode == "auto":
        mode = "real" if configured else "selfcheck"
    if mode == "real" and not configured:
        raise validation_error(
            "未配置模型 API Key（LLM_API_KEY），无法发起真实模式评测",
            hint="如需先验证评测链路，可显式传 impl=\"selfcheck\"（gold 回放模式，不消耗 token）",
        )

    questions = await _load_question_rows(question_codes, limit)
    if not questions:
        raise validation_error(
            "题库为空：请先构建评测题库",
            hint="执行 `python 01.code/scripts/04_build_eval_set.py` 生成并入库 120 题题库",
        )

    model_name = settings.llm_model_main if mode == "real" else "scripted"
    async with control_session() as session:
        run_code = await _next_run_code(session)
        run = EvalRun(
            run_code=run_code, user_id=user_id, name=name, model_name=model_name,
            temperature=0, prompt_version=prompts.PROMPT_VERSION,
            schema_version=prompts.CALIBER_VERSION, question_total=len(questions),
            status="pending", created_at=_now(),
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        await session.commit()

    task = asyncio.create_task(
        _run_worker(
            run_id, user_id, run_code, mode, [item.question_code for item in questions],
            include_narrative,
        )
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    logger.info("评测批次已创建：%s 模式=%s 题量=%d", run_code, mode, len(questions))
    return {
        "run_code": run_code, "status": "pending", "impl": mode, "model_name": model_name,
        "question_total": len(questions),
    }


async def _set_run(run_id: int, **fields: Any) -> None:
    async with control_session() as session:
        run = await session.get(EvalRun, run_id)
        if run is None:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        await session.commit()


async def _create_eval_conversation(user_id: int, run_code: str) -> str:
    """为批次建专用会话（题目按各自报表切活跃报表，会话只承载 slot 继承与留痕）。

    编号刻意用 **`c_eval_` 前缀**：用户会话编号规则是 `c_YYYYMMDD_NNNN`，若评测会话也叫
    `c_20260916_eval_01`，会被 `_next_conversation_code` 的"取当日最大序号"当成候选
    （字典序 `e` > `0`），进而造成后续建会话取号重复——**评测不得干扰用户编号空间**。
    """
    async with control_session() as session:
        stamp = datetime.now()
        base = f"{EVAL_CONVERSATION_PREFIX}{stamp:%Y%m%d}_{run_code.split('_')[-1]}"
        code = base
        suffix = 1
        while await session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.conversation_code == code)
        ):
            suffix += 1
            code = f"{base}_{suffix}"
        conversation = Conversation(
            conversation_code=code, user_id=user_id, title=f"评测批次 {run_code}",
            slot_json=None, message_count=0, is_pinned=0, created_at=_now(), updated_at=_now(),
        )
        session.add(conversation)
        await session.commit()
    return code


async def _report_code_map(user_id: int) -> dict[str, str]:
    """物理表名 → 报表编号（评测按 `report_table` 激活活跃报表）。

    覆盖**预置报表 + 该用户自己上传的报表**：题库默认打预置表，但评测上传表同样是合法场景，
    否则新会话没有活跃报表会导致整批失败。他人的上传表不进入映射（归属隔离）。
    """
    from sqlalchemy import or_

    async with control_session() as session:
        rows = (
            await session.execute(
                select(UploadedReport.table_name, UploadedReport.report_code).where(
                    UploadedReport.status == "ready",
                    or_(UploadedReport.is_preset == 1, UploadedReport.user_id == user_id),
                )
            )
        ).all()
    return {table: code for table, code in rows}


def _gold_narrative(question: EvalQuestion) -> str:
    """自检模式的"标准解读"：由该题 key_numbers 拼一句中文（让命中率检查也有意义）。"""
    keys = json.loads(question.key_numbers_json or "[]")
    parts: list[str] = []
    for key in keys:
        value = key.get("value")
        unit = key.get("unit") or ""
        if isinstance(value, str):
            parts.append(f"{key.get('label')}为 {value}{unit}")
        else:
            parts.append(f"{key.get('label')}为 {value:g} {unit}".strip())
    return "；".join(parts) + "。" if parts else "自检：已按标准 SQL 复现结果。"


async def _run_question(
    user_id: int,
    conversation_code: str,
    question: EvalQuestion,
    report_codes: dict[str, str],
    include_narrative: bool,
    mode: str = "real",
) -> dict[str, Any]:
    """跑一题（多轮题顺序提问、只看最后一轮）并判分。

    `mode=selfcheck` 时**按题注入 gold 回放模型**（`{"route": "sql", "sql": gold_sql}` +
    由 key_numbers 拼的解读）——自检的定位是"一条命令验证 题库→链路→判分→指标→报告 全通"，
    预期正确率/命中率 ≈100%，**任何偏差都说明链路坏了**；它不代表模型能力（真实能力看 `real` 模式）。
    """
    rounds = [line.strip() for line in (question.question or "").splitlines() if line.strip()]
    report_code = report_codes.get(question.report_table)
    if mode == "selfcheck":
        set_llm(
            ScriptedLLM(
                rules={
                    "gen_sql": {
                        "route": "sql", "sql": question.gold_sql, "reason": "selfcheck-gold",
                    },
                    "narrative": {
                        "text": _gold_narrative(question), "key_numbers": [], "anomalies": [],
                    },
                }
            )
        )
    started = time.perf_counter()
    answer = None
    error_code: str | None = None
    rounds_asked = 0
    analysis_ids: list[int] = []
    for text in rounds:
        rounds_asked += 1
        # **首轮显式不继承**（`slot_base={}`，v1.19.6 修复的评测工具缺陷）：整批共用同一个会话做留痕，
        # 若首轮也继承会话 slot，题目之间会互相串条件 —— 实测 q051「周末和工作日…」留下的
        # `is_weekend = 1` 串进了 q052/q055，q074「用电量为 0 的天数」留下的 `consumption_kwh = 0`
        # 串进了 q109，三题因此判错（**不是模型答错，是评测工具串味**）。
        # 多轮题第 2 轮起照常继承：F4.2 的「会话内 slot 继承」仍然被测到，只是不再跨题。
        answer = await dispatch.run_once_dispatch(
            user_id, conversation_code, text,
            report_code=report_code, switch_active_report=True,
            slot_base={} if rounds_asked == 1 else None,
        )
        if answer.record_code:
            analysis_ids.append(int(answer.record_code.split("_")[-1]))
        if answer.status != "success":
            error_code = answer.error_code
            break
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    clarify_card = answer.clarify if answer else None
    gold = json.loads(question.gold_answer_json or "{}")
    generated = {
        "columns": [column.get("name") for column in (answer.columns if answer else [])],
        "rows": list(answer.rows) if answer and answer.status == "success" else [],
    }
    verdict = score_result_sets(gold, generated)
    if clarify_card:
        # **澄清轮不是"结果为空"**（v1.19.26）：它压根没走到执行——澄清轮**不落 analyses**
        # （见 `analysis_service` 的 clarify 字段注释）。此前一律记成 `empty_result` +
        # 「生成结果为空（未执行成功或没有数据）」，读报告的人会以为是"SQL 查空了"
        # （实测排查 q096 时就被这条描述带偏过一次）。如实标成 `clarify_no_result`，
        # 并把澄清卡带上（缺失哪个槽位、给了哪些选项，报告里看得到）。
        verdict = {
            "is_correct": False,
            "tier": None,
            "diff_type": "clarify_no_result",
            "detail": (
                f"本轮返回澄清卡（missing_slot={clarify_card.get('missing_slot')}）："
                f"「{str(clarify_card.get('question'))[:80]}」"
            ),
        }
    keys = json.loads(question.key_numbers_json or "[]")
    narrative = (answer.narrative if answer else "") or ""
    key_score = (
        score_key_numbers(keys, narrative) if include_narrative and keys
        else {"total": len(keys), "hit": 0, "hit_rate": None, "missed": []}
    )

    detail = {
        "question_code": question.question_code,
        "question": question.question,
        "qtype": question.qtype,
        "difficulty": question.difficulty,
        "is_trap": bool(question.is_trap),
        "trap_rule": question.trap_rule,
        "gold_sql": question.gold_sql,
        "rounds": rounds_asked,
        "status": "clarify" if clarify_card else (answer.status if answer else "failed"),
        "route": answer.route if answer else None,
        "generated": "sql" if not (answer and answer.code) else "pandas",
        "generated_sql_or_code": (answer.sql if answer and answer.sql else (answer.code if answer else None)),
        "is_correct": verdict["is_correct"],
        "matched_tier": verdict["tier"],
        "diff_type": verdict["diff_type"],
        "diff_detail": verdict["detail"],
        "clarify": clarify_card,
        "key_hit_rate": key_score["hit_rate"],
        "key_missed": key_score["missed"],
        "elapsed_ms": elapsed_ms,
        "retries": answer.retries if answer else 0,
        "path_switched": bool(answer.path_switched) if answer else False,
        "error_code": error_code,
        "analysis_ids": analysis_ids,
        "narrative": narrative[:500],
    }
    return detail


_SQL_ERROR_CODES = {"E_SQL_FORBIDDEN", "E_SQL_EXEC", "E_CODE_FORBIDDEN", "E_CODE_EXEC"}
#: 模型调用层失败（**不是** SQL 缺陷、也**不是**理解错口径）：覆盖超时、网络异常、
#: HTTP 非 2xx、模型返回空、输出不是可解析 JSON —— 全部打这一个码（见 `llm_client` 与
#: `analysis_service` 的 `E_LLM` 抛出点）。归到别的桶会把"端点抖动"记成"系统缺陷"。
_LLM_ERROR_CODE = "E_LLM"

#: 归因分类的**全集与渲染顺序**（顺序即阅读优先级：先"没跑成"、再"跑成了但不对"）。
#: ⚠️ 新增归因桶时必须同时改这里 —— 报告只渲染元组里有的键，漏写会让那一类错题在报告里
#: **静静消失**（v1.19.26 加「澄清未执行」时就漏过一次：全批次都是澄清卡时表里一片空白）。
#: 用例 `test_attribution_order_covers_every_bucket` 钉住"全集 == `_attribute()` 的值域"。
ATTRIBUTION_ORDER: tuple[str, ...] = (
    "澄清未执行",
    "模型调用失败",
    "口径理解错误",
    "SQL 语法与函数",
    "slot 继承错误",
    "执行失败未重试成功",
    "结果口径偏差",
)


def _attribute(item: dict) -> str:
    """把一道错题归入 F5.4 的一类（**启发式**，顺序即优先级，兜底"结果口径偏差"）。

    - 澄清未执行：整轮返回澄清卡（**没走到执行**，一次调用都没产生结果，v1.19.26 新增）
    - 模型调用失败：链路挂在**模型调用**这一层（`E_LLM`：超时/网络/空输出/不可解析）
    - 口径理解错误：口径陷阱题答错（R1/R3/R6/R7）
    - SQL 语法与函数：被守卫拦截或执行报错（含 pandas 路径的代码拦截/报错）
    - slot 继承错误：多轮题答错（追问继承或切换维度出错）
    - 执行失败未重试成功：链路失败或触发过重试仍错
    - 结果口径偏差：其余（链路正常但数值/列/行不一致）

    **顺序的两次修订**（都有实测依据，别随手调）：

    1. `error_code` 判在"陷阱题"之前 —— 模型**没答上来**时不存在"理解错口径"这回事，
       把它记成口径问题是给上游抖动作伪证；
    2. `E_LLM` 判在"多轮题"之前 —— 实测 q107（多轮题，`E_LLM 槽位解析超时`）曾被记成
       「slot 继承错误」，等于把超时误诊成合并规则的 bug，**这个假象在报表里挂了三天**
       （详见 `02.data/tmp/_q107_诊断与修复方案.md` §5）。
    """
    if item.get("status") == "clarify":
        # 澄清卡与"执行失败"是两回事：前者是**槽位没解析出来、流程主动停下来问用户**，
        # 归到"执行失败"会误导（实测 q096 跨模型复现两批都是这一类）。
        return "澄清未执行"
    if item.get("error_code") == _LLM_ERROR_CODE:
        return "模型调用失败"
    if item.get("is_trap"):
        return "口径理解错误"
    if item.get("error_code") in _SQL_ERROR_CODES:
        return "SQL 语法与函数"
    if item.get("qtype") == "multi_turn":
        return "slot 继承错误"
    if item.get("status") != "success" or item.get("retries", 0) > 0:
        return "执行失败未重试成功"
    return "结果口径偏差"


def _aggregate(details: list[dict]) -> dict[str, Any]:
    """汇总指标（PRD F5.3）：正确率、命中率、耗时、错误与重试分布。"""
    total = len(details)
    correct = sum(1 for item in details if item["is_correct"])
    elapsed = [item["elapsed_ms"] for item in details]
    hit_rates = [item["key_hit_rate"] for item in details if item["key_hit_rate"] is not None]

    def _rate(subset: list[dict]) -> dict[str, Any]:
        count = len(subset)
        ok = sum(1 for item in subset if item["is_correct"])
        return {"total": count, "correct": ok, "rate": round(ok / count, 4) if count else None}

    by_type = {qtype: _rate([i for i in details if i["qtype"] == qtype])
               for qtype in ("stat", "agg", "compare", "anomaly", "structure", "multi_turn")}
    by_difficulty = {level: _rate([i for i in details if i["difficulty"] == level])
                     for level in ("easy", "medium", "hard")}
    traps = [item for item in details if item["is_trap"]]
    diff_types: dict[str, int] = {}
    errors: dict[str, int] = {}
    retries: dict[str, int] = {}
    attribution: dict[str, int] = {}
    for item in details:
        if item["diff_type"]:
            diff_types[item["diff_type"]] = diff_types.get(item["diff_type"], 0) + 1
        if item["error_code"]:
            errors[item["error_code"]] = errors.get(item["error_code"], 0) + 1
        bucket = str(item["retries"])
        retries[bucket] = retries.get(bucket, 0) + 1
        if not item["is_correct"]:
            label = _attribute(item)
            attribution[label] = attribution.get(label, 0) + 1
    return {
        "attribution": attribution,
        "question_total": total,
        "exec_correct_count": correct,
        "exec_correct_rate": round(correct / total, 4) if total else None,
        "narrative_hit_rate": round(sum(hit_rates) / len(hit_rates), 4) if hit_rates else None,
        "avg_elapsed_ms": int(sum(elapsed) / total) if total else None,
        "p95_elapsed_ms": _percentile(elapsed, 0.95),
        "retry_total": sum(item["retries"] for item in details),
        "by_type": by_type,
        "by_difficulty": by_difficulty,
        "trap": {**_rate(traps), "rules": sorted({i["trap_rule"] for i in traps if i["trap_rule"]})},
        "diff_types": diff_types,
        "errors": errors,
        "retry_distribution": retries,
        "tiers": {
            str(tier): sum(1 for item in details if item["matched_tier"] == tier)
            for tier in (1, 2, 3)
        },
    }


async def _link_llm_calls(run_id: int, analysis_ids: list[int]) -> None:
    """把逐题分析产生的 llm_calls 关联到批次（`eval_run_id`，DDL 要求）。"""
    if not analysis_ids:
        return
    async with control_session() as session:
        calls = (
            await session.execute(select(LLMCall).where(LLMCall.analysis_id.in_(analysis_ids)))
        ).scalars().all()
        for call in calls:
            call.eval_run_id = run_id
        await session.commit()


async def _run_worker(
    run_id: int, user_id: int, run_code: str, mode: str,
    question_codes: list[str], include_narrative: bool,
) -> None:
    """后台批次任务：逐题执行 → 汇总 → 落库 → 出报告。任何异常都落 `status='failed'`。

    自检模式会**逐题注入回放模型**（全局 LLM 客户端）→ 两个自检批次不要并发跑；
    真实模式不注入，可并发。
    """
    # 这里必须用 `get_llm()` **先把 `_CLIENT` 初始化好**再取引用。
    # 历史实现直接读 `llm_client._CLIENT`，理由写在下面（原文保留）：
    #   "后者在未配置 Key 时会'顺手创建一个脚本模型'作为副作用，批次结束后把它还原成'曾经存在的
    #    客户端'会污染调用方的状态" —— 这个顾虑是**多余的**：未配置 Key 时 `ScriptedLLM` 本就是
    #   正确默认，恢复它不算污染。
    # 而它反而造成了更严重的缺陷（2026-10-16 真模型评测踩到、2026-10-19 修）：**冷启动时
    # `_CLIENT is None`** ⇒ `finally: if previous_llm is not None` **跳过恢复** ⇒ 逐题注入的
    # `ScriptedLLM` 残留为全局客户端；此后同进程的 `impl=real` 批次**静默**变成脚本回放
    # （`eval_runs.model_name` 仍写真实模型名，只有 `llm_calls.model_name` 露馅 —— 不花钱但读数无意义）。
    # 回归用例：`tests/test_eval.py::test_selfcheck_restores_llm_client_on_cold_start`。
    previous_llm = get_llm() if mode == "selfcheck" else None
    try:
        await _set_run(run_id, status="running", started_at=_now())
        questions = await _load_question_rows(question_codes, None)
        report_codes = await _report_code_map(user_id)
        conversation_code = await _create_eval_conversation(user_id, run_code)

        details: list[dict] = []
        for question in questions:
            try:
                details.append(
                    await _run_question(
                        user_id, conversation_code, question, report_codes, include_narrative, mode
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 单题异常不终止整批
                logger.exception("评测题执行异常：%s", question.question_code)
                details.append({
                    "question_code": question.question_code, "question": question.question,
                    "qtype": question.qtype, "difficulty": question.difficulty,
                    "is_trap": bool(question.is_trap), "trap_rule": question.trap_rule,
                    "rounds": 0, "status": "failed", "route": None, "generated": None,
                    "generated_sql_or_code": None, "is_correct": False, "matched_tier": None,
                    "diff_type": "runner_error", "diff_detail": str(exc)[:300],
                    "key_hit_rate": None, "key_missed": [], "elapsed_ms": 0, "retries": 0,
                    "path_switched": False, "error_code": "E_EVAL_RUNNER",
                    "analysis_ids": [], "narrative": "",
                })

        metrics = _aggregate(details)
        analysis_ids = [aid for item in details for aid in item.get("analysis_ids", [])]
        await _link_llm_calls(run_id, analysis_ids)
        report_path = _write_report(run_code, mode, metrics, details)
        async with control_session() as session:
            run = await session.get(EvalRun, run_id)
            if run is not None:
                run.status = "finished"
                run.exec_correct_count = metrics["exec_correct_count"]
                run.exec_correct_rate = metrics["exec_correct_rate"]
                run.narrative_hit_rate = metrics["narrative_hit_rate"]
                run.avg_elapsed_ms = metrics["avg_elapsed_ms"]
                run.p95_elapsed_ms = metrics["p95_elapsed_ms"]
                run.retry_total = metrics["retry_total"]
                run.result_json = json.dumps(
                    {"impl": mode, "metrics": metrics, "questions": details}, ensure_ascii=False
                )
                run.report_path = report_path
                run.finished_at = _now()
                await session.commit()
        logger.info(
            "评测批次完成：%s 正确率=%s 命中率=%s", run_code,
            metrics["exec_correct_rate"], metrics["narrative_hit_rate"],
        )
    except Exception as exc:  # noqa: BLE001 —— 批次级失败也要留痕
        logger.exception("评测批次失败：%s", run_code)
        await _set_run(run_id, status="failed", error_message=str(exc)[:500], finished_at=_now())
    finally:
        if previous_llm is not None:
            set_llm(previous_llm)


# ══════════════════════════════════════════════════════════════════════════
#  ③ 报告与查询
# ══════════════════════════════════════════════════════════════════════════

def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _rate_table(title: str, data: dict[str, dict]) -> list[str]:
    lines = [f"### {title}", "", "| 分组 | 题量 | 正确 | 正确率 |", "| --- | --- | --- | --- |"]
    for key, item in data.items():
        lines.append(f"| {key} | {item['total']} | {item['correct']} | {_pct(item['rate'])} |")
    lines.append("")
    return lines


def _write_report(
    run_code: str, mode: str, metrics: dict[str, Any], details: list[dict]
) -> str:
    """生成 Markdown 评测报告（PRD F5.3），返回**相对项目根**的路径（入库口径）。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{run_code}.md"
    lines: list[str] = [f"# 评测报告 · {run_code}", ""]
    if mode == "selfcheck":
        lines += [
            "> ⚠️ **自检模式（impl=selfcheck）：逐题按题库 `gold_sql` 回放 + 由 `key_numbers` 生成的"
            "标准解读，用于验证「题库 → 链路 → 判分 → 指标 → 报告」全通（**正常应≈100%**）。"
            "本报告的正确率/命中率**反映的是评测链路自洽性，不代表模型能力**，请勿作为验收数据引用；"
            "真实评测请配置 `LLM_API_KEY`（+ `LLM_BASE_URL` / `LLM_MODEL_MAIN`）后以 `impl=real` 重跑。**",
            "",
        ]
    lines += [
        f"- 生成时间：{_now()}",
        f"- 执行模式：`{'自检（scripted）' if mode == 'selfcheck' else '真实模型'}`",
        f"- 题量：{metrics['question_total']}；执行正确率：**{_pct(metrics['exec_correct_rate'])}**"
        f"（{metrics['exec_correct_count']}/{metrics['question_total']}，目标 ≥85%）",
        f"- 结论命中率：**{_pct(metrics['narrative_hit_rate'])}**（目标 ≥90%）",
        f"- 平均耗时：{metrics['avg_elapsed_ms']} ms；P95 耗时：{metrics['p95_elapsed_ms']} ms"
        "（目标 ≤8000 ms）",
        f"- 重试总次数：{metrics['retry_total']}；首字延迟：未采集（客户端为单次 POST，非流式）",
        "",
        "## 一、分类正确率",
        "",
    ]
    lines += _rate_table("按题型", metrics["by_type"])
    lines += _rate_table("按难度", metrics["by_difficulty"])
    trap = metrics["trap"]
    lines += [
        "### 按陷阱题（口径红线）", "",
        "| 分组 | 题量 | 正确 | 正确率 | 覆盖红线 |",
        "| --- | --- | --- | --- | --- |",
        f"| 陷阱题 | {trap['total']} | {trap['correct']} | {_pct(trap['rate'])} | "
        f"{'、'.join(trap['rules']) or '—'} |",
        "",
        "## 二、差异与失败分布",
        "",
        "| 差异类型 | 次数 |",
        "| --- | --- |",
    ]
    for key, count in sorted(metrics["diff_types"].items(), key=lambda item: -item[1]):
        lines.append(f"| {key} | {count} |")
    if not metrics["diff_types"]:
        lines.append("| （无） | 0 |")
    lines += ["", "| 错误码 | 次数 |", "| --- | --- |"]
    for key, count in sorted(metrics["errors"].items(), key=lambda item: -item[1]):
        lines.append(f"| {key} | {count} |")
    if not metrics["errors"]:
        lines.append("| （无） | 0 |")
    attribution = metrics.get("attribution") or {}
    lines += [
        "",
        "### bad case 归因（启发式，对应 UIUX 5.4 的归因卡片）",
        "",
        "| 归因分类 | 错题数 |",
        "| --- | --- |",
    ]
    # 顺序与全集都由 `ATTRIBUTION_ORDER` 单一维护（新增桶时不用再改这里）
    for key in ATTRIBUTION_ORDER:
        if attribution.get(key):
            lines.append(f"| {key} | {attribution[key]} |")
    if not attribution:
        lines.append("| （本批次无错题） | 0 |")
    tiers = metrics["tiers"]
    lines += [
        "",
        f"> 判分档命中分布：第一档（列位置）{tiers.get('1', 0)} 题、"
        f"第二档（列名归一）{tiers.get('2', 0)} 题、第三档（单列数值集合）{tiers.get('3', 0)} 题；",
        f"> 重试次数分布：{metrics['retry_distribution']}",
        "",
        "## 三、逐题明细",
        "",
        "| 题号 | 题型 | 难度 | 陷阱 | 结果 | 判分档 | 差异类型 | 耗时(ms) | 重试 | 错误码 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in details:
        lines.append(
            f"| {item['question_code']} | {item['qtype']} | {item['difficulty']} | "
            f"{item['trap_rule'] or '—'} | {'✅' if item['is_correct'] else '❌'} | "
            f"{item['matched_tier'] or '—'} | {item['diff_type'] or '—'} | "
            f"{item['elapsed_ms']} | {item['retries']} | {item['error_code'] or '—'} |"
        )
    lines += ["", "## 四、错题复盘（生成 vs 标准）", ""]
    wrong = [item for item in details if not item["is_correct"]]
    if not wrong:
        lines.append("（本批次无错题）")
    for item in wrong[:40]:
        lines += [
            f"### {item['question_code']} · {item['question']}",
            "",
            f"- 标准 SQL：`{(item.get('gold_sql') or '').strip() or '（见题库）'}`",
            f"- 生成 SQL/代码：`{(item['generated_sql_or_code'] or '').strip()[:400] or '—'}`",
            f"- 差异：{item['diff_detail']}",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")
    # 入库口径：项目根内落**相对路径**（便于整体迁移）；自定义目录（测试/外部挂载）落绝对路径
    try:
        return str(path.relative_to(DATA_DIR.parent))
    except ValueError:
        return str(path)


async def _owned_run(session, user_id: int, run_code: str) -> EvalRun:
    run = await session.scalar(select(EvalRun).where(EvalRun.run_code == run_code))
    if run is None or run.user_id != user_id:
        raise not_found(f"评测批次不存在：{run_code}")
    return run


def _run_payload(run: EvalRun) -> dict[str, Any]:
    return {
        "run_code": run.run_code,
        "name": run.name,
        "model_name": run.model_name,
        "temperature": run.temperature,
        "prompt_version": run.prompt_version,
        "schema_version": run.schema_version,
        "question_total": run.question_total,
        "exec_correct_count": run.exec_correct_count,
        "exec_correct_rate": run.exec_correct_rate,
        "narrative_hit_rate": run.narrative_hit_rate,
        "avg_elapsed_ms": run.avg_elapsed_ms,
        "p95_elapsed_ms": run.p95_elapsed_ms,
        "first_token_avg_ms": run.first_token_avg_ms,
        "retry_total": run.retry_total,
        "status": run.status,
        "error_message": run.error_message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_at": run.created_at,
        "report_path": run.report_path,
    }


async def list_runs(user_id: int, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """批次列表（PRD 9.1 `GET /eval/runs`），按创建时间倒序，仅本人可见。"""
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    async with control_session() as session:
        total = await session.scalar(
            select(func.count()).select_from(EvalRun).where(EvalRun.user_id == user_id)
        ) or 0
        rows = (
            await session.execute(
                select(EvalRun).where(EvalRun.user_id == user_id)
                .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
                .offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_run_payload(row) for row in rows]}


async def get_run(user_id: int, run_code: str, *, include_questions: bool = True) -> dict[str, Any]:
    """批次详情（PRD 9.1 `GET /eval/runs/{run_code}`）：指标 + 逐题明细。"""
    async with control_session() as session:
        run = await _owned_run(session, user_id, run_code)
        payload = _run_payload(run)
        stored = json.loads(run.result_json) if run.result_json else None
    if stored:
        payload["impl"] = stored.get("impl")
        payload["metrics"] = stored.get("metrics")
        if include_questions:
            payload["questions"] = stored.get("questions") or []
    return payload


async def get_report_markdown(user_id: int, run_code: str) -> tuple[str, str]:
    """评测报告（PRD 9.1 `GET /eval/runs/{run_code}/report`）：返回 `(文件名, Markdown 文本)`。"""
    async with control_session() as session:
        run = await _owned_run(session, user_id, run_code)
        if run.status != "finished":
            raise validation_error(f"批次尚未完成（当前状态：{run.status}），暂无评测报告")
        report_path = run.report_path
    if not report_path:
        raise not_found("该批次没有报告文件（可能生成失败）")
    stored = Path(report_path)
    path = stored if stored.is_absolute() else DATA_DIR.parent / stored
    if not path.exists():
        raise not_found(f"报告文件不存在：{report_path}")
    return f"{run_code}.md", path.read_text(encoding="utf-8")
