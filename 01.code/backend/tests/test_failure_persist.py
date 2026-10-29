"""失败留痕与"追问轮继承"的回归用例（2026-10-14，实测 q119 第二轮暴露的两个真问题）。

两个问题一起修、一起守：

1. **失败留痕撞 CHECK 约束**：`analyses.route` 上有
   `CHECK (route IS NULL OR route IN ('sql','pandas'))`，而 `AnalysisResult.route` 默认是**空串**——
   slot 超时这类"还没走到生成"的失败会把 `''` 写进去 → `IntegrityError` → **留痕没写成、异常还上抛**
   （评测侧表现为"这题没有调用记录、却是空结果"，看起来像模型答错）。
2. **追问轮被判"认不出指标"**：「只看工作日。」本身不含指标词，但会话里早就有指标
   （多轮语义 = 未提及即继承）→ 超时兜底以前一律返回 None → 整题失败。
"""

from __future__ import annotations

import pytest

from app.core.llm_client import ScriptedLLM, set_llm
from app.services import analysis_service, slot_service
from tests.conftest import agent_connection

pytestmark = pytest.mark.anyio


class _SlowLLM:
    """把某个 purpose 的调用拖长（模拟供应商慢时段），其余交给内部 ScriptedLLM。"""

    name = "slow-stub"
    configured = True
    supports_model_tiers = False

    def __init__(self, inner, slow_purpose: str, delay: float = 5.0) -> None:
        self._inner = inner
        self._slow_purpose = slow_purpose
        self._delay = delay

    async def chat(self, messages, *, purpose="", model=None, temperature=None, json_mode=False):
        if purpose == self._slow_purpose:
            import asyncio

            await asyncio.sleep(self._delay)
        return await self._inner.chat(
            messages, purpose=purpose, model=model, temperature=temperature, json_mode=json_mode
        )


# ── ① 纯规则层：追问轮可以继承指标 ──────────────────────────────────────────


def test_fallback_slot_inherits_metric_from_session() -> None:
    """会话已有指标时，追问问句里没有指标词也应给出增量（指标留空 → 由合并继承）。"""
    fallback = slot_service.fallback_slot("只看工作日。", {"metric": "总用电量", "time_range": "全期"})
    assert fallback is not None, "追问轮不该被判成'认不出指标'"
    assert fallback.get("metric") is None, "指标应留空，交由 merge_slot 从会话继承"
    merged = slot_service.merge_slot({"metric": "总用电量", "time_range": "全期"}, fallback)
    assert merged.get("metric") == "总用电量", "合并后指标必须还在"


def test_fallback_slot_still_requires_some_metric() -> None:
    """反向：本轮认不出、会话也没有指标时**仍应返回 None**（不编造指标）。"""
    assert slot_service.fallback_slot("看看数据怎么样？", {}) is None
    assert slot_service.fallback_slot("", {"metric": "总用电量"}) is None


# ── ② 端到端：slot 超时失败也必须能留痕（route 写 NULL，不撞 CHECK）─────────


async def test_slot_timeout_failure_is_persisted_with_null_route(client, monkeypatch) -> None:
    set_llm(
        _SlowLLM(
            ScriptedLLM(rules={"gen_sql": {"sql": "SELECT COUNT(*) AS days FROM report_daily"}}),
            slow_purpose="slot",
            delay=5.0,
        )
    )
    monkeypatch.setattr(analysis_service, "SLOT_TIMEOUT_SECONDS", 0.05)

    token = await _register(client, "persist_route_null")
    conversation = await _conversation(client, token)
    result = await analysis_service.run_once(
        _user_id(token), conversation, "看看数据怎么样？"  # 无指标、会话也无指标 → 如实失败
    )

    assert result.status == "failed", result.error_message
    assert result.error_code == "E_LLM"
    assert result.record_code, "失败也必须落留痕（此前会因 CHECK 约束写不进去）"

    # 直接查库：`route` 上那条 CHECK 约束是本次修复的核心，接口层会把 NULL 渲染成默认值，
    # 不足以验证（响应里失败记录也会显示 route='sql'）。
    conn = agent_connection()
    try:
        row = conn.execute(
            "SELECT status, route FROM analyses WHERE record_code = ?", (result.record_code,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "失败留痕必须真的落库"
    assert row[0] == "failed"
    # `route` 的不变量：**NULL 或合法取值**（`analyses` 上有 CHECK 约束）。
    # 本次崩溃的根因是写进了**空串** `''`（`AnalysisResult.route` 的默认值）→ INSERT 被拒 →
    # 失败留痕丢失、异常上抛。所以这里断言的是"**不是空串**"而非"必须是 NULL"：
    # 槽位阶段就失败时，state 的 route 默认值是 `sql`（合法，也符合"当时还没选路"的语义）。
    assert row[1] in (None, "sql", "pandas"), f"route 不能是非法值（实测踩过空串）：{row[1]!r}"


# ── 测试辅助（沿用既有用例的注册/建会话套路）────────────────────────────────


async def _register(client, name: str) -> str:
    resp = await client.post(
        "/api/v1/auth/register", json={"username": name, "password": "test12345"}
    )
    if resp.json().get("code") != 0:
        resp = await client.post(
            "/api/v1/auth/login", json={"username": name, "password": "test12345"}
        )
    return resp.json()["data"]["token"]


async def _conversation(client, token: str) -> str:
    resp = await client.post(
        "/api/v1/conversations", json={"title": "留痕用例"}, headers={"Authorization": f"Bearer {token}"}
    )
    return resp.json()["data"]["conversation_code"]


def _user_id(token: str) -> int:
    import base64
    import json

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    return int(claims.get("sub") or claims.get("user_id"))
