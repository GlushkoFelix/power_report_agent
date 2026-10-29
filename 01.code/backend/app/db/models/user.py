"""`users` 表的查询映射（DDL 见 `01.code/scripts/sql/power_agent.sql` 4.1 节）。"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 用户名统一小写存储与比对（决策：大小写不敏感，避免“看着一样实际两个号”）
    username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)  # bcrypt，固定 60 字符
    display_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    login_fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[str | None] = mapped_column(Text)  # 'YYYY-MM-DD HH:MM:SS' 本地时间
    last_login_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
