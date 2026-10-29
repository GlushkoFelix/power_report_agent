"""`analyses` 表的查询映射（DDL 见 `01.code/scripts/sql/power_agent.sql` 5 节）。

命名说明（交付物 4 决策 5）：业务编号列由 `analysis_code` **统一改名为 `record_code`**，
与 PRD 9.1 的 URL 标识（`/analysis/records/{record_code}`）一致；值格式仍为 `a_000123`。
"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(Integer)
    report_id: Mapped[int | None] = mapped_column(Integer)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    slot_json: Mapped[str | None] = mapped_column(Text)
    #: sql / pandas（双工具路由，交付物 5 落地）
    route: Mapped[str | None] = mapped_column(Text)
    sql_text: Mapped[str | None] = mapped_column(Text)
    code_text: Mapped[str | None] = mapped_column(Text)
    result_columns_json: Mapped[str | None] = mapped_column(Text)
    #: 结果快照（≤200KB，超出截断并置 result_truncated=1）
    result_rows_json: Mapped[str | None] = mapped_column(Text)
    result_row_count: Mapped[int | None] = mapped_column(Integer)
    result_truncated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chart_json: Mapped[str | None] = mapped_column(Text)
    narrative: Mapped[str | None] = mapped_column(Text)
    key_numbers_json: Mapped[str | None] = mapped_column(Text)
    anomalies_json: Mapped[str | None] = mapped_column(Text)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    path_switched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: success / failed
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    llm_model: Mapped[str | None] = mapped_column(Text)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
