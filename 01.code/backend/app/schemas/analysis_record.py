"""分析记录接口的响应模型（PRD 9.1：记录列表 / 详情 / 重跑 / 删除；UIUX 5.4 区域 3）。

分页与筛选（本轮决策 1）：`offset` 分页 + `total`（antd Table 原生适配），
排序固定 `created_at DESC, id DESC`（id 做次键，避免同一秒内翻页错位）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RecordItem(BaseModel):
    """列表项（**轻字段**：不含 SQL 与解读正文，避免列表响应膨胀）。"""

    record_code: str
    question: str
    conversation_code: str | None = None
    report_code: str | None = None
    report_filename: str | None = None
    route: str | None = None
    status: str
    error_code: str | None = None
    elapsed_ms: int | None = None
    llm_tokens: int | None = None
    llm_model: str | None = None
    created_at: str


class RecordListOut(BaseModel):
    total: int
    page: int
    page_size: int
    #: 当前筛选条件下的成功/失败计数（便于个人中心直接展示）
    status_count: dict[str, int] = Field(default_factory=dict)
    items: list[RecordItem] = Field(default_factory=list)


class LlmCallOut(BaseModel):
    """单次模型调用流水（详情页「成本与耗时」区块）。"""

    stage: str
    model_name: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    elapsed_ms: int | None = None
    status: str


class ResultOut(BaseModel):
    """执行结果快照（未执行过时 `columns`/`rows` 为空、`row_count` 为 None）。"""

    columns: list[dict[str, Any]] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int | None = None
    truncated: bool = False


class RecordDetailOut(BaseModel):
    """详情（决策 3：全量 + `llm_calls` 明细）。"""

    record_code: str
    question: str
    conversation_code: str | None = None
    report_code: str | None = None
    report_filename: str | None = None
    route: str | None = None
    status: str
    error_code: str | None = None
    error_message: str | None = None
    llm_model: str | None = None
    elapsed_ms: int | None = None
    prompt_version: str | None = None
    schema_version: str | None = None
    #: F2.4 自愈重试次数与是否换过路（前端「执行信息」区块回放用；SSE 的 `meta` 里也有）
    retries: int = 0
    path_switched: bool = False
    created_at: str
    #: 查询条件（PRD F2.1 的 8 个字段）
    slot: dict[str, Any] = Field(default_factory=dict)
    #: **实际执行的 SQL**（含守卫补的 LIMIT；展示即所执行）
    sql: str | None = None
    code_text: str | None = None
    result: ResultOut
    narrative: str | None = None
    key_numbers: list[dict[str, Any]] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    #: 图表（图表批次接入后填充）
    chart: dict[str, Any] | None = None
    llm_calls: list[LlmCallOut] = Field(default_factory=list)
    llm_tokens: int = 0
    #: 是否可重跑：原报表仍存在且为 `ready`（否则前端置灰重跑按钮）
    can_replay: bool = False
    #: 重跑时回显来源记录编号（血缘）
    replayed_from: str | None = None
    #: 重跑的落地说明（v1.19.12）：例如原记录属**只读的评测批次会话**时，重跑会另起新会话执行
    replay_note: str | None = None


class RecordDeleteOut(BaseModel):
    """删除记录的回显（级联影响的条数，便于前端提示）。"""

    record_code: str
    deleted_messages: int = 0
    deleted_llm_calls: int = 0
