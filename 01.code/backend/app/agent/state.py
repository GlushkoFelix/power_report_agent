"""LangGraph 编排层的状态定义（**只放可序列化字段**）。

设计口径（对应《系统架构设计文档》ADR-002 / ADR-017 的接入方案）：

1. **state 里只放纯数据** —— ORM 对象（`UploadedReport` / `Conversation`）、LLM 客户端、
   SSE 推送回调等运行时依赖一律走 `RunnableConfig.configurable`，不进 state；
   否则 `SqliteSaver` 无法持久化，且图状态不可回放（这是 ADR-017 阶段 3 的前提）。
2. **与 `AnalysisResult` 的适配放在 `app/agent/adapters.py`** —— 阶段 1 的节点内部仍复用
   `analysis_service` 的既有函数（保住 303 用例与既有行为），适配层负责两者的来回转换；
   阶段 2 起把「重写环」「形态自检环」拆成真正的条件边后，逐步去掉该适配层。
3. **字段名对齐既有语义**：`route` / `retries` / `path_switched` / `status` 等与
   `AnalysisResult`、`analyses` 表列名保持一致，便于留痕与逐题对照。

事件推送不落在 state 里：节点通过 `langgraph.config.get_stream_writer()` 直接写
`stream_mode="custom"` 通道，保持「事件产生即推送」（首字 10–16ms 不得退化，见 ADR-018）。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Route = Literal["sql", "pandas"]
Status = Literal["running", "success", "clarify", "failed"]
SlotSource = Literal["preset", "patch", "fastpath", "llm", "fallback"]


class LLMCallRecord(TypedDict, total=False):
    """一次模型调用的留痕记录（对应 `llm_calls` 表的轻量投影）。

    字段与 `analysis_service.calls` 的元组一致：`(stage, LLMReply, is_retry)` 展开而来，
    但**序列化为纯数据**，以便随图状态一起被 checkpointer 持久化。
    """

    stage: str            # slot | gen_sql | gen_code | narrative
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    elapsed_ms: int
    is_retry: int


class AnalysisState(TypedDict, total=False):
    """一轮问答的图状态（节点返回**增量**，LangGraph 按 key 合并）。"""

    # ── 入参（跨节点只读）──────────────────────────────────────────
    user_id: int
    conversation_code: str
    question: str
    report_code: str | None
    switch_active_report: bool
    slot_patch: dict[str, Any] | None
    slot_base: dict[str, Any] | None
    preset_slot: dict[str, Any] | None
    include_user_message: bool
    ip: str | None
    user_agent: str | None
    request_id: str | None

    # ── 会话与报表（ORM 取值的纯数据副本）──────────────────────────
    schema_text: str
    schema: dict[str, Any]
    table_name: str
    report_filename: str
    #: 质量报告原始 JSON 文本（`_quality_hint()` 的入参形态，故不解析成 dict）
    quality_json: str | None
    allowed_tables: list[str]
    conversation_title: str
    slot_base_from_conversation: dict[str, Any]

    # ── slot 阶段 ─────────────────────────────────────────────────
    slot: dict[str, Any]
    slot_missing: str | None
    slot_source: SlotSource

    # ── 生成 / 守卫 / 执行 ────────────────────────────────────────
    route: Route
    sql: str | None
    code: str | None
    guard_rule: str | None
    guard_reason: str | None
    limit_applied: bool
    null_guarded: list[str]

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    table_ready: bool

    # ── 图表 / 解读 ───────────────────────────────────────────────
    chart: dict[str, Any] | None
    narrative: dict[str, Any]
    key_numbers: list[Any]
    anomalies: list[str]

    # ── 环控制（阶段 2 起由条件边驱动）────────────────────────────
    retries: int                 # 生成调用已发生次数（上限 3：首次 + 重写 1 + 换路 1）
    path_switched: int           # 是否已换路过（0/1）
    rewrite_reason: str | None   # 带原因重写时回灌的错误/形态说明
    shape_issue: str | None      # 形态自检命中说明（rule_id + 描述）
    blocked: bool                # 安全拦截（E_SQL_FORBIDDEN / E_CODE_FORBIDDEN）→ 不重试不换路

    # ── 失败与自愈（阶段 2：重写环拆成条件边后的显式载体）──────────
    #: 最近一次失败（守卫拦截 / 执行报错 / 形态自检），由 `rewrite` 节点读取
    last_error_code: str | None
    last_error_message: str | None
    last_error_data: dict[str, Any] | None
    #: 重写环额度用尽（首次 + 带错误重写 1 + 换路 1）→ 直接走留痕失败
    retry_exhausted: bool
    #: 本轮守卫是否拦截（**每次守卫都会显式赋值**）。
    #: 不能拿 `last_error_code` 当判断依据：重写成功后它仍是上一次的旧值，
    #: 会让 `route_after_guard` 误判成"又失败了"而跳过执行（阶段 2 等价性用例抓到的真 bug）。
    guard_blocked: bool

    # ── 留痕与统计 ────────────────────────────────────────────────
    calls: list[LLMCallRecord]
    llm_model: str
    llm_tokens: int
    elapsed_ms: int

    # ── HITL：澄清中断与恢复（阶段 4）───────────────────────────────
    #: `clarify_wait` 节点里 `interrupt()` 的返回值（用户回填的 slot 补丁）。
    #: 恢复时该节点会重跑，读到它后继续 → `extract_slot` 用它合并出完整 slot（跳过二次抽取）。
    clarify_resume: dict[str, Any] | None

    # ── 输出 ─────────────────────────────────────────────────────
    status: Status
    clarify: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    #: 守卫/沙箱拦截命中规则（进 `error` 事件的 detail 行）
    last_rule: str | None
    record_code: str | None
    narrative_warning: list[str]


#: 一次请求的初始状态。
#:
#: **它同时是本轮的"重置清单"**（见 `agent/checkpointer.py::per_turn_reset()`）：
#: 引入检查点后图状态会**跨轮累积**，凡是"只属于本轮"的字段都必须在这里显式给出初始值，
#: 否则上一轮的表格 / 失败信息 / 澄清卡会顺着检查点漏到下一轮
#: （与"评测跨题串条件"是同一类坑，见 `大修过程中的问题和难点` P-012）。
#: 入参（`user_id` / `conversation_code` / `question` / `report_code` / `slot_patch` …）
#: 由调用方每轮显式传入，`run_once_graph` 会整体覆盖，不依赖这里的默认值。
#:
#: 注：`slot_source` 只用于观测（谁产出了 slot），不参与任何分支判断，故不重置。
DEFAULT_STATE: AnalysisState = {
    # 入参兜底（调用方每轮覆盖）
    "switch_active_report": True,
    "include_user_message": True,
    "slot_patch": None,
    "slot_base": None,
    "preset_slot": None,
    "ip": None,
    "user_agent": None,
    "request_id": None,
    # 会话与报表（每轮由 inject_schema 重新注入）
    "schema_text": "",
    "schema": {},
    "table_name": "",
    "report_filename": "",
    "quality_json": None,
    "allowed_tables": [],
    "conversation_title": "",
    "slot_base_from_conversation": {},
    # slot
    "slot": {},
    "slot_missing": None,
    # 生成 / 守卫 / 执行
    "route": "sql",
    "sql": None,
    "code": None,
    "limit_applied": False,
    "null_guarded": [],
    "columns": [],
    "rows": [],
    "row_count": 0,
    "truncated": False,
    "table_ready": False,
    # 图表 / 解读
    "chart": None,
    "narrative": {},
    "key_numbers": [],
    "anomalies": [],
    # 环控制与失败
    "retries": 0,
    "path_switched": 0,
    "blocked": False,
    "rewrite_reason": None,
    "shape_issue": None,
    "last_error_code": None,
    "last_error_message": None,
    "last_error_data": None,
    "retry_exhausted": False,
    "guard_blocked": False,
    # 留痕与统计
    "calls": [],
    "llm_model": "",
    "llm_tokens": 0,
    "elapsed_ms": 0,
    # 输出
    "status": "running",
    "clarify": None,
    "clarify_resume": None,
    "error_code": None,
    "error_message": None,
    "last_rule": None,
    "record_code": None,
    "narrative_warning": [],
}
