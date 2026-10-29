"""智电析报 · 两库连接工厂（控制面读写 / 数据面只读 / 数据面受控写）。

对应文档：
- 《系统架构设计文档》5.2 两库分离、5.3 只读的架构级保证（连接层与表名层）
- 《数据库设计文档》3.1 访问模式表、S1「报表库只读打开」、4.6 动态表写入方、6.3 外键（连接级）
- PRD 7.4 / S8 数据库职责与访问隔离

四条铁律：
1. **两库分离**：两个库各自持有连接，**不跨库 JOIN、不混用连接**；
2. **数据面只读**：应用侧默认连接串固定 `file:<path>?mode=ro`（文件级），并叠加
   `PRAGMA query_only=ON`（会话级）；任何写入尝试由 SQLite 直接抛错，LLM 生成代码同样受此约束
   （语句层 SELECT-only 与表名白名单由 `app/core/sql_guard.py` 负责，交付物 4 接入）；
3. **数据面受控写**：`reports.db` 的写入**只有**一条通道——`report_write_conn()`（**原生
   sqlite3 独占写连接**），仅供「上传入库服务」使用（`POST /reports/upload` /
   `{code}/confirm` / `DELETE /reports/{code}`），以 `mode=rw` 打开（**不会凭空创建空库**）、
   `busy_timeout=10000` 兜底、`BEGIN IMMEDIATE` 取锁，并统一在
   `asyncio.to_thread(...)` 内执行、在 `app/core/locks.py` 的进程内写锁内串行化。

   > 为什么不用 `aiosqlite` + SQLAlchemy 写（本轮实测后的调整）：207 万行灌数基准
   > 200,000 行 × 16 列 —— SQLAlchemy `text()` + `to_dict(records)` 19.5µs/行（外推 41s）；
   > 原生 sqlite3 + `executemany` + `itertuples` 12.7µs/行（外推 26s）。差在参数绑定与
   > 逐行字典构造的 Python 开销上（PRD N4 有 60s 预算，故取后者）。两者连接参数完全一致，
   > 只读约束不受影响。
4. **外键必开**：SQLite 外键是**连接级**设置且默认关闭，控制面与数据面写连接建立后都强制
   `PRAGMA foreign_keys = ON`。
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from functools import lru_cache
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

__all__ = [
    "control_engine",
    "report_ro_engine",
    "control_conn",
    "report_ro_conn",
    "report_write_conn",
    "control_session",
    "dispose_engines",
]

#: 只读/写连接的忙等超时（毫秒）：写事务持锁时让只读查询短暂等待而非立即报错
BUSY_TIMEOUT_MS = 10_000


def _build_url(db_path: Path, *, read_only: bool = False) -> str:
    """构造 SQLAlchemy 连接串。

    - 只读：`sqlite+aiosqlite:///file:///<绝对路径>?mode=ro&uri=true`
      （`uri=true` 令 SQLite 以 URI 模式打开；`mode=ro` 是**文件级**只读保证）
    - 控制面：`sqlite+aiosqlite:///<绝对路径>`（读写）

    数据面的写连接不经过 SQLAlchemy（见 `report_write_conn()`）。
    """
    posix = db_path.as_posix()  # Windows 路径统一为 E:/a/b/c 形式
    if read_only:
        return f"sqlite+aiosqlite:///file:///{posix}?mode=ro&uri=true"
    return f"sqlite+aiosqlite:///{posix}"


@lru_cache
def control_engine() -> AsyncEngine:
    """控制面引擎：`power_agent.db`（用户 / 会话 / 分析记录 / 评测，读写）。"""
    # NullPool：SQLite 文件库无池化收益；避免连接跨事件循环/线程复用触发
    # "attached to a different loop"（测试与 uvicorn 场景统一）
    engine = create_async_engine(
        _build_url(get_settings().power_agent_db, read_only=False), poolclass=NullPool
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _setup_control_connection(dbapi_connection, _record) -> None:  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")  # 连接级，必须逐连接开启
        cursor.execute("PRAGMA journal_mode = WAL")  # 提升读写并发（数据库设计 8.3）
        cursor.close()

    return engine


@lru_cache
def report_ro_engine() -> AsyncEngine:
    """数据面引擎：`reports.db`（报表数据，**应用侧只读**）。"""
    engine = create_async_engine(
        _build_url(get_settings().reports_db, read_only=True), poolclass=NullPool
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _setup_report_connection(dbapi_connection, _record) -> None:  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA query_only = ON")  # 会话级兜底，叠加文件级 mode=ro
        # reports.db 是回滚日志模式：写事务期间会独占全库，只读查询需短暂等待
        cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        cursor.close()

    return engine


@contextmanager
def report_write_conn() -> Iterator[sqlite3.Connection]:
    """数据面**独占写连接**（原生 `sqlite3`，仅上传入库服务在 `asyncio.to_thread` 内使用）。

    使用铁律（PRD 7.4 / 数据库设计 4.6）：
    - 只允许 `app/services/report_write_service.py` 引用，**LLM 生成代码永不触碰**；
    - 必须在 `report_write_lock()` 内使用，且必须在 `asyncio.to_thread(...)` 里执行
      （同步阻塞调用，直接放事件循环会卡住所有请求）；
    - `mode=rw`：库不存在直接报错（不会创建一个空库来"假装成功"）；
    - 进出即 `BEGIN IMMEDIATE` / `COMMIT`，异常回滚；分批提交请用
      `connection.commit()` 后紧跟 `connection.execute("BEGIN IMMEDIATE")`。
    """
    db_path = get_settings().reports_db
    connection = sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=rw",
        uri=True,
        isolation_level=None,  # 关闭隐式事务，手动控制 BEGIN IMMEDIATE
        timeout=BUSY_TIMEOUT_MS / 1000,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        connection.close()


@asynccontextmanager
async def control_conn(*, write: bool = False) -> AsyncIterator[AsyncConnection]:
    """控制面连接。`write=True` 走事务（正常退出提交、异常回滚）。"""
    engine = control_engine()
    if write:
        async with engine.begin() as connection:
            yield connection
    else:
        async with engine.connect() as connection:
            yield connection


@asynccontextmanager
async def report_ro_conn() -> AsyncIterator[AsyncConnection]:
    """数据面只读连接（报表查询与会话工具统一走这里）。"""
    async with report_ro_engine().connect() as connection:
        yield connection


@lru_cache
def _control_session_factory() -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False：commit 后仍可读取对象属性（服务层返回 DTO 时需要）
    return async_sessionmaker(control_engine(), expire_on_commit=False)


@asynccontextmanager
async def control_session() -> AsyncIterator[AsyncSession]:
    """控制面 ORM 会话（读写）。

    注意：**ORM 实体装配只能由 Session 完成**——`Connection.execute(select(User))`
    返回的是列值而非 `User` 对象，因此业务查询统一走本会话。
    需要「先把写入落库、再抛业务异常」时，显式 `await session.commit()` 即可
    （异常若发生在 commit 之前会回滚，见 `auth_service` 登录失败路径的说明）。
    """
    factory = _control_session_factory()
    async with factory() as session:
        yield session


async def dispose_engines() -> None:
    """释放全部连接池（应用关闭时调用；写连接是即用即关的，无需释放）。"""
    if control_engine.cache_info().currsize:
        await control_engine().dispose()
    if report_ro_engine.cache_info().currsize:
        await report_ro_engine().dispose()
    _control_session_factory.cache_clear()
