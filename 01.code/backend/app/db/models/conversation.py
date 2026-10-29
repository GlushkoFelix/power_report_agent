"""`conversations` 表的查询映射（DDL 见 `01.code/scripts/sql/power_agent.sql` 4.3 节）。

要点：
- `active_report_id` 是**会话级活跃报表的唯一权威**（决策）；
- `is_pinned` + `pinned_at` 支撑置顶分组（每用户最多 5 条，超出自动挤出最早置顶）；
- `slot_json` 存结构化查询条件（时间/粒度/指标/过滤/对比），**不混入 UI 状态**。
"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)  # 默认取首问摘要（由分析模块写入）
    active_report_id: Mapped[int | None] = mapped_column(Integer)
    slot_json: Mapped[str | None] = mapped_column(Text)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_message_at: Mapped[str | None] = mapped_column(Text)
    is_pinned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pinned_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
