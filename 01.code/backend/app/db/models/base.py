"""ORM 基类。

**纪律**：`01.code/scripts/sql/*.sql` 是表结构的唯一事实源；本目录的 models 只做
**查询映射**，严禁调用 `Base.metadata.create_all()`（对应《数据库设计文档》2.2 设计原则 2）。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 model 的声明式基类。"""
