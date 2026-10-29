"""智电析报 · 密码哈希与 JWT 签发/校验。

对应文档：PRD F6.1（bcrypt 加盐哈希）、F6.2（JWT 有效期 24h）、S4（JWT 鉴权）、
          PRD 10.3（依赖为 bcrypt 直调 + PyJWT）、数据库设计 5.1（password_hash 60 字符）。

实现要点：
- **直调 bcrypt**（不再用 passlib）：`hashpw` / `checkpw`，哈希固定 60 字符；
- bcrypt 对明文有 **72 字节**硬限制（超长直接抛 `ValueError`），本模块显式校验并返回业务异常，
  不静默截断（截断会让不同长密码等价）；
- JWT 用 HS256，`sub` 存用户 ID，另带 `username` / `iat` / `exp` / `jti`（`jti` 便于日后审计与失效扩展）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import get_settings
from app.core.errors import BizError, E_AUTH, validation_error

#: bcrypt 明文硬限制（字节）
BCRYPT_MAX_BYTES = 72


def hash_password(plain: str) -> str:
    """生成 bcrypt 哈希（60 字符）。"""
    raw = plain.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise validation_error(f"密码过长（{len(raw)} 字节，上限 {BCRYPT_MAX_BYTES} 字节）")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, password_hash: str) -> bool:
    """校验密码。任何异常（哈希损坏、超长明文等）一律视为不匹配，不向上抛。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def create_access_token(user_id: int, username: str) -> tuple[str, datetime]:
    """签发 access token，返回（token, 过期时间 UTC）。"""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=settings.jwt_expire_hours)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str) -> dict:
    """校验并解析 token；失败统一抛 401 `E_AUTH`。"""
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise BizError(401, E_AUTH, "登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise BizError(401, E_AUTH, "登录状态无效，请重新登录") from exc
