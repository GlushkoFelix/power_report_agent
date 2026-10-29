"""认证接口：注册 / 登录 / 退出 / 当前用户 + 个人中心四件事（对应 PRD 9.1 认证模块、F6.4）。

个人中心（本轮补齐，此前是"按钮置灰 + 后续版本"的缺口）：
`PATCH /auth/me` 编辑资料、`POST /auth/password` 修改密码、
`POST /auth/clear-records` 清空历史记录、`DELETE /auth/me` 删除账号。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.v1.deps import (
    CurrentUser,
    client_ip,
    client_user_agent,
    request_id,
)
from app.schemas.auth import (
    ClearRecordsIn,
    DeleteAccountIn,
    LoginIn,
    PasswordChangeIn,
    ProfileUpdateIn,
    RegisterIn,
    UserOut,
)
from app.schemas.common import ok
from app.services import auth_service

router = APIRouter(tags=["auth"])


@router.post("/auth/register", summary="用户注册（成功即自动登录并签发 JWT）")
async def register(payload: RegisterIn, request: Request) -> dict[str, Any]:
    """注册并自动登录，直接返回 token（决策）。"""
    result = await auth_service.register(
        payload, ip=client_ip(request), user_agent=client_user_agent(request)
    )
    return ok(result.model_dump(), "注册成功", request_id(request))


@router.post("/auth/login", summary="用户登录（连续 5 次失败锁定 5 分钟）")
async def login(payload: LoginIn, request: Request) -> dict[str, Any]:
    result = await auth_service.login(
        payload, ip=client_ip(request), user_agent=client_user_agent(request)
    )
    return ok(result.model_dump(), "登录成功", request_id(request))


@router.post("/auth/logout", summary="退出登录（前端清 token，服务端落审计）")
async def logout(request: Request, user: CurrentUser) -> dict[str, Any]:
    """退出登录：前端清除 token；服务端落一条审计（PRD F6.2）。"""
    await auth_service.logout(
        user.id, ip=client_ip(request), user_agent=client_user_agent(request)
    )
    return ok(None, "已退出登录", request_id(request))


@router.get("/auth/me", summary="当前登录用户信息")
async def me(request: Request, user: CurrentUser) -> dict[str, Any]:
    return ok(UserOut.model_validate(user).model_dump(), request_id=request_id(request))


# ── 个人中心（PRD F6.4）────────────────────────────────────────────────────


@router.patch("/auth/me", summary="编辑资料（显示名 / 邮箱）")
async def update_me(
    payload: ProfileUpdateIn, request: Request, user: CurrentUser
) -> dict[str, Any]:
    """编辑资料：只改显示名 / 邮箱（用户名不可改）。未提交的字段保持原值。"""
    result = await auth_service.update_profile(
        user.id, payload, ip=client_ip(request), user_agent=client_user_agent(request)
    )
    return ok(result.model_dump(), "资料已更新", request_id(request))


@router.post("/auth/password", summary="修改密码（需校验当前密码）")
async def change_password(
    payload: PasswordChangeIn, request: Request, user: CurrentUser
) -> dict[str, Any]:
    """修改密码：需提供当前密码；改密后已签发的 token 仍然有效（JWT 无服务端会话）。"""
    await auth_service.change_password(
        user.id, payload, ip=client_ip(request), user_agent=client_user_agent(request)
    )
    return ok(None, "密码已修改（其它设备上的登录不会自动退出）", request_id(request))


@router.post("/auth/clear-records", summary="清空历史记录（records=只清记录 / all=连会话对话）")
async def clear_records(
    payload: ClearRecordsIn, request: Request, user: CurrentUser
) -> dict[str, Any]:
    """清空历史记录：`records` 只清分析记录（对话文本保留）；`all` 连会话与对话一起清。"""
    result = await auth_service.clear_records(
        user.id, payload, ip=client_ip(request), user_agent=client_user_agent(request)
    )
    scope_text = "分析记录" if result.scope == "records" else "会话与对话历史"
    return ok(
        result.model_dump(),
        f"已清空{scope_text}（{result.deleted_records} 条记录、"
        f"{result.deleted_conversations} 个会话）",
        request_id(request),
    )


@router.delete("/auth/me", summary="删除账号（密码 + 用户名双确认，不可撤销）")
async def delete_me(
    payload: DeleteAccountIn, request: Request, user: CurrentUser
) -> dict[str, Any]:
    """删除账号（不可撤销）：密码 + 用户名双确认；安全日志保留（`user_id` 置空）。"""
    result = await auth_service.delete_account(
        user.id, payload, ip=client_ip(request), user_agent=client_user_agent(request)
    )
    return ok(
        result.model_dump(),
        f"账号 {result.username} 已删除（安全日志按合规保留 {result.kept_audit_logs} 条）",
        request_id(request),
    )
