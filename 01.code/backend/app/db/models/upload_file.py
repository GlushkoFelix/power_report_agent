"""`upload_files` 表的查询映射（DDL 见 `01.code/scripts/sql/power_agent.sql` 4.6 节）。

与 `uploaded_reports` **一对一**（`report_id` UNIQUE）；`sha256` 供重复上传识别
（决策 5：命中即复用），`stored_path` 存**相对 `02.data/` 的路径**（PRD S5：路径不入库明文）。
"""

from __future__ import annotations

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class UploadFileRecord(Base):
    __tablename__ = "upload_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    origin_filename: Mapped[str] = mapped_column(Text, nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(Text)
    encoding: Mapped[str | None] = mapped_column(Text)
    delimiter: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[str] = mapped_column(Text, nullable=False)
    #: stored / parsed / failed
    status: Mapped[str] = mapped_column(Text, nullable=False, default="stored")
    error_message: Mapped[str | None] = mapped_column(Text)
