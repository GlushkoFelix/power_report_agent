"""分析模块的请求/响应模型（PRD 9.2）。

`POST /analysis/chat`（SSE 流式）+ `POST /analysis/clarify`（澄清回填后就地继续，SSE）；
`/analysis/records*` 见 `analysis_record.py`。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatIn(BaseModel):
    """`POST /analysis/chat` 请求体（PRD 9.2）。"""

    conversation_code: str = Field(description="会话编号，如 c_20260915_0001")
    #: 可选：显式指定报表；与会话当前活跃报表不同则**先切换再分析**（PRD F1.6）
    report_code: str | None = Field(default=None, description="可选：本次分析使用的报表")
    message: str = Field(description="用户问题", min_length=1, max_length=2000)
    #: 可选：条件芯片编辑后的回填（UIUX 6.3「编辑后重算」）；与 clarify 共用同一套
    #: 校验（键白名单 + 类型/长度），先合入会话 slot 再做本轮增量抽取（决策 20-1）
    slot_patch: dict[str, Any] | None = Field(default=None, description="可选：slot 回填")


class ClarifyIn(BaseModel):
    """`POST /analysis/clarify` 请求体（决策 18-3 / 19-3：回填后就地继续执行，返回 SSE）。

    `slot_patch` 的键只能是 8 个 slot 字段（`missing_slot` 不入请求体——澄清卡在前端手里，
    回显用它）；值校验为键白名单 + 类型/长度，不做值域校验。
    """

    conversation_code: str = Field(description="会话编号")
    question: str = Field(description="被澄清的原始问题（前端从澄清卡上下文回传）",
                          min_length=1, max_length=2000)
    slot_patch: dict[str, Any] = Field(description="澄清回填，如 {\"metric\": \"峰段用电量\"}")
