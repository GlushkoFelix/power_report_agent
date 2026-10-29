"""审计日志写入的统一入口（认证事件与安全事件共用）。

**禁止**把明文密码、token、文件绝对路径写入 `payload`（PRD S5 日志脱敏）。
"""

from __future__ import annotations

from datetime import datetime

from app.db.models import AuditLog

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def now_text() -> str:
    """与库内 `datetime('now','localtime')` 一致的时间文本（可直接字符串比较）。"""
    return datetime.now().strftime(TIME_FMT)


def build_audit(
    event_type: str,
    severity: str,
    action: str,
    *,
    user_id: int | None = None,
    conversation_id: int | None = None,
    rule_id: str | None = None,
    payload: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditLog:
    """构造审计行（由调用方 `session.add(...)` 并提交）。"""
    return AuditLog(
        user_id=user_id,
        conversation_id=conversation_id,
        event_type=event_type,
        severity=severity,
        rule_id=rule_id,
        action=action,
        payload=payload,
        ip=ip,
        user_agent=user_agent,
        created_at=now_text(),
    )
