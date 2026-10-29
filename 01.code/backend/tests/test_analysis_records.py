"""分析记录接口测试（交付物 4 第二批：列表 / 详情 / 重跑 / 删除）。

覆盖本轮 3 条决策：
- 决策 1 列表：**offset 分页 + total** + 组合筛选（会话/报表/状态/关键词/日期）+ 稳定排序
- 决策 2 重跑：整轮重跑 + **新建记录** + **锁定原报表**（不改会话活跃报表）
- 决策 3 详情：全量（meta/slot/sql/result/narrative）+ `llm_calls` 明细
"""

from __future__ import annotations

import json

import pytest

from app.agent import prompts
from app.core.llm_client import ScriptedLLM, set_llm
from app.services import analysis_service
from tests.conftest import agent_connection

pytestmark = pytest.mark.anyio

_CSV = (
    "日期,时间,总用电量(kWh)\n"
    "16/12/2006,17:24:00,4.000\n"
    "17/12/2006,17:25:00,5.000\n"
    "18/12/2006,17:26:00,6.000\n"
)


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


async def _uploaded_report(client, token: str) -> tuple[str, str]:
    uploaded = await client.post(
        "/api/v1/reports/upload",
        headers=_auth(token),
        files={"file": ("记录用报表.csv", _CSV.encode("utf-8"), "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    data = uploaded.json()["data"]
    confirmed = await client.post(
        f"/api/v1/reports/{data['report_code']}/confirm", headers=_auth(token)
    )
    assert confirmed.status_code == 200, confirmed.text
    return data["report_code"], data["table_name"]


async def _run(client, token: str, user_id: int, conversation: str, question: str, sql: str) -> str:
    """跑一轮问答（注入 SQL），返回 record_code。"""
    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": sql}}))
    result = await analysis_service.run_once(user_id, conversation, question)
    assert result.status == "success", result.error_message
    return result.record_code


# ── 列表（决策 1）──────────────────────────────────────────────────────────

async def test_list_records_pagination_and_filters(client) -> None:
    token, user_id = await _register(client, "rec_owner_01")
    conversation = await _conversation(client, token)
    codes = []
    for index in range(3):
        codes.append(
            await _run(
                client, token, user_id, conversation, f"第 {index} 个问题 趋势",
                "SELECT COUNT(*) AS cnt FROM report_daily",
            )
        )

    base = await client.get("/api/v1/analysis/records", headers=_auth(token))
    assert base.status_code == 200, base.text
    body = base.json()["data"]
    assert body["total"] == 3
    assert body["page"] == 1 and body["page_size"] == 20
    assert [item["record_code"] for item in body["items"]] == list(reversed(codes))
    assert body["status_count"] == {"success": 3}

    # 列表是**轻字段**：不含 SQL 与解读正文
    assert "sql" not in body["items"][0]
    assert "narrative" not in body["items"][0]
    assert body["items"][0]["llm_tokens"] > 0
    assert body["items"][0]["conversation_code"] == conversation
    assert body["items"][0]["report_code"] == "r_daily"

    # 分页：page_size=2 → 两页，且第 2 页只有 1 条
    first = await client.get(
        "/api/v1/analysis/records?page=1&page_size=2", headers=_auth(token)
    )
    second = await client.get(
        "/api/v1/analysis/records?page=2&page_size=2", headers=_auth(token)
    )
    assert len(first.json()["data"]["items"]) == 2
    assert len(second.json()["data"]["items"]) == 1
    assert first.json()["data"]["total"] == 3

    # 越界参数
    assert (await client.get("/api/v1/analysis/records?page=0", headers=_auth(token))).status_code == 400
    assert (
        await client.get("/api/v1/analysis/records?page_size=999", headers=_auth(token))
    ).status_code == 400
    assert (
        await client.get("/api/v1/analysis/records?status=unknown", headers=_auth(token))
    ).status_code == 400


async def test_list_records_filters_by_keyword_status_and_scope(client) -> None:
    token, user_id = await _register(client, "rec_owner_02")
    other_token, other_id = await _register(client, "rec_other_02")
    conversation = await _conversation(client, token)
    await _run(
        client, token, user_id, conversation, "按月看用电趋势",
        "SELECT COUNT(*) AS cnt FROM report_daily",
    )
    # 造一条失败记录（守卫拦截）
    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": "SELECT * FROM users"}}))
    failed = await analysis_service.run_once(user_id, conversation, "越权查询")
    assert failed.status == "failed"

    by_keyword = await client.get(
        "/api/v1/analysis/records?q=越权", headers=_auth(token)
    )
    assert by_keyword.json()["data"]["total"] == 1
    assert by_keyword.json()["data"]["items"][0]["status"] == "failed"
    assert by_keyword.json()["data"]["items"][0]["error_code"] == "E_SQL_FORBIDDEN"

    by_status = await client.get(
        "/api/v1/analysis/records?status=success", headers=_auth(token)
    )
    assert by_status.json()["data"]["total"] == 1
    assert by_status.json()["data"]["status_count"] == {"success": 1, "failed": 1}

    by_date = await client.get(
        f"/api/v1/analysis/records?date_from=2000-01-01&date_to=2000-01-02", headers=_auth(token)
    )
    assert by_date.json()["data"]["total"] == 0

    by_conversation = await client.get(
        f"/api/v1/analysis/records?conversation_code={conversation}", headers=_auth(token)
    )
    assert by_conversation.json()["data"]["total"] == 2

    # 归属隔离：他人看不到（且不报错，返回空列表）
    foreign = await client.get("/api/v1/analysis/records", headers=_auth(other_token))
    assert foreign.json()["data"]["total"] == 0

    # 不存在的会话/报表编号 → 404
    missing = await client.get(
        "/api/v1/analysis/records?conversation_code=c_19700101_0001", headers=_auth(token)
    )
    assert missing.status_code == 404


# ── 详情（决策 3）──────────────────────────────────────────────────────────

async def test_record_detail_returns_full_contract(client, monkeypatch) -> None:
    # 本用例断言**三阶段调用明细**（slot / gen_sql / narrative）→ 关掉规则快通道（v1.19.26）：
    # 「日报总共多少行」属于行数题，会被快通道命中而省掉 slot 调用。
    monkeypatch.setattr(analysis_service.slot_service, "FASTPATH_ENABLED", False)
    token, user_id = await _register(client, "rec_owner_03")
    conversation = await _conversation(client, token)
    code = await _run(
        client, token, user_id, conversation, "日报总共多少行",
        "SELECT COUNT(*) AS cnt FROM report_daily",
    )

    response = await client.get(f"/api/v1/analysis/records/{code}", headers=_auth(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["record_code"] == code
    assert data["question"] == "日报总共多少行"
    assert data["conversation_code"] == conversation
    assert data["report_code"] == "r_daily"
    assert data["status"] == "success"
    assert data["route"] == "sql"
    # 与常量比对（**不要写死版本号**）：本断言要钉的是"详情里如实回显了当前提示词版本"，
    # 而不是"版本号恰好等于 v3.5"——写死会在每次升版本时误报失败（v3.6 迭代时踩到）
    assert data["prompt_version"] == prompts.PROMPT_VERSION
    assert data["schema_version"] == "caliber-v1.0"
    assert data["can_replay"] is True
    assert data["replayed_from"] is None

    # slot 8 字段齐备
    assert set(data["slot"]) == {
        "time_range", "granularity", "metric", "dimension", "filters", "compare",
        "intent", "chart_hint",
    }
    # SQL 是**过守卫的最终语句**（含补的 LIMIT）
    assert data["sql"].upper().startswith("SELECT")
    assert "LIMIT 1000" in data["sql"].upper()
    # 结果快照
    assert data["result"]["row_count"] == 1
    assert data["result"]["rows"][0]["cnt"] == 0
    assert data["result"]["truncated"] is False
    assert data["narrative"]
    # 三个阶段调用明细 + token 合计
    assert [call["stage"] for call in data["llm_calls"]] == ["slot", "gen_sql", "narrative"]
    assert data["llm_tokens"] == sum(call["total_tokens"] for call in data["llm_calls"])
    # 单值 KPI（COUNT(*)）画不出图 → 只发 chart 事件不写 figure，故详情里 chart 为 null
    assert data["chart"] is None

    # 失败记录的详情：不编造，结果为空
    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": "DROP TABLE report_daily"}}))
    failed = await analysis_service.run_once(user_id, conversation, "删表")
    failed_detail = await client.get(
        f"/api/v1/analysis/records/{failed.record_code}", headers=_auth(token)
    )
    fdata = failed_detail.json()["data"]
    assert fdata["status"] == "failed"
    assert fdata["error_code"] == "E_SQL_FORBIDDEN"
    assert fdata["narrative"] is None
    assert fdata["result"]["rows"] == []
    assert fdata["result"]["row_count"] is None  # 没跑过 → 不写结果


async def test_record_detail_isolation(client) -> None:
    token, user_id = await _register(client, "rec_owner_04")
    other_token, _ = await _register(client, "rec_other_04")
    conversation = await _conversation(client, token)
    code = await _run(
        client, token, user_id, conversation, "问题",
        "SELECT COUNT(*) AS cnt FROM report_daily",
    )

    foreign = await client.get(f"/api/v1/analysis/records/{code}", headers=_auth(other_token))
    assert foreign.status_code == 404
    missing = await client.get("/api/v1/analysis/records/a_999999", headers=_auth(token))
    assert missing.status_code == 404


# ── 重跑（决策 2）──────────────────────────────────────────────────────────

async def test_replay_creates_new_record_and_locks_original_report(client) -> None:
    token, user_id = await _register(client, "rec_owner_05")
    report_code, table = await _uploaded_report(client, token)
    conversation = await _conversation(client, token)
    # 会话活跃报表切到上传表（非预置）
    await client.post(
        f"/api/v1/reports/{report_code}/activate",
        headers=_auth(token),
        json={"conversation_code": conversation},
    )
    original = await _run(
        client, token, user_id, conversation, f"总用电量",
        f'SELECT SUM(total_kwh) AS total_kwh FROM "{table}"',
    )

    # 重跑前把会话活跃报表切回 r_daily：重跑必须**仍作用于原报表**
    await client.post(
        f"/api/v1/reports/r_daily/activate",
        headers=_auth(token),
        json={"conversation_code": conversation},
    )
    set_llm(
        ScriptedLLM(
            rules={"gen_sql": {"sql": f'SELECT COUNT(*) AS cnt FROM "{table}"'}}
        )
    )
    replayed = await client.post(
        f"/api/v1/analysis/records/{original}/replay", headers=_auth(token)
    )
    assert replayed.status_code == 200, replayed.text
    data = replayed.json()["data"]

    assert data["record_code"] != original
    assert data["replayed_from"] == original
    assert data["status"] == "success"
    assert data["report_code"] == report_code  # 锁定原报表
    assert data["question"] == f"总用电量"  # 沿用原问题
    assert data["result"]["rows"][0]["cnt"] == 3

    # 会话活跃报表**未被重跑改写**
    detail = await client.get(
        f"/api/v1/conversations/{conversation}", headers=_auth(token)
    )
    assert detail.json()["data"]["active_report_code"] == "r_daily"

    # 两条记录都在列表里（重跑不覆盖历史）
    listing = await client.get("/api/v1/analysis/records", headers=_auth(token))
    codes = {item["record_code"] for item in listing.json()["data"]["items"]}
    assert {original, data["record_code"]} <= codes


async def test_replay_from_readonly_eval_conversation_goes_to_new_one(client) -> None:
    """只读的评测批次会话（`c_eval_*`）里的记录：重跑**另起新会话**，不污染批次留痕。

    背景（v1.19.12 用户实测）：我的分析记录里混着评测批次产生的记录，对它们点「重跑」若写回
    原会话，会给评测留痕会话塞进新的提问与回答（批次会话用户不会在里面提问）。
    """
    token, user_id = await _register(client, "rec_owner_05b")
    conversation = await _conversation(client, token)
    original = await _run(
        client, token, user_id, conversation, "总用电量",
        "SELECT SUM(consumption_kwh) AS total_kwh FROM report_daily",
    )

    # 把会话编号改成评测批次会话（模拟"该记录来自评测批次"）
    readonly_code = "c_eval_20260918_99"
    with agent_connection() as conn:
        conn.execute(
            "UPDATE conversations SET conversation_code = ? WHERE conversation_code = ?",
            (readonly_code, conversation),
        )
        conn.commit()

    replayed = await client.post(
        f"/api/v1/analysis/records/{original}/replay", headers=_auth(token)
    )
    assert replayed.status_code == 200, replayed.text
    data = replayed.json()["data"]

    assert data["status"] == "success"
    assert data["replayed_from"] == original
    # 落到了**新会话**（不是评测批次会话）
    assert data["conversation_code"] != readonly_code
    assert not data["conversation_code"].startswith("c_eval_")
    assert "评测批次会话" in (data["replay_note"] or "")

    # 评测会话留痕保持干净：只有原始的一问一答
    with agent_connection() as conn:
        kept = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id ="
            " (SELECT id FROM conversations WHERE conversation_code = ?)",
            (readonly_code,),
        ).fetchone()[0]
    assert kept == 2


async def test_replay_rejects_when_original_report_gone(client) -> None:
    token, user_id = await _register(client, "rec_owner_06")
    report_code, table = await _uploaded_report(client, token)
    conversation = await _conversation(client, token)
    await client.post(
        f"/api/v1/reports/{report_code}/activate",
        headers=_auth(token),
        json={"conversation_code": conversation},
    )
    original = await _run(
        client, token, user_id, conversation, "总用电量",
        f'SELECT SUM(total_kwh) AS total_kwh FROM "{table}"',
    )

    # 删除原报表 → 其分析记录按 FK 级联删除（数据库设计 6.2 的既定行为），
    # 因此重跑请求拿到 404 而不是 400：记录本身已不复存在
    assert (
        await client.delete(f"/api/v1/reports/{report_code}", headers=_auth(token))
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/analysis/records/{original}", headers=_auth(token))
    ).status_code == 404
    response = await client.post(
        f"/api/v1/analysis/records/{original}/replay", headers=_auth(token)
    )
    assert response.status_code == 404


# ── 删除 ────────────────────────────────────────────────────────────────────

async def test_delete_record_cascades_llm_calls_and_keeps_messages(client) -> None:
    token, user_id = await _register(client, "rec_owner_07")
    conversation = await _conversation(client, token)
    code = await _run(
        client, token, user_id, conversation, "删除测试",
        "SELECT COUNT(*) AS cnt FROM report_daily",
    )

    # 先记下该记录的 analysis_id 与所属会话（删除后无法再反查）
    conn = agent_connection()
    try:
        analysis_id, conversation_id = conn.execute(
            "SELECT id, conversation_id FROM analyses WHERE record_code = ?", (code,)
        ).fetchone()
    finally:
        conn.close()

    response = await client.delete(f"/api/v1/analysis/records/{code}", headers=_auth(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["record_code"] == code
    assert data["deleted_llm_calls"] == 3
    assert data["deleted_messages"] == 1  # 助手消息解除引用

    assert (
        await client.get(f"/api/v1/analysis/records/{code}", headers=_auth(token))
    ).status_code == 404

    conn = agent_connection()
    try:
        calls = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE analysis_id = ?", (analysis_id,)
        ).fetchone()[0]
        analysis_row = conn.execute(
            "SELECT COUNT(*) FROM analyses WHERE record_code = ?", (code,)
        ).fetchone()[0]
        assistant = conn.execute(
            "SELECT content, analysis_id FROM messages WHERE conversation_id = ? "
            "AND role = 'assistant' ORDER BY id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        user_message = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = 'user'",
            (conversation_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert calls == 0  # llm_calls 随记录级联删除
    assert analysis_row == 0
    assert assistant[1] is None  # 对话文本保留、引用置空
    assert user_message == 1


async def test_delete_record_isolation(client) -> None:
    token, user_id = await _register(client, "rec_owner_08")
    other_token, _ = await _register(client, "rec_other_08")
    conversation = await _conversation(client, token)
    code = await _run(
        client, token, user_id, conversation, "别人的记录",
        "SELECT COUNT(*) AS cnt FROM report_daily",
    )

    foreign = await client.delete(f"/api/v1/analysis/records/{code}", headers=_auth(other_token))
    assert foreign.status_code == 404
    # 原记录仍在
    assert (
        await client.get(f"/api/v1/analysis/records/{code}", headers=_auth(token))
    ).status_code == 200


async def test_records_endpoints_require_auth(client) -> None:
    assert (await client.get("/api/v1/analysis/records")).status_code == 401
    assert (await client.get("/api/v1/analysis/records/a_000001")).status_code == 401
    assert (await client.post("/api/v1/analysis/records/a_000001/replay")).status_code == 401
    assert (await client.delete("/api/v1/analysis/records/a_000001")).status_code == 401
