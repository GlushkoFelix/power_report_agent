"""`ORCHESTRATOR` 灰度开关的分发验证（走**真实端点**，零额度）。

覆盖：
- 开关读取语义：默认 legacy / env 指定 langgraph / 未知值退回 legacy；
- **同一问题、两种编排，SSE 事件帧序列一致**（`POST /analysis/chat`）；
- 澄清回填后在 langgraph 模式下能继续执行（`POST /analysis/clarify`）。
"""

from __future__ import annotations

import json

import pytest

from app.agent.dispatch import MODE_LANGGRAPH, MODE_LEGACY, orchestration_mode
from app.core.config import get_settings
from app.core.llm_client import ScriptedLLM, set_llm

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _scripted_llm():
    set_llm(ScriptedLLM())
    yield
    set_llm(None)


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


async def _sse_frames(client, token: str, path: str, payload: dict) -> list[tuple[str, dict]]:
    """收集 SSE 帧：`[(event, data), ...]`。"""
    frames: list[tuple[str, dict]] = []
    async with client.stream("POST", path, headers=_auth(token), json=payload) as response:
        assert response.status_code == 200, await response.aread()
        event: str | None = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event:
                raw = line.split(":", 1)[1].strip() or "{}"
                frames.append((event, json.loads(raw)))
                event = None
    return frames


# ── 开关语义 ─────────────────────────────────────────────────────────────


async def test_mode_follows_config_when_env_absent(monkeypatch) -> None:
    """未设环境变量时跟随 `.env`（`Settings.orchestrator`）——2026-10-13 起默认值为 `langgraph`。"""
    monkeypatch.delenv("ORCHESTRATOR", raising=False)
    configured = (getattr(get_settings(), "orchestrator", "") or MODE_LEGACY).strip().lower()
    assert orchestration_mode() == configured


async def test_mode_env_overrides_config_and_unknown_falls_back(monkeypatch) -> None:
    """进程环境变量**优先于** `.env`（便于临时切换 / A-B 对比）；未知值退回 legacy 并告警。"""
    monkeypatch.setenv("ORCHESTRATOR", "legacy")
    assert orchestration_mode() == MODE_LEGACY
    monkeypatch.setenv("ORCHESTRATOR", "LANGGRAPH")  # 大小写不敏感
    assert orchestration_mode() == MODE_LANGGRAPH
    monkeypatch.setenv("ORCHESTRATOR", "dummy-value")  # 未知值 → 退回 legacy
    assert orchestration_mode() == MODE_LEGACY


# ── 同一问题、两种编排：SSE 帧序列一致 ────────────────────────────────────


async def test_chat_sse_frames_match_across_orchestrators(client, monkeypatch) -> None:
    token, _ = await _register(client, "lg_dispatch_sse")
    question = "日报一共有多少行？"
    rules = {"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}}

    # 显式指定 legacy，不受 `.env` 默认值影响（这样本用例始终在比"两种编排"）
    monkeypatch.setenv("ORCHESTRATOR", MODE_LEGACY)
    conv_legacy = await _conversation(client, token)
    set_llm(ScriptedLLM(rules=rules))
    legacy_frames = await _sse_frames(
        client, token, "/api/v1/analysis/chat",
        {"conversation_code": conv_legacy, "message": question},
    )

    monkeypatch.setenv("ORCHESTRATOR", MODE_LANGGRAPH)
    conv_graph = await _conversation(client, token)
    set_llm(ScriptedLLM(rules=rules))
    graph_frames = await _sse_frames(
        client, token, "/api/v1/analysis/chat",
        {"conversation_code": conv_graph, "message": question},
    )

    assert [event for event, _ in legacy_frames] == [event for event, _ in graph_frames]
    assert legacy_frames[-1][0] == graph_frames[-1][0] == "done"
    assert legacy_frames[-1][1]["record_id"].startswith("a_")
    assert graph_frames[-1][1]["record_id"].startswith("a_")


# ── 澄清 → 回填：langgraph 模式下能继续执行 ────────────────────────────────


async def test_clarify_continuation_in_langgraph_mode(client, monkeypatch) -> None:
    token, _ = await _register(client, "lg_dispatch_clarify")
    conversation = await _conversation(client, token)
    monkeypatch.setenv("ORCHESTRATOR", MODE_LANGGRAPH)

    slot_signal = {
        "time_range": "全期", "granularity": None, "metric": "总用电量",
        "dimension": None, "filters": None, "compare": None,
        "intent": "澄清", "chart_hint": None, "missing_slot": "granularity",
    }
    set_llm(ScriptedLLM(rules={"slot": slot_signal}))
    first = await _sse_frames(
        client, token, "/api/v1/analysis/chat",
        {"conversation_code": conversation, "message": "看看趋势"},
    )
    assert first[-1][0] == "done"
    assert first[-1][1]["record_id"] is None  # 澄清轮不落 analyses
    clarify = next(data for event, data in first if event == "clarify")
    assert clarify["missing_slot"] == "granularity"

    # 回填 → 继续执行（preset_slot 路径，跳过二次增量抽取）。
    # 注意：澄清轮**不回写 slot_json**（既有设计），因此回填要把"卡片上已有的条件"一并带上
    # —— 这正是前端"条件芯片"的语义（芯片内容 = 澄清卡展示的 partial slot + 本次点选）。
    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}}))
    second = await _sse_frames(
        client, token, "/api/v1/analysis/clarify",
        {
            "conversation_code": conversation,
            "question": "看看趋势",
            "slot_patch": {"metric": "总用电量", "granularity": "月"},
        },
    )
    assert second[-1][0] == "done"
    assert second[-1][1]["record_id"].startswith("a_")
    assert "table" in [event for event, _ in second]
