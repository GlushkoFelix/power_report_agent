"""公共依赖：JWT 鉴权与请求元信息（PRD 9.1「除注册/登录/健康检查外均需 JWT」）。

用法：在端点签名中写 `user: CurrentUser` 即完成鉴权，未携带/携带无效 token 时抛 401 `E_AUTH`。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import unauthorized
from app.core.security import decode_access_token
from app.db.models import User
from app.services import auth_service

#: `auto_error=False`：由我们自己抛业务异常，保证错误响应也是统一包络
_bearer = HTTPBearer(auto_error=False)


def request_id(request: Request) -> str:
    """当前请求的 request_id（由 `RequestIdMiddleware` 注入）。"""
    return getattr(request.state, "request_id", "-")


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def client_user_agent(request: Request) -> str | None:
    return request.headers.get("User-Agent")


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise unauthorized("请先登录")
    claims = decode_access_token(credentials.credentials)
    user_id = int(claims.get("sub", 0))
    user = await auth_service.get_user_by_id(user_id)
    if user is None or not user.is_active:
        raise unauthorized("账号不存在或已停用，请重新登录")
    request.state.user_id = user.id
    return user


#: 端点签名里直接用 `user: CurrentUser`
CurrentUser = Annotated[User, Depends(get_current_user)]
