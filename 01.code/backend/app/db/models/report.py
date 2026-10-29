"""`uploaded_reports` 表的查询映射（DDL 见 `01.code/scripts/sql/power_agent.sql` 4.2 节）。

语义要点（本次决策）：
- 预置 5 张报表 `user_id IS NULL`，**所有用户共享可见**；
- `is_active = 1` 表示**系统默认活跃报表**（仅预置中一张，由 `_common.ACTIVE_REPORT_CODE` 控制）；
  会话内的活跃报表以 `conversations.active_report_id` 为唯一权威。
"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class UploadedReport(Base):
    __tablename__ = "uploaded_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[int | None] = mapped_column(Integer)  # 预置报表为 NULL
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    table_name: Mapped[str] = mapped_column(Text, nullable=False)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int | None] = mapped_column(Integer)
    column_count: Mapped[int | None] = mapped_column(Integer)
    time_start: Mapped[str | None] = mapped_column(Text)
    time_end: Mapped[str | None] = mapped_column(Text)
    caliber_version: Mapped[str | None] = mapped_column(Text)
    is_preset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ready")
    schema_json: Mapped[str | None] = mapped_column(Text)
    quality_json: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
