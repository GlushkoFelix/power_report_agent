# -*- coding: utf-8 -*-
"""05_migrate_audit_events.py —— 扩展 `audit_logs` 的事件白名单（v1.19.12）

**为什么要这个脚本**：`audit_logs` 的 `event_type` 与 `action` 都是 **CHECK 白名单**，
而 SQLite **无法 `ALTER ... CHECK`** —— 新增事件类型只能**重建表**。本次新增 7 类：

- `narrative_number_warn`：解读"未授权数字"告警。v1.19.6 起代码就在写它，但**不在白名单**、
  且 `action` 写的是白名单外的 `flagged` → 插入被 CHECK 拒绝，**告警静默丢失**
  （调用处有 `try/except` 兜底，所以只在日志里留了 exception）。`action` 已修为 `warned`。
- 个人中心四件事：`auth_profile_updated` / `auth_password_changed` / `auth_password_change_failed`
  / `profile_records_cleared` / `auth_account_deleted` / `auth_delete_account_failed`

**做法（幂等）**：从 `sql/power_agent.sql` 读取 `audit_logs` 的**最新 DDL**（口径单一来源，
不在这里复制一份），若现有库的建表语句已含 `auth_account_deleted` 就跳过；否则
`重命名旧表 → 建新表 → 拷数据 → 删旧表 → 重建索引`。

**安全性**：没有任何表的外键指向 `audit_logs`，重建期间关闭外键检查；全过程在**单个事务**内，
异常回滚；数据一行不丢（列顺序一致，`INSERT ... SELECT *`）。

用法：
    .venv\\Scripts\\python.exe 01.code\\scripts\\05_migrate_audit_events.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import _common as C

#: 新白名单里的标志性事件类型：出现它即说明库已是新结构（幂等判据）
MARKER_EVENT = "auth_account_deleted"
_TABLE = "audit_logs"


def extract_audit_ddl(sql_path: Path) -> tuple[str, list[str]]:
    """从 DDL 事实源里抽出 `audit_logs` 的建表语句与索引语句（**不复制粘贴**，避免两处漂移）。"""
    text = sql_path.read_text(encoding="utf-8")
    start = text.index(f"CREATE TABLE IF NOT EXISTS {_TABLE}")
    end = text.index("\n);", start) + len("\n);")
    table_sql = text[start:end].rstrip()
    if not table_sql.endswith(";"):
        table_sql += ";"
    indexes = [
        line.strip()
        for line in text.splitlines()
        if line.startswith("CREATE INDEX") and f"ix_audit_" in line
    ]
    if MARKER_EVENT not in table_sql:
        raise RuntimeError(f"DDL 里的 {_TABLE} 仍未包含 {MARKER_EVENT}，请先更新 sql/power_agent.sql")
    return table_sql, indexes


def already_migrated(db_path: Path) -> bool:
    """看现有表的建表 SQL 里有没有新事件类型（幂等判据）。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (_TABLE,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError(f"{db_path} 里没有 {_TABLE} 表")
    return MARKER_EVENT in (row[0] or "")


def migrate(db_path: Path, table_sql: str, index_sql: list[str]) -> int:
    """重建 `audit_logs`：返回迁移后的行数。"""
    conn = sqlite3.connect(db_path)
    try:
        before = conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0]
        # 外键开关必须在事务之外执行（事务内设置会被忽略）
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            conn.execute(f"ALTER TABLE {_TABLE} RENAME TO {_TABLE}_old")
            conn.execute(table_sql)
            conn.execute(f"INSERT INTO {_TABLE} SELECT * FROM {_TABLE}_old")
            conn.execute(f"DROP TABLE {_TABLE}_old")
            for sql in index_sql:
                conn.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        after = conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0]
        if after != before:
            raise RuntimeError(f"重建后行数不一致：{before} → {after}")
        return after
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="扩展 audit_logs 事件白名单（重建表，幂等）")
    parser.add_argument("--db", default=str(C.AGENT_DB), help="控制面库路径")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[SKIP] 库不存在：{db_path}")
        sys.exit(0)

    table_sql, index_sql = extract_audit_ddl(C.SQL_DIR / "power_agent.sql")
    if already_migrated(db_path):
        print(f"[OK] {db_path.name} 已是新结构（含 {MARKER_EVENT}），无需迁移")
        sys.exit(0)

    rows = migrate(db_path, table_sql, index_sql)
    print(f"[DONE] 已重建 {_TABLE}：{db_path.name}，保留 {rows} 行历史审计")
    print("       新白名单额外支持："
          "narrative_number_warn / auth_profile_updated / auth_password_changed / "
          "auth_password_change_failed / profile_records_cleared / auth_account_deleted / "
          "auth_delete_account_failed")


if __name__ == "__main__":
    main()
