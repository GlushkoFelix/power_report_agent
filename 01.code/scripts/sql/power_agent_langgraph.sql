-- ============================================================================
-- power_agent_langgraph.sql —— LangGraph 检查点表（**手工纳管**）
-- ============================================================================
-- 为什么把框架建的表抄进来（而不是让 `SqliteSaver.setup()` 自己建）：
--   本项目的纪律是「**表结构唯一事实源 = scripts/sql/**」，`app/db/models/base.py`
--   明确禁止 `Base.metadata.create_all()`。检查点表虽然由 LangGraph 读写，但它和
--   会话/分析记录同在**控制面库**（`02.data/power_agent.db`），同属"这个系统的表"，
--   因此必须与其它表一样走 DDL 事实源，而不是让框架在运行时偷偷建。
--
-- 来源与版本（**升级该依赖时必须回来核对**）：
--   `langgraph-checkpoint-sqlite` 3.1.2（2026-10-13 安装）
--   表结构取自包内 `langgraph/checkpoint/sqlite/__init__.py` 的建表语句（逐字抄录，
--   仅加上 `IF NOT EXISTS` 以便幂等执行）。
--
-- 执行方式：
--   ① 建库时：`01.code/scripts/03_init_db.py` 会执行本文件；
--   ② 既有库：应用侧启动/首次使用时由 `app/agent/checkpointer.py::ensure_schema()`
--      幂等执行一次（`CREATE TABLE IF NOT EXISTS`），**无需重建库**。
--
-- 删除语义：`thread_id = 会话编号`，因此
--   · 删会话（`DELETE /conversations/{code}`）→ 必须一并删除该线程的检查点行；
--   · 删账号 / 清空记录 → 必须删除该用户**所有会话**的检查点行。
--   实现见 `app/agent/checkpointer.py::delete_threads()`，调用点在
--   `services/conversation_service.py` 与 `services/auth_service.py`。
-- ============================================================================

-- ① 检查点主表：每条线程的每一步（每个 super-step）一行
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint BLOB,
    metadata BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

-- ② 待写入缓冲：节点产出但尚未合并进检查点的通道值（中断/恢复场景会用到）
CREATE TABLE IF NOT EXISTS writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    task_path TEXT NOT NULL DEFAULT '',
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    value BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
