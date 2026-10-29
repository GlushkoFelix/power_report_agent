"""会话相关请求/响应模型（PRD 9.1 会话模块、F6.3 会话与记录管理）。

URL 标识统一用业务编号 `conversation_code`（如 `c_20260915_0001`）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_code: str
    title: str | None = None
    active_report_id: int | None = None
    #: 会话级活跃报表的业务编号（由 `active_report_id` 解析而来）
    active_report_code: str | None = None
    is_pinned: int
    pinned_at: str | None = None
    #: 消息数：列表接口用一次分组 COUNT 实时计算（不依赖 `message_count` 快照列）
    message_count: int
    last_message_at: str | None = None
    created_at: str
    updated_at: str


class ConversationListOut(BaseModel):
    total: int
    items: list[ConversationOut]


class ConversationDetailOut(ConversationOut):
    #: 结构化查询条件（`slot_json` 解析结果；分析模块负责写入）
    slot: dict[str, Any] = Field(default_factory=dict)


class ConversationCreateIn(BaseModel):
    title: str | None = Field(default=None, max_length=60)
    #: 不传则按「三级回落」推导（决策）：显式 > 该用户最近会话 > 系统默认
    active_report_code: str | None = None


class ConversationCreateOut(ConversationOut):
    #: 活跃报表来源：explicit / last_conversation / system_default
    active_report_source: str


class ConversationPatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=60)
    active_report_code: str | None = None
    #: 置顶开关（每用户最多 5 条，超出自动挤出最早的置顶）
    is_pinned: bool | None = None


class ConversationPatchOut(ConversationOut):
    #: 因置顶超出 5 条而被自动取消置顶的会话编号（正常情况为 None）
    evicted_pinned_code: str | None = None


class ActivateReportIn(BaseModel):
    """`POST /reports/{report_code}/activate` 的请求体（报表视角的语法糖）。"""

    conversation_code: str


class ActivateReportOut(BaseModel):
    conversation_code: str
    active_report_code: str


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    seq_no: int
    role: str
    content: str | None = None
    analysis_id: int | None = None
    #: 该条助手消息对应的**分析记录编号**（由 `analysis_id` 解析而来）。
    #: 前端"刷新后还原结果"用它调 `GET /analysis/records/{record_code}`，
    #: 把表格、图表（口径标记在 figure 的 `layout.meta` 里）与执行信息一并回放——
    #: 只给 `analysis_id` 数字无法反查业务编号，故在这里补上（历史文本仍照旧渲染）。
    record_code: str | None = None
    created_at: str


class MessageListOut(BaseModel):
    """游标式消息历史（决策）：默认最近 50 条，`before_seq` 向历史翻页。"""

    conversation_code: str
    items: list[MessageOut]
    limit: int
    has_more: bool
    oldest_seq: int | None = None
    newest_seq: int | None = None
