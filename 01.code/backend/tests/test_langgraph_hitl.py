"""阶段 4：澄清的 Human-in-the-loop（`interrupt()` + `resume`）。

三条：
1. **真的挂起**：澄清轮结束后，线程里有**未完成的挂起任务**（`aget_state().next` 非空）→
   说明是"断点等待"，而不是"跑完再重跑整轮"；
2. **恢复即续跑**：`/analysis/clarify` 回填后从断点继续 —— 证据是 `llm_calls` 里**没有 slot 阶段**
   （等价既有的 `preset_slot` 语义：不重复做增量抽取、不重复调模型）；
3. **无检查点也能活**：把线程的检查点删掉再回填 → 自动回退为"整轮重跑"，功能不降级。
"""

from __future__ import annotations

import json

import pytest

from app.agent import checkpointer
from app.agent.graph import build_graph_with_checkpointer, run_once_graph
from app.core.llm_client import ScriptedLLM, set_llm
from tests.conftest import agent_connection

pytestmark = pytest.mark.anyio

_SLOT_SIGNAL = {
    "time_range": "全期", "granularity": None, "metric": "总用电量",
    "dimension": None, "filters": None, "compare": None,
    "intent": "澄清", "chart_hint": None, "missing_slot": "granularity",
}
_GEN_SQL = {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}


@pytest.fixture(autouse=True)
def _scripted_llm():
    set_llm(ScriptedLLM())
    yield
    set_llm(None)


@pytest.fixture(autouse=True)
async def _close_checkpointer():
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
    return token, me.json()["data"]["id"]


async def _conversation(client, token: str) -> str:
    response = await client.post("/api/v1/conversations", headers=_auth(token), json={})
    assert response.status_code == 200, response.text
    return response.json()["data"]["conversation_code"]


async def _sse_frames(client, token: str, path: str, payload: dict) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    event: str | None = None
    async with client.stream("POST", path, headers=_auth(token), json=payload) as response:
        assert response.status_code == 200, await response.aread()
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                frames.append((event, json.loads(line.split(":", 1)[1].strip() or "{}")))
                event = None
    return frames


async def test_clarify_stops_with_pending_interrupt(client) -> None:
    """澄清轮结束时线程里应存在**未完成的挂起任务**（HITL 的本质）。"""
    token, user_id = await _register(client, "hitl_pending")
    conversation = await _conversation(client, token)
    set_llm(ScriptedLLM(rules={"slot": _SLOT_SIGNAL}))
    result = await run_once_graph(user_id, conversation, "看看趋势")

    assert result.clarify is not None, result.error_message
    assert result.clarify["missing_slot"] == "granularity"
    assert result.status == "success" and not result.record_code  # 澄清轮不落 analyses
    assert [event["event"] for event in result.events][-2:] == ["meta", "done"]

    # 直接问图：这条线程是不是"停住了、等着回填"？
    graph = build_graph_with_checkpointer(await checkpointer.get_checkpointer())
    snapshot = await graph.aget_state({"configurable": {"thread_id": conversation}})
    assert snapshot.next, "澄清应当留下未完成的挂起任务（interrupt）"
    # 检查点里确实写了行
    conn = agent_connection()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (conversation,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert n > 0


async def test_clarify_endpoint_resumes_and_skips_slot_llm(client) -> None:
    """`/analysis/clarify` 回填 → 从断点续跑：**不重复调 slot 模型**（等价既有 preset 语义）。"""
    token, user_id = await _register(client, "hitl_resume")
    conversation = await _conversation(client, token)

    set_llm(ScriptedLLM(rules={"slot": _SLOT_SIGNAL}))
    first = await _sse_frames(
        client, token, "/api/v1/analysis/chat",
        {"conversation_code": conversation, "message": "看看趋势"},
    )
    assert first[-1][0] == "done" and first[-1][1]["record_id"] is None
    assert [name for name, _ in first][-3:] == ["clarify", "meta", "done"]

    set_llm(ScriptedLLM(rules={"gen_sql": _GEN_SQL}))
    second = await _sse_frames(
        client, token, "/api/v1/analysis/clarify",
        {
            "conversation_code": conversation,
            "question": "看看趋势",
            "slot_patch": {"metric": "总用电量", "granularity": "月"},
        },
    )
    names = [name for name, _ in second]
    assert names[-1] == "done" and second[-1][1]["record_id"].startswith("a_")
    # 与 legacy 澄清延续的事件序列对齐（都从 status(parsing) 开始）
    assert names[0] == "status" and names[1] == "slot"
    assert {"code", "table", "chart", "narrative", "meta"} <= set(names)

    record_code = second[-1][1]["record_id"]
    detail = (
        await client.get(f"/api/v1/analysis/records/{record_code}", headers=_auth(token))
    ).json()["data"]
    stages = [call["stage"] for call in detail["llm_calls"]]
    assert "slot" not in stages, stages        # 断点续跑：不重复做增量抽取
    assert "gen_sql" in stages and detail["status"] == "success"


async def test_clarify_endpoint_falls_back_without_checkpoint(client) -> None:
    """检查点被清理（换库/清理过）时，回填仍应成功——自动回退为"整轮重跑"。"""
    token, user_id = await _register(client, "hitl_fallback")
    conversation = await _conversation(client, token)

    set_llm(ScriptedLLM(rules={"slot": _SLOT_SIGNAL}))
    first = await _sse_frames(
        client, token, "/api/v1/analysis/chat",
        {"conversation_code": conversation, "message": "看看趋势"},
    )
    assert first[-1][1]["record_id"] is None
    await checkpointer.delete_threads([conversation])   # 模拟"检查点没了"

    set_llm(ScriptedLLM(rules={"gen_sql": _GEN_SQL}))
    second = await _sse_frames(
        client, token, "/api/v1/analysis/clarify",
        {
            "conversation_code": conversation,
            "question": "看看趋势",
            "slot_patch": {"metric": "总用电量", "granularity": "月"},
        },
    )
    assert [name for name, _ in second][-1] == "done"
    assert second[-1][1]["record_id"].startswith("a_")   # 回退路径照样出结果
