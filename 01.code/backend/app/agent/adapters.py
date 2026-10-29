"""图状态 ⇄ 既有 `AnalysisResult` 的适配层（**过渡层**，随阶段推进逐步变薄）。

为什么需要它（见 `大修工作进度/阶段1-编排图化骨架.md`）：
阶段 1–2 的目标是"**用 LangGraph 承载编排、但不改行为**"，因此图节点内部仍复用
`services/analysis_service.py` 里已被 **303 条用例**覆盖的函数。本模块负责两边的形态转换。

三层数据分工（这是图化能否成立的关键）：

| 层 | 放什么 | 是否可序列化 | 传递方式 |
| --- | --- | --- | --- |
| `AnalysisState`（`agent/state.py`） | 纯数据（slot / route / sql / 表格 / calls 镜像 …） | ✅（为阶段 3 的 `SqliteSaver` 准备） | 图的 state 通道 |
| `GraphContext`（本模块） | LLM 客户端、SSE 回调、`calls` 累加器、警告收集 | ❌ | `RunnableConfig.configurable` |
| `AnalysisResult`（既有链路） | 既有函数群的"工作对象"，被就地改写 | ❌（含 `EventSink` 副作用） | 节点内部临时对象 |

事件推送：`GraphContext.emit_event()` **优先走 LangGraph 的 custom stream**
（`get_stream_writer()`，即 `stream_mode="custom"`），不存在时回落到既有 SSE 回调 `emit`，
两者都没有则丢弃（对应 `run_once(emit=None)` 的评测/重跑调用方）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agent.state import AnalysisState, LLMCallRecord
from app.services.analysis_service import AnalysisResult, EventPush, EventSink

#: LangGraph custom stream 的写入器（`langgraph.config.get_stream_writer()` 的返回值）
StreamWriter = Callable[[Any], None]


@dataclass
class GraphContext:
    """节点的**运行时依赖**（不进 state，经 `config["configurable"]["ctx"]` 传递）。"""

    llm: Any
    settings: Any
    #: 既有 SSE 推送回调（`run_once(emit=...)`；评测/重跑为 None）
    emit: EventPush | None = None
    #: LangGraph custom stream 写入器（有则优先使用）
    stream_writer: StreamWriter | None = None
    #: LLM 调用累加器：与既有链路同构的 `(stage, LLMReply, is_retry)` 元组列表，
    #: 由 `_persist` 写入 `llm_calls` 表（既有函数签名要求，故保留原形态）
    calls: list[tuple[str, Any, int]] = field(default_factory=list)
    #: 解读「未授权数字」告警样本（既有链路会落 `audit_logs`）
    narrative_warnings: list[str] = field(default_factory=list)
    #: 起始时刻（用于 elapsed_ms）
    started: float = field(default_factory=time.perf_counter)
    #: 本轮会话编号（运行器写入；结果对象需要回填 `conversation_code`）
    conversation_code: str = ""
    #: 会话是否已成功加载（`run_once` 里用 `conversation is None` 区分"无处留痕 → 向上抛"）
    conversation_loaded: bool = False
    #: 执行阶段捕获的 BizError（persist 节点判失败原因时使用）
    last_error: Any = None
    #: pandas 路径模型自产的 Plotly figure（**不可序列化**，供 chart 节点兜底）
    sandbox_figure: Any = None

    #: 是否已尝试探测过 custom-stream 写入器（探测一次即可）
    _writer_probed: bool = False

    def emit_event(self, event: dict[str, Any]) -> None:
        """事件**产生即推送**：优先 custom stream，其次既有回调，都没有则丢弃。

        ⚠️ **custom stream 写入器只在"图运行上下文内"可调用**（它内部要读 LangGraph 的 config
        来定位检查点命名空间）。而 `run_once_graph` 的**失败兜底**（`_persist_failure` → `node_persist`）
        是在**图流已经结束之后**执行的——那时调用它必然抛
        `RuntimeError: Called get_config outside of a runnable context`。
        实测后果（问题 **P-033**）：题目会以 `E_EVAL_RUNNER` **0 秒失败**（看起来像模型答错），
        连"失败留痕"本身都写不下去。因此这里**兜住该异常并回落既有回调**：事件该发还是发，进程不能被带崩。
        """
        if self.stream_writer is None and not self._writer_probed:
            self._writer_probed = True
            self.stream_writer = _current_stream_writer()
        if self.stream_writer is not None:
            try:
                self.stream_writer(event)
                return
            except RuntimeError:
                # 图已结束 / 不在运行上下文 → 该写入器作废，改走既有 SSE 回调（并**记住**，避免每次重试）
                self.stream_writer = None
        if self.emit is not None:
            self.emit(event)

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


def _current_stream_writer() -> StreamWriter | None:
    """取当前 LangGraph 运行的 custom-stream 写入器（`stream_mode="custom"`）。

    不在图运行上下文时返回 `None`，由调用方回落既有 SSE 回调 —— 这样
    "直接用 `ainvoke` 跑图"（评测/测试）与"`astream(stream_mode='custom')` 跑图"（SSE）
    两种调用方式行为都正确。
    """
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:  # noqa: BLE001 —— 不在图运行上下文里
        return None
    return writer if callable(writer) else None


def context_from_config(config: Any) -> GraphContext:
    """从 `RunnableConfig` 取出运行时上下文（缺失即抛，属于接线错误）。"""
    ctx = (config or {}).get("configurable", {}).get("ctx")
    if not isinstance(ctx, GraphContext):
        raise RuntimeError("图节点缺少运行时上下文：config['configurable']['ctx'] 必须是 GraphContext")
    return ctx


# ── 状态 ⇄ 结果对象 ────────────────────────────────────────────────────────


def state_to_result(state: AnalysisState, ctx: GraphContext) -> AnalysisResult:
    """用 state 组装既有链路的 `AnalysisResult`（节点入口调用）。

    `events` 用 `EventSink(ctx.emit_event)`：既有链路上十几处 `events.append(...)` **一行不用改**
    即可继续真流式（保留 `EventSink` 的原始设计动机，见 ADR-018）。
    """
    result = AnalysisResult(
        record_code=state.get("record_code") or "",
        conversation_code=state.get("conversation_code") or "",
        report_code=state.get("report_code") or "",
        table_name=state.get("table_name") or "",
        question=state.get("question") or "",
        status=state.get("status") or "success",
        route=state.get("route") or "",
        retries=int(state.get("retries") or 0),
        path_switched=int(state.get("path_switched") or 0),
        events=EventSink(ctx.emit_event),
    )
    # 与 `result_to_state()` 对称：**所有可序列化字段都要还原**，
    # 否则图内部算对了、返回给调用方（端点/评测/重跑）的对象却是空的
    # （这正是"等价性测试"抓出来的第一个真问题）。
    result.slot = state.get("slot") or {}
    result.sql = state.get("sql")
    result.code = state.get("code")
    result.columns = list(state.get("columns") or [])
    result.rows = list(state.get("rows") or [])
    result.row_count = int(state.get("row_count") or 0)
    result.truncated = bool(state.get("truncated"))
    result.table_ready = bool(state.get("table_ready"))
    result.chart = state.get("chart")
    result.narrative = (state.get("narrative") or {}).get("text") or ""
    result.key_numbers = list(state.get("key_numbers") or [])
    result.anomalies = list(state.get("anomalies") or [])
    result.clarify = state.get("clarify")
    result.llm_model = state.get("llm_model") or ""
    result.llm_tokens = int(state.get("llm_tokens") or 0)
    result.elapsed_ms = int(state.get("elapsed_ms") or 0)
    # 失败信息必须一并还原：`error` 事件的 detail 行与 `analyses` 留痕都依赖这两项
    result.error_code = state.get("error_code")
    result.error_message = state.get("error_message")
    result.last_rule = state.get("last_rule")
    return result


def result_to_state(result: AnalysisResult) -> AnalysisState:
    """把 `AnalysisResult` 上**可序列化**的字段镜像回 state（节点出口调用）。"""
    return {
        "record_code": result.record_code or None,
        "report_code": result.report_code or None,
        "table_name": result.table_name or None,
        "status": result.status,  # type: ignore[typeddict-item]
        "route": result.route or "sql",  # type: ignore[typeddict-item]
        "sql": result.sql,
        "code": result.code,
        "slot": result.slot or {},
        "retries": int(result.retries or 0),
        "path_switched": int(result.path_switched or 0),
        "columns": list(result.columns or []),
        "rows": list(result.rows or []),
        "row_count": int(result.row_count or 0),
        "truncated": bool(result.truncated),
        "table_ready": bool(result.table_ready),
        "chart": result.chart,
        "narrative": result.narrative or {},
        "key_numbers": list(result.key_numbers or []),
        "anomalies": list(result.anomalies or []),
        "clarify": result.clarify,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "last_rule": getattr(result, "last_rule", None),
        "llm_model": result.llm_model,
        "llm_tokens": int(result.llm_tokens or 0),
        "elapsed_ms": int(result.elapsed_ms or 0),
    }


def result_to_result(**kwargs: Any) -> dict[str, Any]:
    """便捷构造 state 增量（仅用于节点里发"字段级"更新，避免整体覆盖）。"""
    return {k: v for k, v in kwargs.items() if v is not None}


# ── 留痕：calls 的两种形态 ────────────────────────────────────────────────


def calls_to_records(calls: list[tuple[str, Any, int]]) -> list[LLMCallRecord]:
    """`[(stage, LLMReply, is_retry)]` → 可随图状态持久化的纯数据列表。"""
    records: list[LLMCallRecord] = []
    for stage, reply, is_retry in calls:
        records.append(
            LLMCallRecord(
                stage=stage,
                model=getattr(reply, "model", "") or "",
                prompt_tokens=int(getattr(reply, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(reply, "completion_tokens", 0) or 0),
                total_tokens=int(getattr(reply, "prompt_tokens", 0) or 0)
                + int(getattr(reply, "completion_tokens", 0) or 0),
                elapsed_ms=int(getattr(reply, "elapsed_ms", 0) or 0),
                is_retry=int(is_retry or 0),
            )
        )
    return records
