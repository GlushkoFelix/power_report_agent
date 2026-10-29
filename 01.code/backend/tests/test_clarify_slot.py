"""多轮 slot 继承 + 澄清回路测试（交付物 4 第四批，决策 18/19/20 轮）。

覆盖：
- `_merge_slot` / `normalize_slot` / `validate_slot_patch` / `resolve_clarify` 纯函数规则；
- chat 的增量继承（覆盖 / 继承 / filters 替换）与澄清轮（事件、落库形态、slot_json 不回写）；
- `POST /analysis/clarify`（回填后就地继续、SSE）；
- replay 的 slot 基线 = 原记录（决策 19-4）；
- 切换活跃报表清空 slot（决策 18-6）。

全部离线（`ScriptedLLM` + 临时库）。
"""

from __future__ import annotations

import json

import pytest

from app.core.errors import BizError
from app.core.llm_client import ScriptedLLM, set_llm
from app.services import analysis_service, slot_service
from tests.conftest import agent_connection

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _scripted_llm():
    set_llm(ScriptedLLM())
    yield
    set_llm(None)


async def _register(client, username: str) -> tuple[str, int]:
    response = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": "Abcd1234"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    return payload["token"], payload["user"]["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _conversation(client, token: str) -> str:
    response = await client.post("/api/v1/conversations", headers=_auth(token), json={})
    assert response.status_code == 200, response.text
    return response.json()["data"]["conversation_code"]


def _slot_event(result) -> dict:
    return next(event["data"] for event in result.events if event["event"] == "slot")


def _clarify_event(result) -> dict:
    return next(event["data"] for event in result.events if event["event"] == "clarify")


# ── 纯函数规则 ──────────────────────────────────────────────────────────────


def test_merge_slot_overrides_inherits_and_merges_filters() -> None:
    base = {
        "time_range": "2020-08", "granularity": "月", "metric": "总用电量",
        "dimension": None, "filters": [{"field": "is_weekend", "op": "=", "value": 1}],
        "compare": None, "intent": "趋势", "chart_hint": "折线",
    }
    # 「只看工作日的」：同 field 替换，其余继承
    merged = slot_service.merge_slot(
        base, {"filters": [{"field": "is_weekend", "op": "=", "value": 0}]}
    )
    assert merged["time_range"] == "2020-08"  # 继承
    assert merged["metric"] == "总用电量"  # 继承
    assert merged["filters"] == [{"field": "is_weekend", "op": "=", "value": 0}]  # 替换

    # 「再按月汇总一次」类：标量覆盖 + 异 field 追加
    merged2 = slot_service.merge_slot(
        base,
        {
            "compare": "同比",
            "filters": [{"field": "is_partial_period", "op": "=", "value": 0}],
        },
    )
    assert merged2["compare"] == "同比"  # 覆盖
    assert [item["field"] for item in merged2["filters"]] == [
        "is_weekend", "is_partial_period",
    ]  # 异 field 追加

    # 增量为空 → 完全继承
    assert slot_service.merge_slot(base, None)["metric"] == "总用电量"


def test_normalize_slot_fills_default_and_resets_clarify_intent() -> None:
    normalized = slot_service.normalize_slot({"metric": "总用电量", "intent": "澄清"})
    assert normalized["time_range"] == "全期"  # 决策 18-1②：默认「全期」
    assert normalized["intent"] is None  # 决策 19-4：澄清意图不带进执行
    assert normalized["metric"] == "总用电量"
    assert set(normalized) == set(slot_service.SLOT_KEYS)


def test_validate_slot_patch_rules() -> None:
    assert slot_service.validate_slot_patch(None) == {}

    # 未知键 → 400
    with pytest.raises(BizError) as unknown:
        slot_service.validate_slot_patch({"hacker": "x"})
    assert unknown.value.error_code == "E_VALIDATION"

    # bool 拒收；数字转字符串；限长
    cleaned = slot_service.validate_slot_patch({"metric": 3.5, "granularity": True is False or "日"})
    assert cleaned == {"metric": "3.5", "granularity": "日"}
    with pytest.raises(BizError):
        slot_service.validate_slot_patch({"metric": True})
    with pytest.raises(BizError):
        slot_service.validate_slot_patch({"metric": "x" * 201})

    # filters 结构校验 + 三键规整
    cleaned = slot_service.validate_slot_patch(
        {"filters": [{"field": "is_weekend", "op": "=", "value": 1, "extra": "drop"}]}
    )
    assert cleaned["filters"] == [{"field": "is_weekend", "op": "=", "value": 1}]
    with pytest.raises(BizError):
        slot_service.validate_slot_patch({"filters": [{"field": "x", "op": "≈", "value": 1}]})

    # null / 空 filters = 未提及，忽略
    assert slot_service.validate_slot_patch({"metric": None, "filters": []}) == {}


def test_resolve_clarify_priority() -> None:
    # 模型澄清 + 合法 missing_slot → 按模型说的问
    assert slot_service.resolve_clarify(
        increment_intent="澄清", model_missing="granularity", merged={"metric": "总用电量"}
    ) == "granularity"
    # 模型说澄清但 missing_slot 非法 → 落回规则；metric 有值 → 不澄清（不让模型卡流程）
    assert slot_service.resolve_clarify(
        increment_intent="澄清", model_missing="weather", merged={"metric": "总用电量"}
    ) is None
    # 规则兜底：metric 缺失必澄清；time_range 缺失不澄清（默认「全期」）
    assert slot_service.resolve_clarify(
        increment_intent="查数", model_missing=None, merged={"time_range": "全期", "metric": None}
    ) == "metric"
    assert slot_service.resolve_clarify(
        increment_intent="查数", model_missing=None,
        merged={"time_range": None, "metric": "总用电量"},
    ) is None


# ── chat：增量继承 ──────────────────────────────────────────────────────────

_SQL_DAILY = "SELECT date, consumption_kwh FROM report_daily"

_SLOT_ROUND1 = {
    "time_range": "2020-08", "granularity": "月", "metric": "总用电量",
    "dimension": None, "filters": None, "compare": None,
    "intent": "趋势", "chart_hint": "折线",
}
_SLOT_ROUND2 = {  # 「只看周末的」：只给 filters，其余继承
    "time_range": None, "granularity": None, "metric": None, "dimension": None,
    "filters": [{"field": "is_weekend", "op": "=", "value": 1}],
    "compare": None, "intent": "对比", "chart_hint": None,
}


async def test_chat_inherits_session_slot(client, monkeypatch) -> None:
    # 本用例验证「LLM 增量抽取 + 会话继承」这条路：**关掉规则快通道**（v1.19.11 新增），
    # 否则快通道会少调一次 LLM，脚本模型的**按序规则**随之错位（断言就测不到本意了）
    monkeypatch.setattr(slot_service, "FASTPATH_ENABLED", False)
    token, user_id = await _register(client, "slot_owner_01")
    conversation = await _conversation(client, token)
    set_llm(
        ScriptedLLM(
            rules={
                "slot": [_SLOT_ROUND1, _SLOT_ROUND2],
                "gen_sql": {"sql": _SQL_DAILY},
            }
        )
    )

    first = await analysis_service.run_once(user_id, conversation, "2020 年 8 月的用电量")
    assert first.status == "success", first.error_message
    assert first.slot["time_range"] == "2020-08"

    second = await analysis_service.run_once(user_id, conversation, "只看周末的")
    assert second.status == "success", second.error_message
    assert second.slot["time_range"] == "2020-08"  # 继承
    assert second.slot["metric"] == "总用电量"  # 继承
    assert second.slot["granularity"] == "月"  # 继承
    assert second.slot["filters"] == [{"field": "is_weekend", "op": "=", "value": 1}]  # 本轮新增
    assert second.slot["intent"] == "对比"

    # 会话 slot_json = 合并后的完整 slot
    detail = await client.get(f"/api/v1/conversations/{conversation}", headers=_auth(token))
    assert detail.json()["data"]["slot"]["filters"][0]["field"] == "is_weekend"


# ── 澄清轮 ──────────────────────────────────────────────────────────────────

_SLOT_METRIC_MISSING = {
    "time_range": None, "granularity": None, "metric": None, "dimension": None,
    "filters": None, "compare": None, "intent": "查数", "chart_hint": None,
}

#: 澄清轮的探针问句：**必须真的认不出指标**。
#: ⚠️ 别用「用电情况怎么样」——2026-10-19 起「用电情况 / 用电状况」已在
#: `slot_service.GENERIC_METRIC_KEYWORDS` 里，规则兜底会直接填「总用电量」，
#: 澄清卡根本不会出现（这条边界由 `test_condition_wording_fills_metric` 单独钉住）。
_PROBE_NO_METRIC = "帮我看看这个月的用电"


async def test_metric_missing_runs_clarify_round(client) -> None:
    token, user_id = await _register(client, "slot_owner_02")
    conversation = await _conversation(client, token)
    set_llm(ScriptedLLM(rules={"slot": _SLOT_METRIC_MISSING}))

    result = await analysis_service.run_once(user_id, conversation, _PROBE_NO_METRIC)

    # 事件序列：status → slot → clarify → meta → done(record_id=null)（决策 18-2 / 20-2）
    assert [event["event"] for event in result.events] == [
        "status", "slot", "clarify", "meta", "done",
    ]
    assert result.events[-1]["data"]["record_id"] is None
    card = result.clarify
    assert card["missing_slot"] == "metric"
    # 选项由规则从 schema 可用指标生成（value 用中文名，与 slot.metric 同域）
    assert card["options"] == [{"label": "总用电量", "value": "总用电量"}]
    # slot 事件与 partial_slot 都是「合并后、填默认前」的部分 slot（metric 空位可见）
    slot_payload = _slot_event(result)
    assert slot_payload["metric"] is None
    assert _clarify_event(result)["partial_slot"]["metric"] is None
    assert result.slot["time_range"] is None  # 澄清轮不填「全期」默认

    conn = agent_connection()
    try:
        analyses = conn.execute(
            "SELECT COUNT(*) FROM analyses WHERE conversation_id = ("
            " SELECT id FROM conversations WHERE conversation_code = ?)",
            (conversation,),
        ).fetchone()[0]
        messages = conn.execute(
            "SELECT m.role, m.content, m.analysis_id FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.conversation_code = ? ORDER BY m.seq_no",
            (conversation,),
        ).fetchall()
        conv_slot = conn.execute(
            "SELECT slot_json FROM conversations WHERE conversation_code = ?", (conversation,)
        ).fetchone()[0]
    finally:
        conn.close()

    # 不落 analyses；落 messages×2（assistant 为澄清卡 JSON）；slot_json 不回写
    assert analyses == 0
    assert len(messages) == 2
    assert messages[0][0] == "user"
    assert messages[0][1] == _PROBE_NO_METRIC
    assert messages[0][2] is None  # 澄清轮消息不关联分析记录
    assert messages[1][0] == "assistant"
    assert messages[1][2] is None
    card_payload = json.loads(messages[1][1])
    assert card_payload["type"] == "clarify"
    assert card_payload["missing_slot"] == "metric"
    assert conv_slot is None  # 没执行成功，不产生脏芯片状态


async def test_condition_wording_fills_metric(client) -> None:
    """「XX 的用电情况」**不再**发澄清卡（2026-10-19 补词）：规则兜底直接填 `总用电量`。

    与 v1.19.6「过澄清」修复同一方向 —— 题面已经能唯一确定指标时，别拿澄清卡打扰用户。
    这里故意让**模型自己判 metric 缺失**（`_SLOT_METRIC_MISSING`），从而证明接住它的是规则兜底、
    而不是"模型碰巧给了指标"。（同一句话在补词之前会返回澄清卡，正是 q107 那条失败路径的入口。）
    """
    token, user_id = await _register(client, "slot_owner_08")
    conversation = await _conversation(client, token)
    set_llm(ScriptedLLM(rules={"slot": _SLOT_METRIC_MISSING, "gen_sql": {"sql": _SQL_DAILY}}))

    result = await analysis_service.run_once(user_id, conversation, "这个月的用电情况")

    assert result.clarify is None
    assert result.slot["metric"] == "总用电量"
    events = [event["event"] for event in result.events]
    assert "clarify" not in events
    assert "code" in events  # 直接走到了执行


async def test_model_clarify_signal_and_fallback(client) -> None:
    token, user_id = await _register(client, "slot_owner_03")
    conversation = await _conversation(client, token)

    # 模型澄清 + 合法 missing_slot → 按模型说的问（粒度）
    set_llm(
        ScriptedLLM(
            rules={
                "slot": {
                    "time_range": "全期", "granularity": None, "metric": "总用电量",
                    "dimension": None, "filters": None, "compare": None,
                    "intent": "澄清", "chart_hint": None, "missing_slot": "granularity",
                }
            }
        )
    )
    result = await analysis_service.run_once(user_id, conversation, "看看趋势")
    assert result.clarify["missing_slot"] == "granularity"
    assert [option["value"] for option in result.clarify["options"]] == ["小时", "日", "月", "年"]

    # 模型说澄清但 missing_slot 非法、metric 有值 → 照常执行（不让模型卡流程）
    set_llm(
        ScriptedLLM(
            rules={
                "slot": {
                    "time_range": "全期", "granularity": "月", "metric": "总用电量",
                    "dimension": None, "filters": None, "compare": None,
                    "intent": "澄清", "chart_hint": None, "missing_slot": "weather",
                },
                "gen_sql": {"sql": _SQL_DAILY},
            }
        )
    )
    normal = await analysis_service.run_once(user_id, conversation, "看看趋势")
    assert normal.status == "success"
    assert normal.clarify is None
    assert "code" in [event["event"] for event in normal.events]


# ── clarify 端点（回填后就地继续）──────────────────────────────────────────


async def _sse_events(client, token: str, path: str, payload: dict) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name: str | None = None
    async with client.stream("POST", path, headers=_auth(token), json=payload) as response:
        assert response.status_code == 200, response.text
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and name:
                events.append((name, json.loads(line.split(":", 1)[1].strip())))
                name = None
    return events


async def test_clarify_endpoint_roundtrip(client) -> None:
    token, user_id = await _register(client, "slot_owner_04")
    conversation = await _conversation(client, token)
    set_llm(ScriptedLLM(rules={"slot": _SLOT_METRIC_MISSING}))

    # ① 第一问触发澄清（SSE）
    events = await _sse_events(
        client, token, "/api/v1/analysis/chat",
        {"conversation_code": conversation, "message": _PROBE_NO_METRIC},
    )
    assert [name for name, _ in events] == ["status", "slot", "clarify", "meta", "done"]
    assert events[-1][1]["record_id"] is None

    # ② 点选回填 → clarify 就地继续（SSE，slot 为合并后的完整条件）
    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": _SQL_DAILY}}))
    events2 = await _sse_events(
        client, token, "/api/v1/analysis/clarify",
        {
            "conversation_code": conversation,
            "question": _PROBE_NO_METRIC,
            "slot_patch": {"metric": "总用电量"},
        },
    )
    names = [name for name, _ in events2]
    assert "clarify" not in names  # 补齐后正常执行
    slot_payload = dict(events2)["slot"]
    assert slot_payload["metric"] == "总用电量"
    assert slot_payload["time_range"] == "全期"  # 默认口径在继续执行时填入
    record_id = events2[-1][1]["record_id"]
    assert record_id.startswith("a_")

    # ③ 回填已持久化到会话 slot；消息 3 条（问、澄清卡、结果），无重复用户气泡
    detail = await client.get(f"/api/v1/conversations/{conversation}", headers=_auth(token))
    assert detail.json()["data"]["slot"]["metric"] == "总用电量"
    conn = agent_connection()
    try:
        rows = conn.execute(
            "SELECT m.role, m.analysis_id FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.conversation_code = ? ORDER BY m.seq_no",
            (conversation,),
        ).fetchall()
        analyses = conn.execute(
            "SELECT record_code, status FROM analyses WHERE record_code = ?", (record_id,)
        ).fetchone()
    finally:
        conn.close()
    assert [row[0] for row in rows] == ["user", "assistant", "assistant"]  # 决策 19-1
    assert rows[1][1] is None and rows[2][1] is not None
    assert analyses[1] == "success"

    # ④ 未登录 → 401；他人会话 → error 事件 E_NOT_FOUND
    anonymous = await client.post(
        "/api/v1/analysis/clarify",
        json={"conversation_code": conversation, "question": "q", "slot_patch": {}},
    )
    assert anonymous.status_code == 401
    other_token, _ = await _register(client, "slot_owner_04_other")
    foreign = await _sse_events(
        client, other_token, "/api/v1/analysis/clarify",
        {"conversation_code": conversation, "question": "q", "slot_patch": {"metric": "总用电量"}},
    )
    assert foreign[0][0] == "error"
    assert foreign[0][1]["code"] == "E_NOT_FOUND"


# ── replay 基线 / 芯片回填 / 切表清 slot ────────────────────────────────────


async def test_replay_uses_original_slot_baseline(client, monkeypatch) -> None:
    # 同 `test_chat_inherits_session_slot`：本用例靠脚本模型的按序 slot 规则，需关掉规则快通道
    monkeypatch.setattr(slot_service, "FASTPATH_ENABLED", False)
    token, user_id = await _register(client, "slot_owner_05")
    conversation = await _conversation(client, token)
    # 第三次 slot 调用（replay 的增量抽取）刻意全空：合并结果完全取决于基线，
    # 从而验证「基线 = 原记录 slot」而不是会话当前 slot（决策 19-4）
    _SLOT_ALL_NULL = {
        "time_range": None, "granularity": None, "metric": None, "dimension": None,
        "filters": None, "compare": None, "intent": "查数", "chart_hint": None,
    }
    set_llm(
        ScriptedLLM(
            rules={
                "slot": [_SLOT_ROUND1, {"metric": "峰段用电量"}, _SLOT_ALL_NULL],
                "gen_sql": {"sql": _SQL_DAILY},
            }
        )
    )
    first = await analysis_service.run_once(user_id, conversation, "2020 年 8 月的用电量")
    second = await analysis_service.run_once(user_id, conversation, "那峰段呢")
    assert second.slot["metric"] == "峰段用电量"  # 会话 slot 已被追问改掉

    replay = await client.post(
        f"/api/v1/analysis/records/{first.record_code}/replay", headers=_auth(token)
    )
    assert replay.status_code == 200, replay.text
    data = replay.json()["data"]
    # 基线 = 原记录 slot：后续追问改过的 metric 不污染「并排比较」
    assert data["slot"]["metric"] == "总用电量"
    assert data["slot"]["time_range"] == "2020-08"
    assert data["replayed_from"] == first.record_code


async def test_chat_with_slot_patch_chip_edit(client) -> None:
    token, user_id = await _register(client, "slot_owner_06")
    conversation = await _conversation(client, token)
    set_llm(
        ScriptedLLM(
            rules={
                "slot": {"time_range": None, "granularity": "月", "metric": None,
                         "dimension": None, "filters": None, "compare": None,
                         "intent": "查数", "chart_hint": None},
                "gen_sql": {"sql": _SQL_DAILY},
            }
        )
    )
    events = await _sse_events(
        client, token, "/api/v1/analysis/chat",
        {
            "conversation_code": conversation,
            "message": "看看",
            "slot_patch": {"metric": "峰段用电量"},  # 芯片编辑（决策 20-1）
        },
    )
    slot_payload = dict(events)["slot"]
    assert slot_payload["metric"] == "峰段用电量"  # 芯片回填先生效
    assert slot_payload["granularity"] == "月"
    detail = await client.get(f"/api/v1/conversations/{conversation}", headers=_auth(token))
    assert detail.json()["data"]["slot"]["metric"] == "峰段用电量"


async def test_switch_report_clears_slot(client) -> None:
    token, user_id = await _register(client, "slot_owner_07")
    conversation = await _conversation(client, token)
    set_llm(
        ScriptedLLM(
            rules={"slot": _SLOT_ROUND1, "gen_sql": {"sql": _SQL_DAILY}}
        )
    )
    result = await analysis_service.run_once(user_id, conversation, "2020 年 8 月的用电量")
    assert result.slot["metric"] == "总用电量"

    # 切换活跃报表 → slot 清空（决策 18-6）
    switched = await client.post(
        "/api/v1/reports/r_ghost/activate", headers=_auth(token),
        json={"conversation_code": conversation},
    )
    assert switched.status_code == 200, switched.text
    detail = await client.get(f"/api/v1/conversations/{conversation}", headers=_auth(token))
    assert detail.json()["data"]["slot"] == {}
