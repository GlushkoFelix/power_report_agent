"""认证相关请求/响应模型（PRD F6.1 / F6.2 / F6.4）。

校验规则（本次决策）：
- 用户名：`^[A-Za-z0-9_]{4,20}$`，**不区分大小写**（服务端统一转小写存储与比对）；
- 密码：8–64 字符，必须同时含字母与数字，且 UTF-8 编码后不超过 **72 字节**（bcrypt 硬限制）；
- 邮箱：轻量格式校验（含 `@` 与域名点号即可），不引入 `email-validator` 依赖。

个人中心（PRD F6.4）：编辑资料 / 修改密码 / 清空历史记录 / 删除账号 四个入参都在本模块，
**密码类字段一律不落审计、不落日志**（PRD S5）。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

USERNAME_PATTERN = r"^[A-Za-z0-9_]{4,20}$"
BCRYPT_MAX_BYTES = 72
#: 轻量邮箱校验：本地部@域名.顶级（够用即可，不做 RFC 5322 完整实现）
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def validate_password_strength(value: str) -> str:
    """密码强度（注册与改密**共用同一套规则**，避免两处口径漂移）。"""
    if not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
        raise ValueError("密码必须同时包含字母与数字")
    if len(value.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise ValueError(f"密码过长（UTF-8 编码后不得超过 {BCRYPT_MAX_BYTES} 字节）")
    return value


class RegisterIn(BaseModel):
    username: str = Field(..., pattern=USERNAME_PATTERN, description="4–20 位字母/数字/下划线")
    password: str = Field(..., min_length=8, max_length=64, description="≥8 位，含字母与数字")
    display_name: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=64)

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return validate_password_strength(value)


class LoginIn(BaseModel):
    username: str = Field(..., min_length=4, max_length=20)
    password: str = Field(..., min_length=1, max_length=64)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str | None = None
    email: str | None = None
    last_login_at: str | None = None
    created_at: str


class TokenOut(BaseModel):
    """注册与登录的统一返回（注册即自动登录）。"""

    token: str
    token_type: str = "Bearer"
    expires_at: str  # ISO8601（UTC）
    user: UserOut


# ── 个人中心（PRD F6.4）────────────────────────────────────────────────────


class ProfileUpdateIn(BaseModel):
    """编辑资料：**只允许改显示名与邮箱**（用户名是账号标识，不可改）。

    未提交的字段保持原值 —— 用 `model_fields_set` 区分"没传"与"显式传 null（清空）"。
    """

    display_name: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=64)

    @field_validator("display_name", "email", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.match(EMAIL_PATTERN, value):
            raise ValueError("邮箱格式不正确（示例：name@example.com）")
        return value


class PasswordChangeIn(BaseModel):
    """修改密码：需提供当前密码（二次确认身份），新密码与注册同一套强度规则。"""

    old_password: str = Field(..., min_length=1, max_length=64, description="当前密码")
    new_password: str = Field(..., min_length=8, max_length=64, description="新密码：≥8 位含字母与数字")

    @field_validator("new_password")
    @classmethod
    def _validate_new_password(cls, value: str) -> str:
        return validate_password_strength(value)


class ClearRecordsIn(BaseModel):
    """清空历史记录（危险操作，前端必须二次确认后提交）。

    - `records`：只清**分析记录**（SQL/结果/解读），会话与对话文本保留（与"逐条删除"一致）；
    - `all`：连同**会话与对话历史**一起清空（置顶会话也在内）。
    """

    scope: Literal["records", "all"] = "records"


class ClearRecordsOut(BaseModel):
    """清空结果回显（如实报告清掉了多少，便于用户核对）。"""

    scope: str
    deleted_records: int = 0
    deleted_conversations: int = 0
    deleted_messages: int = 0
    deleted_llm_calls: int = 0


class DeleteAccountIn(BaseModel):
    """删除账号（不可撤销）：**密码 + 用户名双确认**，防误触。"""

    password: str = Field(..., min_length=1, max_length=64, description="当前密码")
    confirm_username: str = Field(..., min_length=1, max_length=20, description="再次输入用户名")


class DeleteAccountOut(BaseModel):
    """删除结果回显：清掉了什么、保留了什么（安全日志按合规保留）。"""

    username: str
    deleted_reports: int = 0
    deleted_conversations: int = 0
    deleted_records: int = 0
    deleted_eval_runs: int = 0
    kept_audit_logs: int = 0
