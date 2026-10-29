"""智电析报 · 进程内写锁（报表库写路径串行化）。

决策（本轮 grilling 定案，见 PRD 7.4 / 数据库设计 4.6）：
`reports.db` 是 `journal_mode=delete`（回滚日志），**一旦开始写事务即独占全库**；
应用侧又用 `mode=ro` 只读连接随时可能查询预览。若允许并发写或长事务，
只读查询会拿到 `database is locked`。因此：

1. **一把进程内 `asyncio.Lock`** 串行化三个写入阶段 —— ① `upload` 解析、
   ② `confirm` 灌数、③ `delete` 删表；同一时刻只有一个写者；
2. **`confirm` 灌数分批提交**（见 `report_write_service.INGEST_BATCH`），把写锁
   持有时间切碎，让只读查询有窗口插入；
3. 只读连接另有 `busy_timeout=10000` 兜底（见 `app/db/session.py`）。

范围说明：本锁只保护**单进程**（uvicorn 单 worker 是本项目部署形态，见架构文档 7）。
多 worker 部署需换成文件锁或独立写服务，属后续扩展，当前不实现。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

__all__ = ["report_write_lock", "is_write_locked"]

#: 报表库写锁（解析 / 灌数 / DROP TABLE 三者互斥）
_report_write_lock = asyncio.Lock()


@asynccontextmanager
async def report_write_lock() -> AsyncIterator[None]:
    """获取报表库写权；等待期间不阻塞事件循环（长任务本身已在线程池执行）。"""
    async with _report_write_lock:
        yield


def is_write_locked() -> bool:
    """是否有写任务在跑（供健康检查/诊断观察，不参与业务判断）。"""
    return _report_write_lock.locked()
