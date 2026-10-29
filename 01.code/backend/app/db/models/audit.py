"""`audit_logs` 表的查询映射（DDL 见 `01.code/scripts/sql/power_agent.sql` 4.8 节）。

`event_type` 白名单（**17 个**，由 DDL 的 CHECK 强制；扩展会需要重建表，见 `05_migrate_audit_events.py`）：
- 安全拦截类：`sql_blocked` / `code_blocked` / `injection_attempt` / `auth_forbidden` / `cascade_delete`
- 认证审计类：`auth_register` / `auth_login` / `auth_login_failed` / `auth_locked` / `auth_logout`
- 质量告警类：`narrative_number_warn`（解读出现未授权数字）
- 个人中心类（v1.19.12）：`auth_profile_updated` / `auth_password_changed` /
  `auth_password_change_failed` / `profile_records_cleared` /
  `auth_account_deleted` / `auth_delete_account_failed`

**注意**：`action` 也是白名单（`blocked` / `warned` / `allowed`）——别再写 `flagged` 这类值，
否则插入会被 CHECK 拒绝（v1.19.6 的告警曾因此静默失败，v1.19.12 修正为 `warned`）。

**注意**：`payload` 严禁写入明文密码或 token（PRD S5 日志脱敏）。

**注意**：`payload` 严禁写入明文密码或 token（PRD S5 日志脱敏）。
"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer)
    conversation_id: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="medium")
    rule_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
