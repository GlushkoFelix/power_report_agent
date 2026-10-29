"""会话接口（对应 PRD 9.1 会话模块）。

`POST /conversations`、`GET /conversations`、`GET/PATCH/DELETE /conversations/{conversation_code}`、
`GET /conversations/{conversation_code}/messages`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from app.api.v1.deps import CurrentUser, client_ip, client_user_agent, request_id
from app.schemas.common import ok
from app.schemas.conversation import ConversationCreateIn, ConversationPatchIn
from app.services import conversation_service

router = APIRouter(tags=["conversations"])


@router.post("/conversations", summary="新建会话（可指定初始数据源）")
async def create_conversation(
    payload: ConversationCreateIn, request: Request, user: CurrentUser
) -> dict[str, Any]:
    """新建会话。活跃报表按三级回落推导（显式 > 最近会话 > 系统默认），响应回显来源。"""
    result = await conversation_service.create_conversation(user.id, payload)
    return ok(result.model_dump(), request_id=request_id(request))


@router.get("/conversations", summary="会话列表（关键词 + 时间筛选）")
async def list_conversations(
    request: Request,
    user: CurrentUser,
    q: str | None = Query(default=None, description="标题关键词模糊筛选"),
    date_from: str | None = Query(default=None, description="更新时间下界（YYYY-MM-DD）"),
    date_to: str | None = Query(default=None, description="更新时间上界（YYYY-MM-DD）"),
) -> dict[str, Any]:
    """会话列表：置顶组在前，其余按更新时间倒序（不分页，与报表列表一致）。"""
    result = await conversation_service.list_conversations(
        user.id, keyword=q, date_from=date_from, date_to=date_to
    )
    return ok(result.model_dump(), request_id=request_id(request))


@router.get("/conversations/{conversation_code}", summary="会话详情")
async def get_conversation(
    conversation_code: str, request: Request, user: CurrentUser
) -> dict[str, Any]:
    """会话详情（含结构化 slot）。"""
    result = await conversation_service.get_conversation(user.id, conversation_code)
    return ok(result.model_dump(), request_id=request_id(request))


@router.patch("/conversations/{conversation_code}", summary="重命名 / 置顶会话")
async def patch_conversation(
    conversation_code: str,
    payload: ConversationPatchIn,
    request: Request,
    user: CurrentUser,
) -> dict[str, Any]:
    """重命名 / 置顶 / 切换活跃报表（与 `POST /reports/{report_code}/activate` 等价）。"""
    result = await conversation_service.patch_conversation(user.id, conversation_code, payload)
    return ok(result.model_dump(), request_id=request_id(request))


@router.delete("/conversations/{conversation_code}", summary="删除会话（级联对话与分析记录）")
async def delete_conversation(
    conversation_code: str, request: Request, user: CurrentUser
) -> dict[str, Any]:
    """删除会话（消息与分析记录级联清理，安全日志保留）。"""
    result = await conversation_service.delete_conversation(
        user.id,
        conversation_code,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
    )
    return ok(result, "会话已删除", request_id(request))


@router.get("/conversations/{conversation_code}/messages", summary="消息历史（游标分页）")
async def list_messages(
    conversation_code: str,
    request: Request,
    user: CurrentUser,
    limit: int | None = Query(default=None, description="返回条数，默认 50，上限 200"),
    before_seq: int | None = Query(default=None, description="取序号小于该值的更早消息（向历史翻页）"),
) -> dict[str, Any]:
    """消息历史（游标式：默认最近 50 条）。"""
    result = await conversation_service.list_messages(
        user.id, conversation_code, limit=limit, before_seq=before_seq
    )
    return ok(result.model_dump(), request_id=request_id(request))
