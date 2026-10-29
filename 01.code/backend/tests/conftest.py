"""pytest 全局夹具。

要点：
1. **临时库**：在导入 `app` 之前把 `POWER_AGENT_DB_PATH` / `REPORTS_DB_PATH` 指向临时文件，
   用真实 DDL（`01.code/scripts/sql/*.sql`）建表，**不触碰** `02.data` 下的正式库；
2. **同事件循环**：用 `httpx.AsyncClient` + `ASGITransport` + anyio，避免 `TestClient`
   为每个请求开新事件循环导致 async engine 报 “attached to a different loop”。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# ── 1. 先改环境变量，再导入 app（config 首次实例化时读 env）──
_TMP_DIR = Path(tempfile.mkdtemp(prefix="zdxb_test_"))
_AGENT_DB = _TMP_DIR / "power_agent.db"
_REPORTS_DB = _TMP_DIR / "reports.db"
_UPLOAD_DIR = _TMP_DIR / "uploads"

os.environ["POWER_AGENT_DB_PATH"] = str(_AGENT_DB)
os.environ["REPORTS_DB_PATH"] = str(_REPORTS_DB)
# 上传落盘也必须隔离到临时目录，否则测试会往 02.data/uploads 写真实文件
os.environ["UPLOAD_DIR_PATH"] = str(_UPLOAD_DIR)
os.environ["JWT_SECRET_KEY"] = "pytest-only-secret-key-not-for-any-other-use"
# 快通道口径：测试**固定走 v1**（`SLOT_FASTPATH_V2=false`），与交付默认（默认 `true`）**解耦** ——
# 否则本地 `.env` 一改，既有断言的 `llm_calls` stage 序列（如 `("slot", 0)`）会被整体带偏，
# 而"某题是否被规则接管"并非这些用例的断言对象。v2 路径由 `tests/test_slot_fastpath_v2.py`
# 用自己的用例显式开关覆盖（含两态分流、消融矩阵、边界）。
os.environ["SLOT_FASTPATH_V2"] = "false"

_SQL_DIR = Path(__file__).resolve().parents[2] / "scripts" / "sql"

#: 供详情/Schema/质量接口断言的样例载荷（真实路径由 03_init_db.py 回填，此处手工等价构造）
_SCHEMA_JSON = json.dumps(
    {
        "table": "report_daily",
        "title": "日报",
        "caliber_version": "caliber-v1.0",
        "row_count": 1442,
        "time_range": ["2006-12-17", "2010-11-26"],
        "columns": [
            {"name": "date", "type": "TEXT", "unit": "—", "agg": "—", "description": "'YYYY-MM-DD'"},
            {"name": "consumption_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
        ],
    },
    ensure_ascii=False,
)
_QUALITY_JSON = json.dumps(
    {
        "level": "dataset",
        "caliber_version": "caliber-v1.0",
        "dataset": {
            "missing_rows": 25979,
            "missing_ratio": 0.012518,
            "time_axis_gap": 0,
            "duplicate_timestamps": 0,
            "voltage_out_of_range_minutes": 4010,
        },
        "checks": {"A1_levels_equal": {"passed": True}, "A5_row_counts": {"passed": True}},
    },
    ensure_ascii=False,
)


def _init_databases() -> None:
    """用三份 DDL 事实源建临时库，并造 2 条预置报表元数据。

    第三份是 **LangGraph 检查点表**（`power_agent_langgraph.sql`，手工纳管）——
    默认编排已是 `langgraph`，图状态会写进这两张表，故测试库必须与生产库同构。
    """
    for db_path, ddl_name in ((_AGENT_DB, "power_agent.sql"), (_REPORTS_DB, "reports.sql")):
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript((_SQL_DIR / ddl_name).read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()

    conn = sqlite3.connect(_AGENT_DB)
    try:
        conn.executescript(
            (_SQL_DIR / "power_agent_langgraph.sql").read_text(encoding="utf-8")
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(_AGENT_DB)
    try:
        # ① 正常预置：物理表存在 → table_exists 应为 True（is_active=1 表示系统默认活跃）
        conn.execute(
            """
            INSERT INTO uploaded_reports
                (report_code, user_id, filename, table_name, report_type, row_count,
                 column_count, time_start, time_end, caliber_version, is_preset, is_active, status,
                 schema_json, quality_json)
            VALUES ('r_daily', NULL, 'report_daily.csv', 'report_daily', 'daily', 1442,
                    20, '2006-12-17', '2010-11-26', 'caliber-v1.0', 1, 1, 'ready', ?, ?)
            """,
            (_SCHEMA_JSON, _QUALITY_JSON),
        )
        # ② 异常预置：物理表不存在 → table_exists 应为 False（验证自检字段有效）
        conn.execute(
            """
            INSERT INTO uploaded_reports
                (report_code, user_id, filename, table_name, report_type, is_preset, is_active, status)
            VALUES ('r_ghost', NULL, 'ghost.csv', 'report_ghost_missing', 'uploaded', 1, 0, 'ready')
            """
        )
        # ③ 他人上传的报表（user_id = 999）→ 越权访问应返回 403
        conn.execute(
            """
            INSERT INTO uploaded_reports
                (report_code, user_id, filename, table_name, report_type, is_preset, is_active, status)
            VALUES ('r_up_9999', 999, 'others.csv', 'report_daily', 'uploaded', 0, 0, 'ready')
            """
        )
        conn.commit()
    finally:
        conn.close()


_init_databases()


def agent_connection() -> sqlite3.Connection:
    """测试内直连临时系统库（用于断言 login_fail_count / audit_logs 等落库结果）。"""
    return sqlite3.connect(_AGENT_DB)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client():
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac
