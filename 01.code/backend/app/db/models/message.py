"""`messages` 表的查询映射（DDL 见 `01.code/scripts/sql/power_agent.sql` 4.4 节）。

`seq_no` 为会话内顺序号（从 1 开始），与 `conversation_id` 组成唯一约束——
消息历史的游标分页（`?before_seq=`）直接命中该索引。
"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)  # user / assistant / system
    content: Mapped[str | None] = mapped_column(Text)
    analysis_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
