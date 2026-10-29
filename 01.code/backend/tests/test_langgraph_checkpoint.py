"""阶段 3：LangGraph 检查点（会话级持久化 + 删除语义级联）。

覆盖四条：
1. **写入**：一次提问后，该会话线程在 `checkpoints` / `writes` 里有行；
2. **不串轮**（最容易被忽略的坑）：第二轮**不得继承**上一轮的过程态
   —— 上一轮是"守卫拦截失败"，第二轮正常问题必须干净成功、不发 `error` 事件；
3. **删除语义级联**：删会话（走真实接口）后，该线程的检查点行清零；
4. **幂等建表**：`ensure_schema()` 可重复调用（既有库不用重建就能用上检查点）。
"""

from __future__ import annotations

import pytest

from app.agent import checkpointer
from app.agent.graph import run_once_graph
from app.core.llm_client import ScriptedLLM, set_llm
from app.services import analysis_service
from tests.conftest import agent_connection

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _scripted_llm():
    set_llm(ScriptedLLM())
    yield
    set_llm(None)


@pytest.fixture(autouse=True)
async def _close_checkpointer():
    """用例结束后关掉**本循环**的检查点连接，避免 aiosqlite 线程在测试间累积。"""
    yield
    await checkpointer.close_checkpointer()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client, username: str) -> tuple[str, int]:
    response = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": "Abcd1234"}
    )
    assert response.status_code == 200, response.text
    token = response.json()["data"]["token"]
    me = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200, me.text
    return token, me.json()["data"]["id"]


async def _conversation(client, token: str) -> str:
    response = await client.post("/api/v1/conversations", headers=_auth(token), json={})
    assert response.status_code == 200, response.text
    return response.json()["data"]["conversation_code"]


def _count_rows(thread_id: str) -> dict[str, int]:
    """直连临时控制面库，统计该线程的检查点行数。"""
    conn = agent_connection()
    try:
        return {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?", (thread_id,)
            ).fetchone()[0]
            for table in (checkpointer.TABLE_CHECKPOINTS, checkpointer.TABLE_WRITES)
        }
    finally:
        conn.close()


async def test_checkpoint_rows_are_written_per_conversation(client) -> None:
    """一次提问 → 该会话线程写入检查点（thread_id = 会话编号）。"""
    token, user_id = await _register(client, "cp_write")
    conversation = await _conversation(client, token)
    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}}))

    result = await run_once_graph(user_id, conversation, "日报一共有多少行？")

    assert result.status == "success", result.error_message
    rows = _count_rows(conversation)
    assert rows[checkpointer.TABLE_CHECKPOINTS] > 0, rows
    # 另一个会话不应受影响（线程隔离）
    other = await _conversation(client, token)
    assert _count_rows(other)[checkpointer.TABLE_CHECKPOINTS] == 0


async def test_second_turn_does_not_inherit_previous_state(client, monkeypatch) -> None:
    """第二轮不得继承上一轮的过程态（检查点带来的最隐蔽的坑）。

    第 1 轮刻意做成"守卫拦截 → failed"（会写下 `last_error_*` / `status=failed`）；
    若状态跨轮泄漏，第 2 轮就会带着失败信息跑，甚至不发 `meta`。
    """
    monkeypatch.setattr(analysis_service.slot_service, "FASTPATH_ENABLED", False, raising=False)
    token, user_id = await _register(client, "cp_reset")
    conversation = await _conversation(client, token)

    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": "SELECT * FROM users"}}))
    first = await run_once_graph(user_id, conversation, "帮我删掉这张表")
    assert first.status == "failed" and first.error_code == "E_SQL_FORBIDDEN"

    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}}))
    second = await run_once_graph(user_id, conversation, "日报一共有多少行？")

    assert second.status == "success", second.error_message
    assert second.error_code is None and second.error_message is None
    assert second.retries == 0 and not second.path_switched
    assert [event["event"] for event in second.events][-2:] == ["meta", "done"]
    assert second.row_count == 1 and second.rows[0]["cnt"] == 0  # 干净的这一轮结果


async def test_delete_conversation_removes_checkpoints(client) -> None:
    """删会话 → 该线程检查点随之清理（走真实接口，与调用方同一入口）。"""
    token, user_id = await _register(client, "cp_delete")
    conversation = await _conversation(client, token)
    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}}))
    assert (await run_once_graph(user_id, conversation, "日报一共有多少行？")).status == "success"
    assert _count_rows(conversation)[checkpointer.TABLE_CHECKPOINTS] > 0

    response = await client.delete(f"/api/v1/conversations/{conversation}", headers=_auth(token))
    assert response.status_code == 200, response.text

    rows = _count_rows(conversation)
    assert rows[checkpointer.TABLE_CHECKPOINTS] == 0 and rows[checkpointer.TABLE_WRITES] == 0


async def test_ensure_schema_is_idempotent() -> None:
    """幂等建表：重复调用不报错（既有库无需重建即可用上检查点）。"""
    await checkpointer.ensure_schema()
    await checkpointer.ensure_schema()
    await checkpointer.ensure_schema()
