"""智电析报 · 对话分析服务（交付物 4/5：单轮问答闭环 + 图表 + 双工具沙箱 + 自愈重试）。

链路（PRD 3.3）：

    slot 抽取（轻量模型）→ Schema 注入 → 生成（主力模型自报 route：sql / pandas）
    → SQL：sql_guard 守卫 → 只读执行（`mode=ro`）
      pandas：code_guard AST 预检 → 子进程沙箱（`sandbox_service`）
    → 表格快照 → **图表生成（本地计算，不调 LLM）** → 文字解读（主力模型）→ 全量留痕

F2.4 自愈重试（交付物 5 定案）：执行失败 → 带错误重写一次（SQL 重写可改报 pandas）；
SQL 执行失败仍不行 → **换路 pandas**（`path_switched=1`）；**安全拦截不换路**（重写一次即止，
不给恶意请求多一次生成机会）；额度用尽仍失败 → `status='failed'`，`retries` 留痕。

三条设计红线（对应 PRD S1 / P4 / 数据库设计 S1）：

1. **生成的 SQL 永不直接执行**：先过 `sql_guard`（单条 SELECT + 表名白名单 + 危险函数 +
   R3 混加 + 强制 LIMIT），拦截时写 `audit_logs(sql_blocked)` 并落 `analyses.status='failed'`；
2. **执行只走只读通道**（SQL：`report_ro_conn` `mode=ro`；pandas：子进程内 `mode=ro` 连接），
   与守卫/预检形成两道独立防线；
3. **失败不静默、不编造**：任何失败都落库留痕（`status='failed'` + `error_code`），
   助手消息为固定文案，**不写 narrative / key_numbers**。

事件序列契约（PRD 9.2）：成功 `status→slot→code→status→table→chart→narrative→meta→done`；
失败 `status→[slot→code…]→error→done`（**error 之后仍发 done**，前端据此停止 loading 并拿到
`record_id` 跳详情）。**会话不存在/不属于本人**时无处留痕，直接抛 `BizError`（由端点发 error 事件）。

图表环节的两条约定（交付物 4 第三批决策）：

- **`chart` 事件位置固定**在 `table` 之后、`narrative` 之前（UIUX 8.1 的 ≤7s 卡点）；
- **图表失败绝不阻断主流程**（决策 ⑧）：画不出来时仍发 `chart` 事件，但 `figure=null` +
  `fallback="table"` + `reason`，`status` 仍为 `success`、解读照常生成——图表是增值项而非前置依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import prompts
from app.core import sql_guard
from app.core.config import get_settings
from app.core.errors import (
    BizError,
    E_CODE_FORBIDDEN,
    E_LLM,
    E_OUT_OF_SCOPE,
    E_SQL_EXEC,
    E_SQL_FORBIDDEN,
    E_VALIDATION,
    not_found,
    validation_error,
)
from app.core.llm_client import LLMReply, get_llm
from app.db.models import Analysis, Conversation, LLMCall, Message, UploadedReport
from app.db.session import control_session, report_ro_conn
from app.services import (
    audit_service,
    chart_service,
    conversation_service,
    sandbox_service,
    schema_service,
    slot_service,
)

logger = logging.getLogger("zdxb.analysis")

__all__ = ["AnalysisResult", "EventSink", "run_once"]

#: 落库的结果快照上限（PRD：`result_rows_json` ≤200KB）
RESULT_SNAPSHOT_LIMIT = 200 * 1024

#: 失败时写入的助手消息文案（P4：明确声明未生成结果）
FAILURE_ASSISTANT_TEXT = "本次未生成结果，系统不会给出推测数据。可调整提问或稍后重试。"

#: 分析记录编号前缀（值形如 `a_000123`）
RECORD_CODE_PREFIX = "a_"

#: slot 阶段超时兜底（v1.19.11 延迟专项）：超过该秒数未返回 → 按**规则**兜底继续，不干等长尾。
#: 依据：实测 slot 最长 21s、P95 的主要来源就是这类长尾；而"指标 / 时间"多数能靠词面规则拿到。
#:
#: **v1.19.20 由 6s 提到 12s**：6s 是按当时的主力档（deepseek-v4-flash，单次 slot ≈1.5–2.5s）定的；
#: 换到更慢的档位（kimi-k3 实测单次 3–5s）后，**6s 频繁触发**，而兜底规则只能覆盖"指标能从词面
#: 认出"的问法——实测 5 题（q052/q083/q107/q111/q114）因此被直接判失败（错误码 `E_LLM`），
#: 而它们本是**能答对的**。超时是**上限而非固定延迟**：调高只影响极少数长尾，正常问法不受影响。
#: 2026-10-14 由 12.0 提到 **20.0**（问题 `P-038`）：慢时段里的 slot 调用常在 12–20 s，
#: 撞上限后要么走规则兜底（"认不出指标"就整题失败）、要么直接失败；
#: 实测同批超时题**重跑 18/19 通过** ⇒ 上限宜留余量，别让"端点慢"变成"题丢了"。
SLOT_TIMEOUT_SECONDS = 20.0

#: 解读阶段超时（v1.19.11）：解读是**增值项**，超时就跳过（表格与图表照给、`status` 仍 success），
#: 不让"解说词"把整轮拖失败或拖到十几秒（实测 narrative 最长 17s）。
#: 2026-09-22：可被 `.env` 的 `NARRATIVE_TIMEOUT_SECONDS` 覆盖（**默认仍是 8.0，产品行为不变**），
#: 用于"强制思考的慢模型 8s 下解读必被取消 ⇒ 评测命中率测不出来"这种一次性测量场景。
#: **读写都必须在调用时进行**：模块 import 期不能碰 `get_settings()` —— 单测夹具在那之前才设环境变量，
#: 提前读会把配置缓存成生产值，实测连锁失败 35 个用例（2026-09-22 踩到并回退）。
NARRATIVE_TIMEOUT_SECONDS = 8.0

#: 解读超时的默认值（配置未提供时使用；也是判断"常量是否被单测覆写"的基准）
_NARRATIVE_TIMEOUT_DEFAULT = 8.0


def _narrative_timeout() -> float:
    """解读阶段超时秒数：`.env` 配置优先，未配置回落 8.0。

    模块级 `NARRATIVE_TIMEOUT_SECONDS` 被改成**非默认值**时视为单测 monkeypatch 覆写，
    直接采信 —— 保住 `test_narrative_timeout_is_not_fatal` 的既有语义。
    """
    if NARRATIVE_TIMEOUT_SECONDS != _NARRATIVE_TIMEOUT_DEFAULT:
        return NARRATIVE_TIMEOUT_SECONDS
    configured = get_settings().narrative_timeout_seconds
    return float(configured) if configured else _NARRATIVE_TIMEOUT_DEFAULT


#: 生成超时的默认值（用途同 `_NARRATIVE_TIMEOUT_DEFAULT`）
_GENERATE_TIMEOUT_DEFAULT = 25.0


def _generate_timeout() -> float:
    """生成阶段**软超时**秒数（`.env` 的 `GENERATE_TIMEOUT_SECONDS` 优先，默认 25.0）。"""
    configured = getattr(get_settings(), "generate_timeout_seconds", None)
    return float(configured) if configured else _GENERATE_TIMEOUT_DEFAULT


def _rule_narrative(table: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
    """**规则化解读（自动摘要）**：LLM 解读超时/解析失败时的兜底 —— 只依据结果表算出的结论。

    为什么必须有它（2026-10-14 需求方明确）：本项目的交付物就是"给用户**分析结果与结论**"，
    一条没有解读的记录等于半成品；而供应商在慢时段的一次解读会超过
    `NARRATIVE_TIMEOUT_SECONDS`（实测同一端点同一提示词：快时段 5.1s、慢时段 >8s 且 3/3 被跳过）。

    口径（与 P4「失败不静默、不编造」一致）：
    · **只用结果表里的数字**（行数 / 首末 / 极值 / 占比），不做表外推断、不引用上传阶段元信息；
    · 正文以「**自动摘要**」开头、末尾注明生成方式 —— 读者一眼能分辨这不是模型写的；
    · `key_numbers` 与正文一致，供"结论命中率"统计与人工核对。
    """
    from app.services import chart_service  # 局部导入避免环（沿用 slot_service 的做法）

    def _is_num(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _unit_of(name: str) -> str:
        if chart_service._is_ratio(name):  # noqa: SLF001 —— 比率判定只有这一处权威
            return "%"
        for suffix, unit in (("_kwh", "kWh"), ("_kw", "kW")):
            if name.endswith(suffix):
                return unit
        return ""

    def _label_of(name: str) -> str:
        return chart_service._label_of(name, None)  # noqa: SLF001

    def _show(value: float, unit: str) -> str:
        # 比率列在库里是小数（0.0621），展示与"关键数字"都换算成百分比 —— 与人工核对口径一致
        scaled = value * 100 if unit == "%" else value
        text_ = chart_service._fmt_number(scaled)  # noqa: SLF001
        return f"{text_}{unit}" if unit else text_

    columns = [
        column.get("name") for column in (table.get("columns") or []) if isinstance(column, dict)
    ]
    rows = [row for row in (table.get("rows") or []) if isinstance(row, dict)]
    row_count = int(table.get("row_count") or len(rows))
    numeric = [
        name
        for name in columns
        if any(_is_num(row.get(name)) for row in rows)
    ]
    dims = [name for name in columns if name not in numeric]

    numbers: list[dict[str, Any]] = []
    parts: list[str] = []
    if not rows:
        parts.append(f"本次查询未返回数据（共 {row_count} 行）")
    elif len(rows) == 1:
        items: list[str] = []
        for name in numeric[:4]:
            value = rows[0].get(name)
            if not _is_num(value):
                continue
            unit = _unit_of(name)
            items.append(f"{_label_of(name)} {_show(float(value), unit)}")
            scaled = round(float(value) * 100 if unit == "%" else float(value), 4)
            numbers.append({"label": _label_of(name), "value": scaled, "unit": unit})
        parts.append("、".join(items) if items else "本次查询返回 1 行结果")
    else:
        for name in numeric[:2]:
            values = [(float(row[name]), row) for row in rows if _is_num(row.get(name))]
            if not values:
                continue
            unit = _unit_of(name)
            label = _label_of(name)
            ident = dims[0] if dims else None

            def _ident_of(row: dict[str, Any]) -> str:
                value = row.get(ident) if ident else None
                return f"（{value}）" if value not in (None, "") else ""

            first_value, first_row = values[0]
            last_value, last_row = values[-1]
            max_value, max_row = max(values, key=lambda item: item[0])
            min_value, min_row = min(values, key=lambda item: item[0])
            parts.append(
                f"{label} 共 {len(values)} 个点：{_show(first_value, unit)}{_ident_of(first_row)}"
                f" → {_show(last_value, unit)}{_ident_of(last_row)}，"
                f"最高 {_show(max_value, unit)}{_ident_of(max_row)}，"
                f"最低 {_show(min_value, unit)}{_ident_of(min_row)}"
            )
            scale = 100 if unit == "%" else 1
            numbers.extend([
                {"label": label, "value": round(max_value * scale, 4), "unit": unit},
                {"label": label, "value": round(min_value * scale, 4), "unit": unit},
            ])

    tail = f"（{reason}；以上由结果表自动生成，仅供参考）" if reason else "（以上由结果表自动生成，仅供参考）"
    if table.get("truncated"):
        tail = tail[:-1] + "；结果已截断，仅展示前若干行）"
    text = "自动摘要：" + "；".join(parts) + "。" + tail if parts else f"自动摘要：本次查询未返回数据。{tail}"
    return {"text": text, "key_numbers": numbers, "anomalies": [], "auto": True}


def _narrative_model(llm: Any, settings: Any) -> str | None:
    """解读阶段用哪个模型：`LLM_MODEL_NARRATIVE` 优先，未配置回落轻量档；不支持分档则不传。

    单独一档的理由见 `Settings.llm_model_narrative` 的注释（避免"换解读模型=换 slot 模型"）。
    """
    if not getattr(llm, "supports_model_tiers", False):
        return None
    configured = str(getattr(settings, "llm_model_narrative", "") or "").strip()
    return configured or getattr(settings, "llm_model_light", None)


def _looks_like_generation(text_: str) -> bool:
    """生成结果是否**可用**：解析得动且 `sql` / `code` 至少有一个非空。

    用于 `_chat_generate` 的"不完整就重发"判定（2026-10-14 补，实测 q119/q110）：
    模型偶发返回 `{"route": "sql", "reason": "..."}` 这种**缺 sql/code 的半成品**——
    它不含任何可执行语义，与"空输出"等价；不重发则整题以空结果收场。
    """
    try:
        payload = _safe_json(text_, stage="gen_sql")
    except Exception:  # noqa: BLE001 —— 解析失败同样视为"不可用"，交给重发
        return False
    return bool(str(payload.get("sql") or "").strip() or str(payload.get("code") or "").strip())


async def _chat_generate(
    llm: Any,
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    validate: Any = None,
) -> list[Any]:
    """生成阶段调用：**软超时（超时也重发一次）+ 空/半成品重发一次**，返回"实际发生的调用序列"（1–2 次）。

    三条依据都来自真实实测（见问题 `P-032` 与 `大修工作进度/提速实验-20261014.md`）：

    ① **空输出重发**：该端点会偶发"思考吃光输出额度 → `content` 为空"（`E_LLM 模型输出为空`）。
       空输出**不含任何语义**，重发同样的请求既安全又能救回整题；不重发则整题以 E_LLM 失败，
       并曾**连带触发** `P-033`（失败兜底里的事件推送抛 `get_config outside of a runnable context`）。
    ② **半成品重发**：解析得动但 `sql` / `code` 都为空（实测 q119/q110），同上处理。
    ③ **超时也重发（C1，2026-10-16）**：同端点出现过单次 **53.9 s** 的长尾。慢时段实测有整题
       卡死在 gen 上撞满超时（该轮 `llm_calls` 只留 `[slot]` 一行，gen 那次直接消失）。
       超时多为"这一枪慢"而非"命中拥塞"，重发一次的代价（1 次 gen 调用）远小于整题失败，
       故**首次超时不再直接判死**：重发一次，两次都超时才按失败如实上报。
       注意**最坏耗时 = 2 × timeout**。

    返回**列表**是刻意的：成功调用都真实计费，`llm_calls` 必须逐次留痕（`is_retry` 用下标）。
    **超时的那一次没有回复对象，无法落 `llm_calls`**（`model_name` 非空约束）——与 slot 超时同口径，
    只在日志留一条 warning；此时被救回的那次重发在下标 0，`is_retry` 记 0（库里没有更早的行可供它"重试"）。
    """
    timeout = _generate_timeout()

    async def _once() -> Any:
        return await asyncio.wait_for(
            llm.chat(messages, purpose="gen_sql", model=model, json_mode=True), timeout=timeout
        )

    replies: list[Any] = []
    # ① 首次调用：**超时不再直接判死**（C1）→ 落到下面的统一重发出口，只记一条 warning
    try:
        first = await _once()
    except TimeoutError:
        logger.warning(
            "生成首次调用超时（>%.0fs，端点长尾，见 P-032）→ 重发一次同样的请求", timeout
        )
    else:
        replies.append(first)
        text_ = (getattr(first, "text", "") or "").strip()
        # 两种情况都值得重发一次：**空输出**（思考吃光额度）与**半成品**（解析得动但没有 sql/code）。
        # 二者都不含可执行语义 → 重发同样的请求既安全又能救回整题（两次调用逐次留痕、逐次计费）。
        unusable = not text_ or (validate is not None and not validate(text_))
        if not unusable:
            return replies
        logger.warning(
            "生成结果不可用（%s，端点偶发，见 P-032）→ 重发一次同样的请求",
            "空输出" if not text_ else "缺 sql/code",
        )
    # ② 第二次调用（重发）：首次超时、空输出、半成品三条路在此汇合；这次再超时才如实失败
    try:
        replies.append(await _once())
    except TimeoutError:
        raise BizError(
            500, E_LLM, f"生成超时（>{timeout:.0f}s，已重发一次）", {"stage": "gen_sql"},
            hint="模型服务较慢或拥塞，请稍后重试",
        ) from None
    return replies


#: 事件推送回调（**同步非阻塞**）：端点注入 `asyncio.Queue.put_nowait` 之类的实现
EventPush = Callable[[dict[str, Any]], None]


class EventSink(list):
    """SSE 事件集合：`append` 时**同步**推给可选回调（真流式，v1.19.11）。

    **为什么做成 list 子类**：链路上有十几处 `result.events.append(...)`，逐个改成 `await emit(...)`
    侵入面太大、也容易漏；覆盖 `append` 后**原有代码一行不用动**，事件一产生就推给端点。

    回调必须是**同步非阻塞**的（端点实现是 `asyncio.Queue.put_nowait`）——阻塞会把整条链路拖慢。
    """

    def __init__(self, push: EventPush | None = None) -> None:
        super().__init__()
        self._push = push

    def append(self, item: dict[str, Any]) -> None:
        super().append(item)
        if self._push is not None:
            self._push(item)


@dataclass
class AnalysisResult:
    """一次问答的完整结果（端点**边产生边推送** `events`，也保留全量供落库/回放）。"""

    record_code: str
    conversation_code: str
    report_code: str
    table_name: str
    question: str
    status: str
    route: str = "sql"
    slot: dict[str, Any] = field(default_factory=dict)
    sql: str | None = None
    #: pandas 路径生成的代码（route=pandas 时落 `analyses.code_text`）
    code: str | None = None
    #: F2.4 自愈重试统计（不含首次生成；换路 = SQL 失败后改走 pandas）
    retries: int = 0
    path_switched: bool = False
    #: 最后一次被守卫拦截的 rule_id（error 事件 detail 用）
    last_rule: str | None = None
    columns: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    #: 是否真的执行出过结果表（守卫拦截/生成失败时为 False → 快照列写 NULL 而非 `[]`）
    table_ready: bool = False
    #: 图表 figure（降级时为 None；落库到 `analyses.chart_json`）
    chart: dict[str, Any] | None = None
    #: 澄清卡（决策 18-2/3）：非 None 表示本轮是**澄清轮**——不落 analyses、
    #: 事件序列 status → slot → clarify → meta → done(record_id=null)
    clarify: dict[str, Any] | None = None
    narrative: str = ""
    key_numbers: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    elapsed_ms: int = 0
    llm_model: str = ""
    llm_tokens: int = 0
    #: SSE 事件序列（按 PRD 9.2 的顺序）；`EventSink` 让事件**产生即推送**（真流式）
    events: list[dict[str, Any]] = field(default_factory=EventSink)


# ── 主流程 ──────────────────────────────────────────────────────────────────

async def run_once(
    user_id: int,
    conversation_code: str,
    question: str,
    *,
    report_code: str | None = None,
    switch_active_report: bool = True,
    slot_patch: dict[str, Any] | None = None,
    slot_base: dict[str, Any] | None = None,
    preset_slot: dict[str, Any] | None = None,
    include_user_message: bool = True,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    emit: EventPush | None = None,
) -> AnalysisResult:
    """跑一轮问答：解析 → 生成 → 守卫 → 执行 → 解读 → 留痕。

    `emit`：事件**产生即回调**（真流式，v1.19.11）。传 None 时行为与从前完全一致
    （事件只攒在 `result.events` 里），因此**非 SSE 调用方（评测 / 重跑）无需改动**。

    失败（守卫拦截 / 执行报错 / 模型输出不可解析）同样落库：`analyses.status='failed'`。

    `switch_active_report`：显式传 `report_code` 且与会话活跃报表不同时，是否顺带切换会话活跃报表。
    对话入口保持默认 True（PRD F1.6：「后续提问作用于新报表」）；**重跑入口传 False**，
    以锁定原报表、保证前后可对比（且不改动用户的会话状态）。

    slot 三个入口参数（交付物 4 第四批，互斥使用）：

    - `slot_patch`（chat 可选字段 / clarify 回填）：先合入基线并**即刻持久化**到会话
      `slot_json`——条件芯片编辑（UIUX 6.3）即走这里；
    - `slot_base`（replay 用，决策 19-4）：以**原记录的 slot_json** 为继承基线，
      增量抽取照常跑（保住 PRD「整轮重跑」），会话后续追问改过的条件不污染；
      传 `{}` 表示显式不继承；
    - `preset_slot`（clarify 继续执行用，决策 18-3）：slot 已由「会话条件 + 回填」合并归一化，
      **跳过 LLM 增量抽取**（原问题上一轮已解析过，只剩缺失项被回填补齐），llm_calls 为
      `[gen_sql, narrative]` 两阶段。
    """
    prompt_text = (question or "").strip()
    if not prompt_text:
        raise validation_error("问题不能为空")
    if len(prompt_text) > 2000:
        raise validation_error("问题过长（上限 2000 字），请精简后重试")
    # slot_patch 的键白名单/类型校验在 try 之外：参数非法属于上下文级失败（端点发 error 事件）
    slot_patch = slot_service.validate_slot_patch(slot_patch)

    started = time.perf_counter()
    result = AnalysisResult(
        record_code="", conversation_code=conversation_code, report_code="",
        table_name="", question=prompt_text, status="success",
        events=EventSink(emit),
    )
    result.events.append({"event": "status", "data": {"stage": "parsing"}})

    llm = get_llm()
    settings = get_settings()
    calls: list[tuple[str, LLMReply, int]] = []
    #: 解读「未授权数字」告警样本（v1.19.6）：非空时落 `audit_logs(narrative_number_warn)`
    narrative_warnings: list[str] = []
    conversation: Conversation | None = None
    report = None

    try:
        async with control_session() as session:
            conversation = await _load_conversation(session, user_id, conversation_code)
            report = await _resolve_report(
                session, user_id, conversation, report_code, switch=switch_active_report
            )
            result.report_code = report.report_code
            result.table_name = report.table_name
            schema_text = schema_service.render_injection_text(report)["text"]
            schema = json.loads(report.schema_json) if report.schema_json else {}
            if slot_patch:
                # 条件芯片编辑即刻生效（决策 20-1）：即使用户后续追问触发澄清，改动也不丢
                merged_patch = slot_service.merge_slot(
                    json.loads(conversation.slot_json) if conversation.slot_json else {},
                    slot_patch,
                )
                conversation.slot_json = json.dumps(
                    slot_service.normalize_slot(merged_patch), ensure_ascii=False
                )
                await session.commit()

        if preset_slot is not None:
            # clarify 继续执行：slot 已由「会话条件 + 回填」合并归一化，跳过增量抽取
            merged = dict(preset_slot)
            increment_intent: str | None = None
            model_missing: str | None = None
        else:
            # ① slot 增量抽取（**轻量模型**，PRD 10.3 模型分级；继承基线见 slot_base）
            base_slot = (
                slot_base
                if slot_base is not None
                else (json.loads(conversation.slot_json) if conversation.slot_json else {})
            )
            # ①.0 **规则快通道优先**（v1.19.11 延迟专项）：问题明确且简单时直接产出增量，
            #      省掉一次 LLM 往返（实测 slot 阶段 1.5–2.0s，占端到端约 18–25%）；
            #      不确定的一律落到下面的 LLM 路径——澄清 / 多轮 / 分组 / 占比语义不退化。
            fast_increment = slot_service.fast_slot(prompt_text, base_slot)
            if fast_increment is not None:
                increment_intent = fast_increment.get("intent")
                model_missing = None  # 规则命中即视为条件齐备
                merged = slot_service.merge_slot(base_slot, fast_increment)
                logger.info(
                    "slot 快通道命中（跳过 LLM）：metric=%s time_range=%s",
                    fast_increment.get("metric"),
                    fast_increment.get("time_range"),
                )
            else:
                try:
                    slot_reply = await asyncio.wait_for(
                        llm.chat(
                            [
                                {"role": "system", "content": prompts.SLOT_SYSTEM},
                                {
                                    "role": "user",
                                    "content": prompts.slot_user_prompt(
                                        prompt_text,
                                        _available_hints(schema),
                                        current_slot=json.dumps(base_slot, ensure_ascii=False),
                                    ),
                                },
                            ],
                            purpose="slot",
                            model=_model_for(llm, settings.llm_model_light),
                            json_mode=True,
                        ),
                        timeout=SLOT_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    # ①.1 超时兜底（v1.19.11）：长尾不干等，用**宽松规则**构造增量继续。
                    #      拿不到指标才如实失败（不编造）——错误信息要能指导用户改写问题。
                    fallback = slot_service.fallback_slot(prompt_text, base_slot)
                    if fallback is None:
                        raise BizError(
                            504,
                            E_LLM,
                            f"槽位解析超时（超过 {SLOT_TIMEOUT_SECONDS:.0f}s），且无法从问题中识别指标",
                            {"purpose": "slot", "timeout_s": SLOT_TIMEOUT_SECONDS},
                            hint="请把问题写得明确一些（指明指标，如「总用电量」「峰段用电量」）后重试",
                        ) from None
                    logger.warning(
                        "slot 超时兜底（>%.0fs）：metric=%s time_range=%s",
                        SLOT_TIMEOUT_SECONDS,
                        fallback.get("metric"),
                        fallback.get("time_range"),
                    )
                    increment_intent = fallback.get("intent")
                    model_missing = None
                    merged = slot_service.merge_slot(base_slot, fallback)
                else:
                    calls.append(("slot", slot_reply, 0))
                    payload = _safe_json(slot_reply.text, stage="slot")
                    increment_intent = payload.get("intent")
                    model_missing = payload.get("missing_slot")  # 模型的澄清信号（不入 slot_json）
                    increment = _normalize_slot(payload)
                    merged = slot_service.merge_slot(base_slot, increment)

        # ①.5 澄清检查（决策 18-1：模型优先，规则 metric 缺失兜底；v1.19.6 增"指标词面兜底"）
        clarify_missing = slot_service.resolve_clarify(
            increment_intent=increment_intent,
            model_missing=model_missing,
            merged=merged,
            question=prompt_text,
        )
        if not clarify_missing and not merged.get("metric"):
            # 过澄清兜底：模型报 `missing_slot=metric` 被规则驳回后，按词面补一个指标，
            # 否则后续 SQL 生成阶段会缺指标（实测"指标写得很清楚却被判缺失"的题占全量 15%）
            guessed_metric = slot_service.guess_metric_from_question(prompt_text)
            if guessed_metric:
                merged["metric"] = guessed_metric
                logger.info(
                    "澄清兜底命中指标：%s（question=%.40s）", guessed_metric, prompt_text
                )
        if clarify_missing:
            # 澄清轮的 slot 事件展示**部分 slot**（合并后、填默认前）——芯片如实显示空位；
            # 「全期」默认只在继续执行（preset_slot 归一化）时填入
            partial = dict(merged)
            if partial.get("intent") == slot_service.CLARIFY_INTENT:
                partial["intent"] = None
            result.slot = partial
        else:
            result.slot = slot_service.normalize_slot(merged)
        result.events.append({"event": "slot", "data": result.slot})

        if clarify_missing:
            card = await _build_clarify_card(clarify_missing, user_id=user_id, schema=schema)
            result.llm_model = calls[0][1].model if calls else ""
            await _finish_clarify(
                result, card, user_id=user_id, conversation_code=conversation_code,
                question=prompt_text, llm=llm, started=started, calls=calls,
            )
            return result

        # ② 工具选择 + 生成（主力模型；prompt-v3.0 自报 route，SQL / pandas 二选一）
        slot_json_text = json.dumps(result.slot, ensure_ascii=False)
        gen_replies = await _chat_generate(
            llm,
            [
                {"role": "system", "content": prompts.SQL_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.sql_user_prompt(prompt_text, schema_text, slot_json_text),
                },
            ],
            # 与 graph 节点同口径：空输出**或**半成品都重发一次
            validate=_looks_like_generation,
        )
        for index, reply in enumerate(gen_replies):
            calls.append(("gen_sql", reply, index))
        gen_reply = gen_replies[-1]
        payload = _safe_json(gen_reply.text, stage="gen_sql")
        result.llm_model = gen_reply.model
        route = str(payload.get("route") or "").strip().lower()
        raw_sql = str(payload.get("sql") or "").strip()
        raw_code = str(payload.get("code") or "").strip()
        if route not in ("sql", "pandas"):
            # 模型没自报路由 → 按"给了什么"推断，再兜底 sql（T2：pandas 只做增量出口）
            route = "pandas" if (raw_code and not raw_sql) else "sql"
        if route == "pandas" and not raw_code:
            route = "sql"

        # ③④ 执行（SQL：守卫 + 只读连接；pandas：AST 预检 + 子进程沙箱。
        #     F2.4 自愈：失败 → 带错误重写一次 → 再失败换路 pandas 一次 → 仍失败才落 failed）
        table_payload, sandbox_figure = await _generate_and_execute(
            llm=llm,
            prompt_text=prompt_text,
            schema_text=schema_text,
            slot_json_text=slot_json_text,
            result=result,
            report=report,
            route=route,
            raw_sql=raw_sql,
            raw_code=raw_code,
            calls=calls,
            user_id=user_id,
            conversation_code=conversation_code,
            ip=ip,
            user_agent=user_agent,
        )
        result.table_ready = True
        result.columns = table_payload["columns"]
        result.rows = table_payload["rows"]
        result.row_count = table_payload["row_count"]
        result.truncated = bool(table_payload["truncated"])
        result.events.append({"event": "table", "data": table_payload})

        # ⑤ 图表（**本地计算、不调 LLM**；PRD F3.1：与表格使用同一份结果数据）
        #    位置固定在 table 之后、narrative 之前（UIUX 8.1）；失败只降级、不抛异常。
        #    pandas 路径若模型产出 `fig`，仅当图表服务画不出时作兜底（保证口径标记统一）
        chart_payload = _chart_payload(schema, result, sandbox_figure=sandbox_figure)
        result.chart = chart_payload.get("figure")
        result.events.append({"event": "chart", "data": chart_payload})

        # ⑥ 文字解读（主力模型；只依据表格数据，不编造）
        #    ⑥.0 超时兜底（v1.19.11）：解读**不是**结果本身——超时只跳过解读，
        #        表格与图表已经产出，不能因为"解说词"把整轮拖到十几秒甚至判失败。
        narrative_skipped_note: str | None = None
        narrative_timeout = _narrative_timeout()
        try:
            narrative_reply = await asyncio.wait_for(
                llm.chat(
                    [
                        {"role": "system", "content": prompts.NARRATIVE_SYSTEM},
                        {
                            "role": "user",
                            "content": prompts.narrative_user_prompt(
                                prompt_text, result.sql or "", table_payload,
                                truncated=result.truncated,
                                quality_hint=_quality_hint(report.quality_json),
                                code=result.code,
                            ),
                        },
                    ],
                    purpose="narrative",
                    # 解读用**专用档**（与 graph 节点同口径）：`_narrative_model()` 优先取
                    # `LLM_MODEL_NARRATIVE`，未配置回落轻量档；与 slot 的模型选择解耦。
                    model=_narrative_model(llm, settings),
                    json_mode=True,
                ),
                timeout=narrative_timeout,
            )
        except TimeoutError:
            # 超时**不再"无解读收场"**（需求方 2026-10-14 明确：解读是本项目的交付物）：
            # 用结果表能直接算出来的事实生成「自动摘要」，并如实标注来源。
            logger.warning("narrative 超时（>%.0fs）→ 改用自动摘要", narrative_timeout)
            # 告警文案先记在局部变量，**等解读字段赋值之后再追加** —— 下面的
            # `result.anomalies = [...]` 会整行重写，先写会被覆盖（写测试时才发现的顺序坑）
            narrative_skipped_note = (
                f"模型解读超时（超过 {narrative_timeout:.0f}s），已改用自动摘要；表格与图表结果不受影响"
            )
            narrative = _rule_narrative(
                table_payload, reason=f"模型解读超时（超过 {narrative_timeout:.0f}s）"
            )
        else:
            calls.append(("narrative", narrative_reply, 0))
            try:
                narrative = _safe_json(narrative_reply.text, stage="narrative")
            except BizError:
                # 解读解析失败**不能拖垮整题**（解读是增值项）：退化为自动摘要（此前会整题失败）
                logger.warning("narrative 解析失败 → 改用自动摘要", exc_info=True)
                narrative_skipped_note = (
                    "模型解读输出无法解析，已改用自动摘要；表格与图表结果不受影响"
                )
                narrative = _rule_narrative(table_payload, reason="模型解读输出无法解析")
        result.narrative = str(narrative.get("text") or "").strip()
        result.key_numbers = _as_list(narrative.get("key_numbers"))
        result.anomalies = [str(item) for item in _as_list(narrative.get("anomalies"))]
        if narrative_skipped_note:
            result.anomalies = [*result.anomalies, narrative_skipped_note]
        # ⑥.1 解读「未授权数字」检测（v1.19.6，真实模型评测暴露）：
        #      模型会援引上传阶段的质量信息（典型："数据质量提示显示存在 25979 行整行缺失"），
        #      那个数字既不在本轮结果里、也与问题无关 —— 属编造。这里**只检测不重试**（零额外 token）：
        #      写审计告警 + 在异常提示里如实告知用户"以表格/图表为准"。
        stray_numbers = _unauthorized_numbers(result.narrative, table_payload, result.key_numbers)
        if stray_numbers:
            result.anomalies = [
                *result.anomalies,
                "解读中出现未在查询结果里的数字（"
                + "、".join(stray_numbers[:5])
                + "），请以表格与图表为准",
            ]
            narrative_warnings = stray_numbers
        result.events.append(
            {"event": "narrative", "data": {
                "text": result.narrative,
                "key_numbers": result.key_numbers,
                "anomalies": result.anomalies,
            }}
        )
    except BizError as exc:
        if conversation is None:
            # 会话本身不存在/不属于本人 → **无处留痕**，交给端点按 HTTP 语义处理（SSE 里发 error 事件）
            raise
        result.status = "failed"
        result.error_code = exc.error_code
        result.error_message = exc.message
        result.last_rule = (exc.data or {}).get("rule_id")
    except Exception as exc:  # noqa: BLE001 —— 任何未预期异常也要留痕，不静默
        logger.exception("分析执行未预期异常")
        result.status = "failed"
        result.error_code = "E_INTERNAL"
        result.error_message = f"服务内部错误：{exc}"

    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    result.llm_tokens = sum(reply.total_tokens for _, reply, _ in calls)

    if result.status == "success":
        result.events.append(
            {"event": "meta", "data": {
                "elapsed_ms": result.elapsed_ms,
                "retries": result.retries,
                "llm_tokens": result.llm_tokens,
                "model": result.llm_model,
                "llm_impl": llm.name,
            }}
        )
    else:
        result.events.append(
            {"event": "error", "data": {
                "code": result.error_code,
                "message": result.error_message,
                "detail": f"route={result.route} rule={result.last_rule or '-'}",
                "retries": result.retries,
                "path_switched": result.path_switched,
            }}
        )

    # ⑥ 全量留痕（决策 4）：messages ×2 + analyses + llm_calls + 会话回写
    #    只要**会话存在**就留痕（报表不可用导致的失败也留痕，`report_id` 置 NULL）
    if conversation is not None:
        try:
            async with control_session() as session:
                conversation = await _load_conversation(session, user_id, conversation_code)
                result.record_code = await _persist(
                    session, user_id, conversation, report, result, calls, request_id,
                    include_user_message=include_user_message,
                )
        except Exception:  # noqa: BLE001 —— 结果已生成，落库失败不应让用户拿不到结果
            logger.exception("分析结果落库失败 record=%s", result.record_code)
            if not result.record_code:
                result.record_code = "a_000000"

    # ⑥.2 解读编造告警落审计（v1.19.6）：只留痕不改写结果——编造属"事实性问题"，
    #      需要可统计、可回溯（报告与审计都能看到），但**不重试**（避免额外 token 与延迟）。
    if narrative_warnings and conversation is not None:
        try:
            await _audit_narrative_warning(
                user_id=user_id,
                conversation_code=conversation_code,
                record_code=result.record_code,
                numbers=narrative_warnings,
                ip=ip,
                user_agent=user_agent,
            )
        except Exception:  # noqa: BLE001 —— 告警失败不影响结果返回
            logger.exception("解读未授权数字告警落库失败 record=%s", result.record_code)

    result.events.append({"event": "done", "data": {"record_id": result.record_code}})
    logger.info(
        "分析完成：%s status=%s route=%s rows=%d elapsed=%dms tokens=%d",
        result.record_code, result.status, result.route, result.row_count,
        result.elapsed_ms, result.llm_tokens,
    )
    return result


# ── 澄清回路（决策 18-2/3、19-1/2）─────────────────────────────────────────

async def run_clarify(
    user_id: int,
    conversation_code: str,
    question: str,
    *,
    slot_patch: dict[str, Any],
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    emit: EventPush | None = None,
) -> AnalysisResult:
    """澄清回填后就地继续执行（PRD F2.6 / F4.4，`POST /analysis/clarify`）。

    1. `slot_patch` 校验（键白名单/类型，决策 19-3）→ 合入会话 `slot_json` 并**持久化**
       （用户点选不丢，即使本轮再次触发澄清）；
    2. 以合并后的完整 slot 作为 `preset_slot` 继续跑完整链路——**跳过二次增量抽取**
       （原问题上一轮已解析过，本轮只补齐了缺失项），事件序列与 chat 相同；
    3. 归属隔离：会话不存在 / 不属于本人 → 404（与 `_load_conversation` 一致）。
    """
    patch = slot_service.validate_slot_patch(slot_patch)
    async with control_session() as session:
        conversation = await _load_conversation(session, user_id, conversation_code)
        base = json.loads(conversation.slot_json) if conversation.slot_json else {}
        merged = slot_service.normalize_slot(slot_service.merge_slot(base, patch))
        conversation.slot_json = json.dumps(merged, ensure_ascii=False)
        await session.commit()
    return await run_once(
        user_id,
        conversation_code,
        question,
        preset_slot=merged,
        include_user_message=False,  # 原问题在澄清轮已落过 user 消息（决策 19-1）
        ip=ip,
        user_agent=user_agent,
        request_id=request_id,
        emit=emit,
    )


async def _build_clarify_card(
    missing_slot: str, *, user_id: int, schema: dict[str, Any]
) -> dict[str, Any]:
    """组装澄清卡（决策 18-5：纯规则零 LLM）；报表歧义需查用户可见报表列表。"""
    reports: list[dict[str, Any]] | None = None
    if missing_slot == "report":
        async with control_session() as session:
            rows = (
                (
                    await session.execute(
                        select(UploadedReport)
                        .where(
                            or_(
                                UploadedReport.is_preset == 1,
                                UploadedReport.user_id == user_id,
                            ),
                            UploadedReport.status == "ready",
                        )
                        .order_by(UploadedReport.is_preset.desc(), UploadedReport.id)
                    )
                )
                .scalars()
                .all()
            )
            reports = [
                {"report_code": row.report_code, "filename": row.filename} for row in rows
            ]
    return slot_service.build_clarify_card(
        missing_slot, schema_columns=schema.get("columns"), reports=reports
    )


async def _finish_clarify(
    result: AnalysisResult,
    card: dict[str, Any],
    *,
    user_id: int,
    conversation_code: str,
    question: str,
    llm: Any,
    started: float,
    calls: list[tuple[str, LLMReply]],
) -> None:
    """澄清轮收尾（决策 18-2 / 19-1 / 20-2）：

    - 事件 `clarify`（`partial_slot` = slot 事件同款：合并后、填默认前的部分 slot）→
      `meta` → `done(record_id=null)`；
    - 落 messages×2（user=原问题、assistant=澄清卡 JSON，`analysis_id=NULL`）——
      历史里能看到澄清卡；**不落 analyses、不回写 slot_json**（没执行成功不产生脏芯片状态）。
    """
    result.clarify = card
    result.events.append(
        {
            "event": "clarify",
            "data": {
                "question": card["question"],
                "options": card["options"],
                "missing_slot": card["missing_slot"],
                "partial_slot": result.slot,
            },
        }
    )
    result.elapsed_ms = int((time.perf_counter() - started) * 1000)
    result.llm_tokens = sum(reply.total_tokens for _, reply, _ in calls)
    result.events.append(
        {"event": "meta", "data": {
            "elapsed_ms": result.elapsed_ms, "retries": 0,
            "llm_tokens": result.llm_tokens, "model": result.llm_model,
            "llm_impl": llm.name,
        }}
    )
    try:
        async with control_session() as session:
            conversation = await _load_conversation(session, user_id, conversation_code)
            await _persist_clarify(session, user_id, conversation, question, card)
    except Exception:  # noqa: BLE001 —— 与主流程同样的留痕宽容：落库失败不影响事件流
        logger.exception("澄清轮消息落库失败 conversation=%s", conversation_code)
    result.events.append({"event": "done", "data": {"record_id": None}})


async def _persist_clarify(
    session: AsyncSession,
    user_id: int,
    conversation: Conversation,
    question: str,
    card: dict[str, Any],
) -> None:
    """澄清轮落库：messages×2 + 会话计数（决策 19-1：执行轮不再落 user 消息）。"""
    now = audit_service.now_text()
    next_seq = (
        await session.scalar(
            select(func.coalesce(func.max(Message.seq_no), 0)).where(
                Message.conversation_id == conversation.id
            )
        )
    ) or 0
    session.add(
        Message(
            conversation_id=conversation.id, user_id=user_id, seq_no=next_seq + 1,
            role="user", content=question, analysis_id=None, created_at=now,
        )
    )
    # 澄清卡按决策 19-2 存 JSON（前端按 type 渲染；旧消息纯文本不受影响）
    session.add(
        Message(
            conversation_id=conversation.id, user_id=user_id, seq_no=next_seq + 2,
            role="assistant",
            content=json.dumps({"type": "clarify", **card}, ensure_ascii=False),
            analysis_id=None, created_at=now,
        )
    )
    conversation.message_count = (conversation.message_count or 0) + 2
    conversation.last_message_at = now
    conversation.updated_at = now
    if not conversation.title:
        conversation.title = question[:30]
    await session.commit()


# ── 上下文准备 ──────────────────────────────────────────────────────────────

async def _load_conversation(
    session: AsyncSession, user_id: int, conversation_code: str
) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(Conversation.conversation_code == conversation_code)
    )
    if conversation is None:
        raise not_found(f"会话不存在：{conversation_code}")
    if conversation.user_id != user_id:
        raise not_found(f"会话不存在：{conversation_code}")  # 不泄露他人资源是否存在
    return conversation


async def _resolve_report(
    session: AsyncSession,
    user_id: int,
    conversation: Conversation,
    report_code: str | None,
    *,
    switch: bool = True,
):
    """确定本轮分析的报表：显式给出 `report_code` 且会话活跃报表不同 → 先切换（PRD F1.6）。

    `switch=False`（重跑）时只锁定该报表、**不改动会话状态**。
    """
    from app.db.models import UploadedReport

    if report_code:
        report = await session.scalar(
            select(UploadedReport).where(UploadedReport.report_code == report_code)
        )
        if report is None or report.status == "deleted":
            raise not_found(f"报表不存在：{report_code}")
        if not report.is_preset and report.user_id != user_id:
            raise not_found(f"报表不存在：{report_code}")
        if switch and conversation.active_report_id != report.id:
            await conversation_service.set_active_report(
                user_id, conversation.conversation_code, report_code
            )
            conversation = await _load_conversation(session, user_id, conversation.conversation_code)
    else:
        if conversation.active_report_id is None:
            raise validation_error("该会话尚未选择活跃报表，请先切换报表后再提问")
        report = await session.get(UploadedReport, conversation.active_report_id)
        if report is None or report.status == "deleted":
            raise not_found("会话的活跃报表已被删除，请重新选择报表")

    if report.status != "ready":
        raise validation_error(
            f"报表 {report.report_code} 当前状态为 {report.status}，尚不可用于分析"
        )
    return report


def _available_hints(schema: dict[str, Any]) -> dict[str, list[str]]:
    """给 slot 提示词一份"可用指标 / 维度"清单，降低模型编造列名的概率。"""
    metrics: list[str] = []
    dimensions: list[str] = []
    for column in schema.get("columns") or []:
        name = column.get("name") or ""
        origin = column.get("origin_name") or name
        label = origin if origin != name else name
        if column.get("agg") == "sum":
            metrics.append(f"{label}（{name}）")
        elif name in ("year", "month", "day", "hour", "weekday", "is_weekend", "period", "date", "dt_hour", "year_month"):
            dimensions.append(f"{label}（{name}）")
    return {"可用指标": metrics[:20], "可用分组维度": dimensions[:10]}


def _normalize_slot(payload: dict[str, Any]) -> dict[str, Any]:
    """规整 slot：只保留 PRD F2.1 的 8 个字段，未知字段丢弃（避免脏字段进 `slot_json`）。"""
    keys = (
        "time_range", "granularity", "metric", "dimension", "filters", "compare",
        "intent", "chart_hint",
    )
    slot = {key: payload.get(key) for key in keys}
    slot["filters"] = [item for item in _as_list(slot.get("filters")) if isinstance(item, dict)]
    return slot


def _quality_hint(quality_json: str | None) -> str | None:
    """把质量报告的关键数字压缩成一句提示，注入解读环节（避免模型无视缺失/重复）。"""
    if not quality_json:
        return None
    try:
        dataset = (json.loads(quality_json) or {}).get("dataset") or {}
    except json.JSONDecodeError:
        return None
    parts: list[str] = []
    if dataset.get("missing_rows"):
        parts.append(f"存在 {dataset['missing_rows']} 行整行缺失")
    if dataset.get("partially_missing_rows"):
        parts.append(f"{dataset['partially_missing_rows']} 行部分列缺失")
    if dataset.get("duplicate_timestamps"):
        parts.append(f"{dataset['duplicate_timestamps']} 个重复时间戳")
    return "；".join(parts) or None


def _chart_payload(
    schema: dict[str, Any], result: AnalysisResult, *, sandbox_figure: dict[str, Any] | None = None
) -> dict[str, Any]:
    """生成 `chart` 事件的载荷（PRD F3.1 / UIUX 7.1–7.2）。

    **绝不向上抛异常**：`chart_service.build_chart` 内部已按决策 ⑧ 降级，这里再兜一层，
    保证"图表"这个增值环节永远不会把一条本来正确的分析拖成失败
    （画不出来时 `figure=null` + `fallback="table"` + `reason`，前端提示"该结果不适合可视化"）。

    pandas 路径的 `sandbox_figure`（模型在沙箱里产出的 `fig`）**只作兜底**：
    图表服务画得出来时仍用统一管线（语义色 / R3 收敛 / `layout.meta` 口径标记都由它保证）。
    """
    try:
        outcome = chart_service.build_chart(
            result.columns,
            result.rows,
            schema_columns=(schema or {}).get("columns"),
            slot=result.slot,
        )
        payload = outcome.to_payload()
        if payload.get("figure") is None and sandbox_figure:
            payload = {**payload, "figure": sandbox_figure, "fallback": None, "reason": None,
                       "chart_type": "custom"}
        return payload
    except Exception:  # noqa: BLE001 —— 兜底：任何异常都降级，不冒泡
        logger.exception("图表生成失败，已降级为表格")
        return {
            "figure": sandbox_figure, "chart_type": "table", "fallback": "table",
            "reason": "图表生成失败，已降级为表格", "unit": None, "x_axis": None,
            "series": [], "r3_adjusted": False, "r3_note": None,
            "dropped_columns": [], "excluded_columns": [],
            "top_n": None, "merged_count": 0, "downsampled_from": None, "gap_count": 0,
        }


# ── 生成与执行（双路径 + F2.4 自愈重试）─────────────────────────────────────

#: 不参与 F2.4 重试的错误：模型输出不可解析 / 超出范围 / 参数与上下文问题（重写也不会好）
_NO_RETRY_CODES = frozenset({E_LLM, E_OUT_OF_SCOPE, E_VALIDATION})


def _error_digest(exc: BizError) -> str:
    """把 BizError 压成一段喂给重写提示词的错误上下文（含沙箱堆栈尾行）。"""
    text = f"（{exc.error_code}）{exc.message}"
    trace_tail = (exc.data or {}).get("trace_tail") or []
    if trace_tail:
        text += "；" + " | ".join(str(line.strip()) for line in trace_tail[-2:])
    return text[:1500]


#: 比率类问题的"明细行"阈值（v1.19.23）：占比 / 比例若按"全期 / 年"问，答案本该是**一行（或几行）**；
#: 超过这个行数几乎必然是把"逐行比率列"当成了答案（实测速度档 q015 直接 `SELECT peak_ratio` 返回 1000 行）。
SHAPE_DETAIL_ROW_LIMIT = 100

#: 判定"问题在问占比"的词面（**不能只看槽位**：实测 q115 第一轮的 `intent` 掉成了 `查数`）。
_RATIO_WORDS = ("占比", "比例", "占多少", "百分比")

#: 触发形态自检的粒度：只有"全期 / 年"这类**汇总口径**才可能是形态错——
#: 粒度是"日 / 小时"时，"逐行给出每一天的比率"本身是合法形态（如"每天的峰段占比"），**不能动**。
_RATIO_SUMMARY_GRAIN = frozenset({"全期", "年"})


def _ratio_shape_issue(
    slot: dict[str, Any], question: str, table_payload: dict[str, Any]
) -> str | None:
    """结果形态自检：**问的是"占比 / 比例"，返回的却是明细行** → 带原因交给重写环重写一次。

    为什么放在**执行之后**：`SELECT peak_ratio FROM report_daily LIMIT 1000` 这类写法
    **既不会被执行报错、也不会被守卫拦住**（语法合法、只读、有 LIMIT），
    只有看**结果行数**才能判断形态错了 —— 这正是"能在代码里机检的事就别指望模型"。

    判定取**保守口径**（三个条件同时成立才触发）：① 问占比（槽位 `intent` 或问题词面）；
    ② 粒度是"全期 / 年"（汇总口径）；③ 结果行数 > `SHAPE_DETAIL_ROW_LIMIT`。
    """
    asks_ratio = (slot or {}).get("intent") == "占比" or any(
        word in (question or "") for word in _RATIO_WORDS
    )
    if not asks_ratio:
        return None
    if (slot or {}).get("granularity") not in _RATIO_SUMMARY_GRAIN:
        return None
    row_count = int(table_payload.get("row_count") or 0)
    if row_count <= SHAPE_DETAIL_ROW_LIMIT:
        return None
    columns = [str(item.get("name")) for item in (table_payload.get("columns") or [])]
    return (
        f"问题要的是「占比 / 比例」，但结果返回了 {row_count} 行明细"
        f"（列为 {'、'.join(columns[:6])}）——比率类问题应给出**汇总后的比例**，"
        "且必须用「分子求和 ÷ 分母求和」重算（红线 R6），不要把逐行比率列直接列出来。"
    )


#: 极值 / 排名类问题的词面（v1.19.24）
_EXTREME_WORDS = ("最高", "最低", "最大", "最小", "最多", "最少", "峰值", "排名", "第几", "前几", "top")

#: "只做筛选"的追问词面（v1.19.24）：这类追问**只改过滤条件**，不该把上一轮的分组维度丢掉。
_FILTER_ONLY_WORDS = ("只看", "只要", "仅看", "只保留", "仅保留", "排除")


def _extreme_detail_issue(
    slot: dict[str, Any], question: str, table_payload: dict[str, Any]
) -> str | None:
    """结果形态自检：**问的是"最高 / 最低 / 第 N 名"，返回的却是一堆明细行**。

    与比率那条同一类问题：`SELECT date, consumption_kwh FROM report_daily ORDER BY … LIMIT 1000`
    语法合法、能执行，但"极值题"要的是**一行（或前 N 行）**。

    保守口径同比率条：只在"全期 / 年"这类**汇总粒度** + 行数超阈值时触发——
    「各年度的峰值功率」这类**逐期极值**本来就该是多行，且行数很小，不会触发。
    """
    asks_extreme = any(word in (question or "").lower() for word in _EXTREME_WORDS)
    if not asks_extreme:
        return None
    if (slot or {}).get("granularity") not in _RATIO_SUMMARY_GRAIN:
        return None
    row_count = int(table_payload.get("row_count") or 0)
    if row_count <= SHAPE_DETAIL_ROW_LIMIT:
        return None
    return (
        f"问题问的是「极值 / 排名」，但结果返回了 {row_count} 行明细——"
        "极值类问题应给出**汇总后的那一行（或前 N 行）**（`ORDER BY 指标 LIMIT 1/N`，并排除 NULL），"
        "不要把整表按序罗列出来。"
    )


def _dropped_dimension_issue(
    slot: dict[str, Any], question: str, sql: str, table_payload: dict[str, Any]
) -> str | None:
    """结果形态自检：**"只看某类"的筛选型追问，把上一轮的分组维度整列丢了**（实测 q119）。

    实测（速度档）：「按星期几汇总总用电量。」→「只看工作日。」——正确结果应是 **2 列**
    （`weekday` + 合计），模型却只回了 1 列合计，**分组维度整列消失**。

    判定（三个条件同时成立）：① 本轮是**筛选型追问**（含"只看 / 只要 / 仅看 / 只保留 / 排除"）；
    ② 槽位里**有分组维度**（`dimension` 非空，通常由上一轮继承而来）；③ 生成的 SQL **没有 `GROUP BY`**。
    """
    if not any(word in (question or "") for word in _FILTER_ONLY_WORDS):
        return None
    dimension = (slot or {}).get("dimension")
    if not dimension:
        return None
    if "group by" in (sql or "").lower():  # 守卫给出的 SQL 已被 sqlglot 规范化，写法稳定
        return None
    columns = [str(item.get("name")) for item in (table_payload.get("columns") or [])]
    return (
        f"这是**筛选型追问**（「{(question or '').strip()[:20]}」），上一轮的分组维度是「{dimension}」，"
        f"但本轮结果只剩 {len(columns)} 列（{'、'.join(columns[:6])}）——"
        f"**只改过滤条件、不要丢掉分组**：按「{dimension}」分组后再叠加本轮的筛选条件。"
    )


def _shape_issue(
    slot: dict[str, Any], question: str, sql: str, table_payload: dict[str, Any]
) -> tuple[str, str] | None:
    """结果形态自检总入口（v1.19.24）：返回（`rule_id`, 说明）或 None。

    覆盖三类**语法合法、守卫放行、执行也不报错、只有看结果形态才能发现**的错：
    ① 问占比却回明细行；② 问极值却回明细行；③ 筛选型追问丢掉分组维度。
    三类都在**执行之后**判断（前两类看行数，第三类看 SQL 里有没有 `GROUP BY`）。
    """
    for rule_id, check in (
        ("shape:ratio_detail", _ratio_shape_issue),
        ("shape:extreme_detail", _extreme_detail_issue),
    ):
        issue = check(slot, question, table_payload)
        if issue:
            return rule_id, issue
    issue = _dropped_dimension_issue(slot, question, sql, table_payload)
    if issue:
        return "shape:dimension_dropped", issue
    return None


async def _generate_and_execute(
    *,
    llm: Any,
    prompt_text: str,
    schema_text: str,
    slot_json_text: str,
    result: AnalysisResult,
    report: Any,
    route: str,
    raw_sql: str,
    raw_code: str,
    calls: list[tuple[str, LLMReply, int]],
    user_id: int,
    conversation_code: str,
    ip: str | None,
    user_agent: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """按路由执行并实施 F2.4 自愈（返回 table_payload 与沙箱产出的 figure）。

    重试链（每个额度最多用一次，总生成调用 ≤3 次 + slot/narrative）：

    1. **SQL 带错误重写**：守卫拦截 / 执行报错 → 错误上下文回喂，模型可改报 pandas 路由；
    2. **换路 pandas**：SQL 重写后仍失败 → 改写 pandas 代码走沙箱（`path_switched=1`）；
    3. **pandas 带错误重写**：pandas 首选路径失败 → 同样重写一次（不反向换路——
       SQL 能覆盖的场景模型首选 pandas 时，重写后仍不行说明问题不在路径上）。

    额度用尽仍失败 → 抛出**最后一次**的 BizError，由 run_once 统一落库 failed。
    """
    settings = get_settings()
    current_route = route
    current_sql = raw_sql
    current_code = raw_code
    retried = False      # 「带错误重写」额度（SQL/pandas 共用一次）
    switched = False     # 「换路」额度（仅 SQL → pandas 方向）

    while True:
        try:
            if current_route == "sql":
                # 守卫（表名白名单 = 当前会话活跃报表的表名）
                guarded = sql_guard.guard_sql(current_sql, {report.table_name})
                result.sql = guarded.sql or current_sql
                if guarded.null_guarded:
                    # Top-N 极值自动排除 NULL（v1.19.23）：**落一条日志**——落库的 SQL 里
                    # `模型自己写的 IS NOT NULL` 与 `守卫补的` 都会被 sqlglot 规范化成同一形态，
                    # 日志是唯一能区分二者的痕迹（排障与"修复是否真的生效"都靠它）。
                    logger.info(
                        "守卫为 Top-N 极值补了 NULL 排除：%s（record=%s）",
                        "、".join(guarded.null_guarded), result.record_code,
                    )
                result.events.append(
                    {"event": "code", "data": {"route": "sql", "sql": result.sql}}
                )
                if not guarded.allowed:
                    await _audit_blocked(
                        user_id, conversation_code, current_sql, guarded, ip, user_agent
                    )
                    raise BizError(
                        400, E_SQL_FORBIDDEN,
                        f"生成的 SQL 未通过安全校验：{guarded.reason}",
                        {"rule_id": guarded.rule_id},
                        hint="系统只允许对当前报表表名范围内的单条 SELECT 查询",
                    )
                # 只读执行
                result.events.append({"event": "status", "data": {"stage": "executing"}})
                table_payload = await _execute(
                    guarded.sql, limit_applied=guarded.limit_applied
                )
                # ⑨.5 结果形态自检（v1.19.23 起）：问了"占比 / 极值"却回明细行、或"只看某类"的筛选型
                #      追问把分组维度整列丢了 → 当做一次失败，交给下面统一的重写环（带原因重写一次）；
                #      形态对了才继续走图表与解读。
                shape = _shape_issue(result.slot, prompt_text, guarded.sql, table_payload)
                if shape:
                    rule_id, message = shape
                    raise BizError(
                        500, E_SQL_EXEC, message, {"rule_id": rule_id},
                        hint="请按问题要的**结果形态**重写：比率给汇总比例、极值给那一行、筛选型追问保留分组维度",
                    )
                result.route = "sql"
                result.retries = int(retried)
                result.path_switched = switched
                return table_payload, None

            # pandas 路径：AST 预检在 run_pandas_code 内部（不过预检根本不进沙箱）
            result.code = current_code
            result.events.append(
                {"event": "code", "data": {"route": "pandas", "code": current_code}}
            )
            result.events.append({"event": "status", "data": {"stage": "executing"}})
            outcome = await sandbox_service.run_pandas_code(
                current_code, report.table_name, db_path=settings.reports_db
            )
            table_payload = {
                "columns": outcome.columns,
                "rows": outcome.rows,
                "row_count": outcome.row_count,
                "truncated": outcome.truncated,
            }
            result.route = "pandas"
            result.retries = int(retried)
            result.path_switched = switched
            return table_payload, outcome.figure

        except BizError as exc:
            if exc.error_code in _NO_RETRY_CODES:
                raise
            # 安全拦截（守卫拒绝）与执行失败分开对待：**拦截不换路**——否则"删表"这类
            # 恶意请求会换来一次无关的 pandas 成功结果，且多给一次生成机会；
            # 执行失败才允许换路（问题可能真的在 SQL 表达力上）。
            blocked = exc.error_code in (E_SQL_FORBIDDEN, E_CODE_FORBIDDEN)
            error_context = _error_digest(exc)
            if exc.error_code == E_CODE_FORBIDDEN:
                # 代码预检拦截落审计（与 SQL 侧的 `_audit_blocked` 对位）：此前只落
                # `analyses.error_code`，B5 拦截统计因此只看得到 `sql_blocked`
                await _audit_code_blocked(
                    user_id, conversation_code, current_code,
                    str((exc.data or {}).get("rule_id") or ""), exc.message,
                    ip, user_agent,
                )

            # ① 带错误重写一次（SQL 路径的重写在"执行失败"时允许模型改报 pandas 路由）
            if not retried:
                retried = True
                if current_route == "sql":
                    reply = await llm.chat(
                        [
                            {"role": "system", "content": prompts.SQL_SYSTEM},
                            {
                                "role": "user",
                                "content": prompts.sql_rewrite_user_prompt(
                                    prompt_text, schema_text, slot_json_text,
                                    previous_sql=current_sql, previous_error=error_context,
                                ),
                            },
                        ],
                        purpose="gen_sql",
                        json_mode=True,
                    )
                    calls.append(("gen_sql", reply, 1))
                    payload = _safe_json(reply.text, stage="gen_sql")
                    new_sql = str(payload.get("sql") or "").strip()
                    new_code = str(payload.get("code") or "").strip()
                    new_route = str(payload.get("route") or "").strip().lower()
                    if not blocked and new_code and (new_route == "pandas" or not new_sql):
                        current_route, current_code, current_sql = "pandas", new_code, ""
                        switched = True
                    else:
                        current_sql = new_sql or current_sql
                    continue

                reply = await llm.chat(
                    [
                        {"role": "system", "content": prompts.PANDAS_SYSTEM},
                        {
                            "role": "user",
                            "content": prompts.pandas_code_user_prompt(
                                prompt_text, schema_text, slot_json_text,
                                previous_code=current_code, previous_error=error_context,
                            ),
                        },
                    ],
                    purpose="gen_code",
                    json_mode=True,
                )
                calls.append(("gen_code", reply, 1))
                payload = _safe_json(reply.text, stage="gen_code")
                current_code = str(payload.get("code") or "").strip()
                if not current_code:
                    raise
                continue

            # ② 换路 pandas（仅 SQL 路径的**执行失败**；安全拦截不换）
            if current_route == "sql" and not switched and not blocked:
                switched = True
                reply = await llm.chat(
                    [
                        {"role": "system", "content": prompts.PANDAS_SYSTEM},
                        {
                            "role": "user",
                            "content": prompts.pandas_code_user_prompt(
                                prompt_text, schema_text, slot_json_text,
                                previous_error=error_context,
                            ),
                        },
                    ],
                    purpose="gen_code",
                    json_mode=True,
                )
                calls.append(("gen_code", reply, 1))
                payload = _safe_json(reply.text, stage="gen_code")
                current_code = str(payload.get("code") or "").strip()
                current_route = "pandas"
                if not current_code:
                    raise
                continue

            # 额度用尽：如实抛最后一次错误
            result.retries = int(retried)
            result.path_switched = switched
            raise


def _safe_json(text_: str, *, stage: str) -> dict[str, Any]:
    """三层容错解析（`json.loads` → `json_repair` → 代码块）；失败抛 `E_LLM`。"""
    from app.core.llm_client import extract_json

    try:
        return extract_json(text_)
    except BizError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BizError(500, E_LLM, f"{stage} 阶段模型输出解析失败：{exc}", {"stage": stage}) from exc


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


# ── 执行 ────────────────────────────────────────────────────────────────────

async def _execute(sql: str, *, limit_applied: bool = False) -> dict[str, Any]:
    """只读执行（`mode=ro` + `query_only`）；行数上限由守卫补的 LIMIT 保证。

    SQLite 报错（列名不存在 / 类型不可比 / 语法死角等）包装为 `E_SQL_EXEC`——
    这是 F2.4「带错误信息重写」的输入，不能让它裸奔成 E_INTERNAL。
    """
    from sqlalchemy.exc import SQLAlchemyError

    try:
        async with report_ro_conn() as conn:
            rows = (await conn.execute(text(sql))).mappings().all()
    except SQLAlchemyError as exc:
        origin = str(exc.orig) if getattr(exc, "orig", None) else str(exc)
        raise BizError(
            500, E_SQL_EXEC,
            f"查询执行失败：{origin.splitlines()[0][:200]}",
            {"sqlite_error": origin[:500]},
            hint="系统会带着错误信息自动重写一次；若仍失败请调整提问方式",
        ) from exc
    payload_rows = [dict(row) for row in rows]
    columns = (
        [{"name": key, "type": _type_name(value)} for key, value in payload_rows[0].items()]
        if payload_rows
        else []
    )
    return {
        "columns": columns,
        "rows": payload_rows,
        "row_count": len(payload_rows),
        # 只有"守卫补了 LIMIT 且确实取满"才判定为截断（结果本来就这么多的不算截断）
        "truncated": bool(limit_applied and len(payload_rows) >= sql_guard.DEFAULT_LIMIT),
    }


def _model_for(llm: Any, light_model: str) -> str | None:
    """模型分级（PRD 10.3）：支持分级的客户端（OpenAI 兼容）传轻量模型名，其余不传。

    依据客户端的**能力位** `supports_model_tiers`（而不是厂商名）——换厂商时不需要改这里，
    也避免"厂商名硬编码"再次渗透进业务逻辑。
    """
    return light_model if getattr(llm, "supports_model_tiers", False) else None


def _type_name(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "TEXT"


async def _audit_blocked(
    user_id: int,
    conversation_code: str,
    raw_sql: str,
    guarded: sql_guard.GuardResult,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """守卫拦截 → `audit_logs(sql_blocked)`（PRD S1 / 数据库设计 4.8）。"""
    async with control_session() as session:
        session.add(
            audit_service.build_audit(
                "sql_blocked",
                "high",
                "blocked",
                user_id=user_id,
                rule_id=guarded.rule_id,
                payload=json.dumps(
                    {"sql": raw_sql[:2000], "reason": guarded.reason,
                     "conversation_code": conversation_code},
                    ensure_ascii=False,
                ),
                ip=ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
    logger.warning("SQL 被守卫拦截：rule=%s reason=%s", guarded.rule_id, guarded.reason)


#: 代码预检里属于「**越权 / 沙箱逃逸尝试**」的规则——命中时**追加**一条 `injection_attempt`：
#: 非白名单 import（试图引入沙箱外能力）、双下划线（反射逃逸链）、危险调用（`open`/`eval`/
#: `exec`/`getattr`…）、危险模块名裸名（`os`/`sys`/`subprocess`…）。
#: 其余规则（空代码 / 超长 / 语法错）只记 `code_blocked`——那更像"模型把代码写跑了"，
#: 与"主动突破沙箱"不是一回事，混在一起会让 B5 的统计失去区分度。
_ESCAPE_RULE_IDS = frozenset({
    "codeguard:import_not_allowed",
    "codeguard:dunder",
    "codeguard:forbidden_call",
    "codeguard:forbidden_name",
})


async def _audit_code_blocked(
    user_id: int,
    conversation_code: str,
    code: str,
    rule_id: str,
    reason: str,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """代码 AST 预检拦截 → `audit_logs`（PRD S1 / 数据库设计 4.8 / B5 拦截统计）。

    与 SQL 侧的 `_audit_blocked` **对位**：每次拦截记一条 `code_blocked`
    （首轮 + 重写各一条，与 `sql_blocked` 的"逐次留痕"口径一致）；
    命中 `_ESCAPE_RULE_IDS`（越权/逃逸）时**再**记一条 `injection_attempt`（同 `rule_id`、
    `payload.policy=sandbox_escape`）——`injection_attempt ⊆ code_blocked`，前者是后者的高危子集。

    两个 `event_type` 都在 `audit_logs` 白名单内，但 v1.19.28 之前**没有任何写入方**
    （代码拦截只落 `analyses.error_code = E_CODE_FORBIDDEN`，见测试报告 §4.2 的如实登记）。
    """
    escape = rule_id in _ESCAPE_RULE_IDS
    payload = json.dumps(
        {"code": (code or "")[:2000], "reason": reason,
         "conversation_code": conversation_code},
        ensure_ascii=False,
    )
    async with control_session() as session:
        session.add(
            audit_service.build_audit(
                "code_blocked",
                "high",
                "blocked",
                user_id=user_id,
                rule_id=rule_id,
                payload=payload,
                ip=ip,
                user_agent=user_agent,
            )
        )
        if escape:
            session.add(
                audit_service.build_audit(
                    "injection_attempt",
                    "high",
                    "blocked",
                    user_id=user_id,
                    rule_id=rule_id,
                    payload=json.dumps(
                        {"code": (code or "")[:2000], "reason": reason,
                         "conversation_code": conversation_code,
                         "policy": "sandbox_escape"},
                        ensure_ascii=False,
                    ),
                    ip=ip,
                    user_agent=user_agent,
                )
            )
        await session.commit()
    logger.warning(
        "代码被 AST 预检拦截：rule=%s escape=%s reason=%s", rule_id, escape, reason
    )


#: 未授权数字检测的最小关注量级（v1.19.6）：≥ 该值的数字才检查。
#: 小计数（"3 个设备""12 个月"）常由结果推导而来，误报率高、危害小，故忽略。
_STRAY_NUMBER_MIN = 100
#: 数值提取（够用即可：整数与小数，负号可选）
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
#: **千分位归一**（2026-10-14）：`1,234` 不归一会被 `_NUMBER_RE` 拆成 `1` 与 `234`，
#: 后半段常被判成"未授权数字"→ 误报。自动摘要用千分位展示时先踩到，对**真实模型解读**同样适用。
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _numbers_in(text: str) -> list[float]:
    """从文本里提取全部数字（失败忽略，不抛异常；先做千分位归一）。"""
    values: list[float] = []
    for raw in _NUMBER_RE.findall(_THOUSANDS_RE.sub("", text or "")):
        try:
            values.append(float(raw))
        except ValueError:  # pragma: no cover - 正则已限定形态
            continue
    return values


def _unauthorized_numbers(
    text: str, table_payload: dict[str, Any], key_numbers: list[dict[str, Any]]
) -> list[str]:
    """解读里的「未授权数字」：既不在结果表中、也不在 `key_numbers` 里（相对容差 0.5%）。

    真实模型评测暴露的典型编造："数据质量提示显示存在 **25979** 行整行缺失" —— 该数字来自上传阶段的
    质量报告，与本轮结果无关（PRD P4「不编造」红线）。这里**只检测与留痕**：不重试、不改写解读，
    因为改写需要再调一次模型（额外 token、额外延迟），而本项目的诉求是"可发现、可统计、可复盘"。
    """
    if not text:
        return []
    allowed: list[float] = []
    for row in table_payload.get("rows") or []:
        values = row.values() if isinstance(row, dict) else [row]
        for value in values:
            allowed.extend(_numbers_in(str(value)))
    for value in (table_payload.get("row_count"), len(table_payload.get("columns") or [])):
        if isinstance(value, (int, float)):
            allowed.append(float(value))
    for item in key_numbers or []:
        if isinstance(item, dict):
            allowed.extend(_numbers_in(str(item.get("value"))))

    stray: list[str] = []
    # 与 `_numbers_in()` 同口径：**先做千分位归一**再提取（否则 `1,200` 会被拆成 `1` 与 `200`，
    # 后半段 ≥ `_STRAY_NUMBER_MIN` → 误报"编造"；2026-10-14 实测于自动摘要与真实模型解读）
    for token in _NUMBER_RE.findall(_THOUSANDS_RE.sub("", text)):
        value = float(token)
        if abs(value) < _STRAY_NUMBER_MIN:
            continue
        if any(abs(value - base) <= max(1e-6, abs(value) * 0.005) for base in allowed):
            continue
        if token not in stray:
            stray.append(token)
    return stray


async def _audit_narrative_warning(
    *,
    user_id: int,
    conversation_code: str,
    record_code: str,
    numbers: list[str],
    ip: str | None,
    user_agent: str | None,
) -> None:
    """解读出现「未授权数字」→ `audit_logs(narrative_number_warn)`（v1.19.6）。

    `severity=medium`：不影响本轮结果可用性，但偏离"不编造"红线（PRD P4），需可统计、可回溯。
    """
    async with control_session() as session:
        session.add(
            audit_service.build_audit(
                "narrative_number_warn",
                "medium",
                # `action` 是 CHECK 白名单（blocked/warned/allowed）：v1.19.6 误写 `flagged`，
                # 插入被拒 → 告警**静默丢失**（v1.19.12 修正，见 `audit.py` 注释）
                "warned",
                user_id=user_id,
                payload=json.dumps(
                    {
                        "record_code": record_code,
                        "conversation_code": conversation_code,
                        "numbers": numbers[:10],
                    },
                    ensure_ascii=False,
                ),
                ip=ip,
                user_agent=user_agent,
            )
        )
        await session.commit()
    logger.warning("解读包含未授权数字：record=%s numbers=%s", record_code, numbers[:5])


# ── 落库（决策 4：全留痕）───────────────────────────────────────────────────

async def _persist(
    session: AsyncSession,
    user_id: int,
    conversation: Conversation,
    report: Any | None,
    result: AnalysisResult,
    calls: list[tuple[str, LLMReply]],
    request_id: str | None,
    *,
    include_user_message: bool = True,
) -> str:
    """一次事务写入：用户消息 + 分析记录 + 助手消息 + 模型调用流水 + 会话回写。

    `report` 为 None 时（活跃报表被删/不可用导致的失败）仍留痕，`report_id` 置 NULL。
    `include_user_message=False`（clarify 执行轮，决策 19-1）：原问题在澄清轮已落过
    user 消息，本轮只落 assistant 结果，避免同一问题出现两条用户气泡。
    """
    now = audit_service.now_text()
    # 只有真正执行出结果表才写快照：守卫拦截等"没跑过"的情况写 NULL（写 `[]` 会误导成"查了但没数据"）
    snapshot_json, snapshot_truncated = (
        _snapshot(result.rows) if result.table_ready else (None, False)
    )

    analysis = Analysis(
        record_code=f"tmp-{int(time.time() * 1000)}",  # 占位，flush 后用自增 id 生成正式编号
        user_id=user_id,
        conversation_id=conversation.id,
        report_id=report.id if report is not None else None,
        question=result.question,
        slot_json=json.dumps(result.slot, ensure_ascii=False),
        # ⚠️ `analyses.route` 上有 CHECK 约束（`route IS NULL OR route IN ('sql','pandas')`），
        # 而 `AnalysisResult.route` 的默认值是**空串**——失败路径（例如 slot 超时）会把 '' 写进去，
        # 触发 `IntegrityError` → **失败留痕没写成、异常还上抛**（实测 q119 第二轮，2026-10-14）。
        # 这里统一归一化：非 sql/pandas 一律写 NULL（"没跑到生成"与"生成了但被拦"都不该记成 sql）。
        route=result.route if result.route in ("sql", "pandas") else None,
        sql_text=result.sql,
        code_text=result.code,
        result_columns_json=(
            json.dumps(result.columns, ensure_ascii=False) if result.table_ready else None
        ),
        result_rows_json=snapshot_json,
        result_row_count=result.row_count if result.table_ready else None,
        result_truncated=1 if (result.truncated or snapshot_truncated) else 0,
        # 图表 figure（降级时为 None → 写 NULL，而不是写一个空对象让前端以为"有图"）
        chart_json=(
            json.dumps(result.chart, ensure_ascii=False) if result.chart is not None else None
        ),
        narrative=result.narrative or None,
        key_numbers_json=json.dumps(result.key_numbers, ensure_ascii=False) if result.key_numbers else None,
        anomalies_json=json.dumps(result.anomalies, ensure_ascii=False) if result.anomalies else None,
        status=result.status,
        error_code=result.error_code,
        error_message=(result.error_message or "")[:500] or None,
        llm_model=result.llm_model or None,
        elapsed_ms=result.elapsed_ms,
        retries=result.retries,
        path_switched=1 if result.path_switched else 0,
        prompt_version=prompts.PROMPT_VERSION,
        schema_version=(report.caliber_version if report is not None else None)
        or prompts.CALIBER_VERSION,
        created_at=now,
    )
    session.add(analysis)
    await session.flush()
    analysis.record_code = f"{RECORD_CODE_PREFIX}{analysis.id:06d}"
    await session.flush()

    for stage, reply, is_retry in calls:
        session.add(
            LLMCall(
                analysis_id=analysis.id,
                stage=stage,
                model_name=reply.model,
                temperature=getattr(reply, "temperature", None),
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
                total_tokens=reply.total_tokens,
                elapsed_ms=reply.elapsed_ms,
                is_retry=is_retry,
                status="success",
                request_id=request_id,
                created_at=now,
            )
        )

    next_seq = (
        await session.scalar(
            select(func.coalesce(func.max(Message.seq_no), 0)).where(
                Message.conversation_id == conversation.id
            )
        )
    ) or 0
    added = 1
    if include_user_message:
        session.add(
            Message(
                conversation_id=conversation.id, user_id=user_id, seq_no=next_seq + 1,
                role="user", content=result.question, analysis_id=None, created_at=now,
            )
        )
        added = 2
    session.add(
        Message(
            conversation_id=conversation.id, user_id=user_id,
            seq_no=next_seq + added,
            role="assistant",
            content=result.narrative if result.status == "success" else FAILURE_ASSISTANT_TEXT,
            analysis_id=analysis.id, created_at=now,
        )
    )

    conversation.slot_json = json.dumps(result.slot, ensure_ascii=False)
    conversation.message_count = (conversation.message_count or 0) + added
    conversation.last_message_at = now
    conversation.updated_at = now
    if not conversation.title:
        conversation.title = result.question[:30]

    await session.commit()
    return analysis.record_code


def _snapshot(rows: list[dict[str, Any]]) -> tuple[str, bool]:  # noqa: UP037
    """结果快照落库：超过 200KB 逐行裁剪并标记 `result_truncated`（PRD 数据库设计 5.5）。"""
    encoded = json.dumps(rows, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) <= RESULT_SNAPSHOT_LIMIT:
        return encoded, False
    kept: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        chunk = len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
        if size + chunk > RESULT_SNAPSHOT_LIMIT:
            break
        kept.append(row)
        size += chunk
    return json.dumps(kept, ensure_ascii=False, default=str), True
