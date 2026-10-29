"""对话级检查点（LangGraph Checkpointer）—— SQLite 持久化。

它解决什么
----------
阶段 1–2 里，图的状态只活在**单次请求**内：请求结束，state 就没了。引入检查点之后：

1. **同一会话的多次提问共享一条线程**（`thread_id = conversation_code`）→ 状态可回放、可追溯；
2. 为阶段 4 的 **`interrupt()`（澄清卡）** 铺路：澄清发生在一次请求里、用户回填在**下一次请求**里，
   中间必须有个地方存住"跑到哪一步了" —— 那就是检查点；
3. 故障与中途失败后可续跑（本项目的澄清环正是"两跳请求"的形态，见 ADR-018）。

三条设计约束
------------
- **DDL 手工纳管**：表结构唯一事实源在 `01.code/scripts/sql/power_agent_langgraph.sql`
  （逐字抄自 `langgraph-checkpoint-sqlite` 3.1.2），由建库脚本与 `ensure_schema()` 执行；
  **不让框架自己 `setup()` 建表**（与 `db/models/base.py` 禁止 `create_all` 是同一条纪律）。
- **每轮显式重置过程态**：`per_turn_reset()` 给出所有"只属于本轮"的字段初值。
  带检查点后 state 会**跨轮累积**，若不显式重置，上一轮的表格/失败信息会漏到下一轮
  （与"评测跨题串条件"是同一类坑，见 `大修过程中的问题和难点` P-012）。
- **删除语义级联**：删会话 / 删账号要一并清掉线程行，避免孤儿数据。
  两个入口：`delete_threads_in_session()`（**在调用方事务内**，推荐）与
  `delete_threads()`（独立短连接，适合离线清理）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Iterable

import aiosqlite
from sqlalchemy import text

from app.agent.state import DEFAULT_STATE, AnalysisState
from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: 手工纳管的 DDL（唯一事实源）
DDL_PATH = Path(__file__).resolve().parents[3] / "scripts" / "sql" / "power_agent_langgraph.sql"

#: 检查点表名（删除语义与测试用）
TABLE_CHECKPOINTS = "checkpoints"
TABLE_WRITES = "writes"

#: saver 按**事件循环**缓存（`aiosqlite` 连接与 asyncio 原语都绑定创建它们的循环）。
#: 生产只有一个循环 → 等价于进程级单例；测试每个用例一个循环 → 各建各的、互不干扰。
_savers: dict[int, Any] = {}
_schema_ready = False


def _loop_key() -> int:
    """当前事件循环的标识（用于按循环缓存 saver）。"""
    return id(asyncio.get_running_loop())


async def ensure_schema(conn: aiosqlite.Connection | None = None) -> None:
    """幂等建表（既有库无需重建即可用上检查点）。

    为什么不是"只在建库脚本里建"：`03_init_db.py` 对**已灌数的库会提前返回**，
    因此既有安装永远走不到那段 DDL —— 这里补一次幂等执行（`CREATE TABLE IF NOT EXISTS`）。
    """
    global _schema_ready
    if _schema_ready and conn is None:
        return
    ddl = DDL_PATH.read_text(encoding="utf-8")
    if conn is not None:
        await conn.executescript(ddl)
        return
    path = Path(get_settings().power_agent_db)
    async with aiosqlite.connect(path) as own:
        await own.executescript(ddl)
        await own.commit()
    _schema_ready = True
    logger.info("检查点表已就绪（%s）", path.name)


async def get_checkpointer() -> Any:
    """取当前事件循环的 `AsyncSqliteSaver`（首次调用时建表并建连）。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    key = _loop_key()
    saver = _savers.get(key)
    if saver is not None:
        return saver
    path = Path(get_settings().power_agent_db)
    await ensure_schema()
    conn = await aiosqlite.connect(path)
    saver = AsyncSqliteSaver(conn)
    _savers[key] = saver
    logger.info("检查点已启用：%s（thread_id = 会话编号）", path.name)
    return saver


async def close_checkpointer() -> None:
    """关闭**当前循环**的检查点连接（应用退出/测试收尾用；下次调用会重建）。"""
    saver = _savers.pop(_loop_key(), None)
    if saver is not None:
        try:
            await saver.conn.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不影响业务
            logger.exception("关闭检查点连接失败")


def _ids(thread_ids: Iterable[str]) -> list[str]:
    return [str(item) for item in thread_ids if item]


async def delete_threads_in_session(session: Any, thread_ids: Iterable[str]) -> int:
    """**在调用方事务里**删除线程检查点（删会话 / 删账号时用）。

    为什么不开独立连接：控制面此时正持有写事务，另开连接 DELETE 会撞
    `database is locked`；而且"业务行与检查点同生共死"本就该在一个事务里。
    删除失败只记日志、不阻断主流程（检查点属过程数据，缺了不影响结果可取）。
    """
    ids = _ids(thread_ids)
    if not ids:
        return 0
    placeholders = ", ".join(f":t{index}" for index in range(len(ids)))
    params = {f"t{index}": value for index, value in enumerate(ids)}
    removed = 0
    try:
        for table in (TABLE_WRITES, TABLE_CHECKPOINTS):  # 先删缓冲表，再删主表
            result = await session.execute(
                text(f"DELETE FROM {table} WHERE thread_id IN ({placeholders})"), params
            )
            removed += result.rowcount or 0
    except Exception:  # noqa: BLE001 —— 表缺失/异常都不应阻断删除主流程
        logger.exception("清理检查点失败（线程：%s）", "、".join(ids[:3]))
        return 0
    if removed:
        logger.info("已随事务清理 %d 行检查点（线程：%s）", removed, "、".join(ids[:5]))
    return removed


async def delete_threads(thread_ids: Iterable[str]) -> int:
    """用**独立短连接**删除线程检查点（离线清理 / 脚本用），返回删除行数。"""
    ids = _ids(thread_ids)
    if not ids:
        return 0
    path = Path(get_settings().power_agent_db)
    if not path.exists():
        return 0
    await ensure_schema()
    placeholders = ", ".join("?" for _ in ids)
    removed = 0
    async with aiosqlite.connect(path) as conn:
        for table in (TABLE_WRITES, TABLE_CHECKPOINTS):
            cursor = await conn.execute(
                f"DELETE FROM {table} WHERE thread_id IN ({placeholders})", ids
            )
            removed += cursor.rowcount or 0
        await conn.commit()
    if removed:
        logger.info("已清理 %d 行检查点（线程：%s）", removed, "、".join(ids[:5]))
    return removed


def per_turn_reset() -> AnalysisState:
    """本轮必须**显式重置**的字段初值（带检查点后 state 会跨轮累积）。

    直接复用 `DEFAULT_STATE`：它已被整理成"覆盖全部单轮字段"的完整初始状态
    （入参由调用方每轮显式传入并覆盖）。
    """
    return dict(DEFAULT_STATE)  # type: ignore[return-value]
