"""`llm_calls` 表的查询映射（DDL 见 `01.code/scripts/sql/power_agent.sql` 7 节）。

一次问答会产生多条流水（`stage` = `slot` / `gen_sql` / `narrative` / `retry` / `clarify` …），
供 N1/N2 性能统计、评测的 token 成本核算与限流调优（PRD 7.4 / 10.4）。
"""

from __future__ import annotations

from sqlalchemy import Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int | None] = mapped_column(Integer)
    eval_run_id: Mapped[int | None] = mapped_column(Integer)
    #: slot / gen_sql / gen_pandas / retry / narrative / clarify
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    first_token_ms: Mapped[int | None] = mapped_column(Integer)
    is_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer)
    #: success / failed
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
