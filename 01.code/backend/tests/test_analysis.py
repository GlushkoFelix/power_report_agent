"""分析链路测试（交付物 4 第一批：单轮问答最小闭环）。

覆盖本轮 8 条决策中可离线验证的部分：
- 决策 2 可注入 LLM：测试注入 `ScriptedLLM`，全程不联网
- 决策 4 全留痕：`messages`×2 + `analyses` + `llm_calls` + 会话回写
- 决策 5 编号命名：`analyses.record_code`
- 决策 6 失败全留痕：守卫 / 执行 / 解析三类失败都落 `status='failed'`
- 决策 7 三层解析容错：`json.loads` → `json_repair` → 代码块
"""

from __future__ import annotations

import json

import anyio
import pytest

from app.agent import prompts
from app.core import sql_guard
from app.core.errors import BizError
from app.core.llm_client import ScriptedLLM, extract_json, llm_status, set_llm
from app.services import analysis_service
from tests.conftest import agent_connection

pytestmark = pytest.mark.anyio

_CSV = (
    "日期,时间,总用电量(kWh),电压\n"
    "16/12/2006,17:24:00,4.000,234.0\n"
    "17/12/2006,17:25:00,5.000,235.0\n"
    "18/12/2006,17:26:00,6.000,236.0\n"
)


@pytest.fixture(autouse=True)
def _scripted_llm():
    """每个用例都从离线脚本模型起步（不联网、不花 token、结果可复现）。"""
    set_llm(ScriptedLLM())
    yield
    set_llm(None)


async def _register(client, username: str) -> tuple[str, int]:
    """注册并返回（token, user_id）——临时库跨用例累积用户，**不能假设 id 恒为 1**。"""
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
    """上传并确认一份 3 行中文 CSV，返回（report_code, table_name）。"""
    uploaded = await client.post(
        "/api/v1/reports/upload",
        headers=_auth(token),
        files={"file": ("分析用报表.csv", _CSV.encode("utf-8"), "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    data = uploaded.json()["data"]
    confirmed = await client.post(
        f"/api/v1/reports/{data['report_code']}/confirm", headers=_auth(token)
    )
    assert confirmed.status_code == 200, confirmed.text
    return data["report_code"], data["table_name"]


# ── 成功链路 + 全留痕 ──────────────────────────────────────────────────────

async def test_run_once_success_persists_everything(client, monkeypatch) -> None:
    # 本用例断言**完整调用序列**（含 slot）→ 显式关掉规则快通道（v1.19.26）：
    # 「日报一共有多少行？」属于**行数题**，现在会被快通道认出指标「记录行数」而省掉 slot 调用
    # （这正是 v1.19.26 修 q096 的那个改动；快通道命中本身由 test_prompt_iteration.py 覆盖）。
    monkeypatch.setattr(analysis_service.slot_service, "FASTPATH_ENABLED", False)
    token, user_id = await _register(client, "ana_owner_01")
    conversation = await _conversation(client, token)

    set_llm(
        ScriptedLLM(
            rules={
                "gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily", "reason": "计数"}
            }
        )
    )
    result = await analysis_service.run_once(
        user_id=user_id,
        conversation_code=conversation,
        question="日报一共有多少行？",
    )

    assert result.status == "success", result.error_message
    assert result.record_code.startswith("a_")
    assert result.table_name == "report_daily"
    assert result.sql and "LIMIT" in result.sql.upper()  # 守卫补了 LIMIT
    assert result.row_count == 1
    assert result.rows[0]["cnt"] == 0  # 临时库的预置表为空
    assert result.narrative
    assert result.llm_tokens > 0

    # SSE 事件顺序（PRD 9.2；`chart` 固定落在 table 之后、narrative 之前）
    assert [event["event"] for event in result.events] == [
        "status", "slot", "code", "status", "table", "chart", "narrative", "meta", "done",
    ]

    conn = agent_connection()
    try:
        analysis_row = conn.execute(
            "SELECT record_code, status, route, sql_text, result_row_count, narrative, "
            "prompt_version, schema_version, error_code FROM analyses "
            "WHERE record_code = ?",
            (result.record_code,),
        ).fetchone()
        # 必须按**本用例的会话/记录**过滤：临时库跨用例累积数据，
        # 不加过滤会变成"依赖 pytest 收集顺序"的假通过
        messages = conn.execute(
            "SELECT m.seq_no, m.role, m.content, m.analysis_id FROM messages m "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE c.conversation_code = ? ORDER BY m.seq_no",
            (conversation,),
        ).fetchall()
        calls = conn.execute(
            "SELECT l.stage, l.model_name, l.total_tokens FROM llm_calls l "
            "JOIN analyses a ON a.id = l.analysis_id "
            "WHERE a.record_code = ? ORDER BY l.id",
            (result.record_code,),
        ).fetchall()
        conversation_row = conn.execute(
            "SELECT slot_json, message_count, title FROM conversations WHERE conversation_code = ?",
            (conversation,),
        ).fetchone()
    finally:
        conn.close()

    assert analysis_row[1] == "success"
    assert analysis_row[2] == "sql"
    assert analysis_row[3].upper().startswith("SELECT")
    assert analysis_row[4] == 1
    assert "cnt" not in (analysis_row[5] or "")  # narrative 文本不含列名噪声
    # 与常量比对（**不要写死版本号**）：钉的是"落库时记下了当前提示词版本"，避免每次升版本误报
    assert analysis_row[6] == prompts.PROMPT_VERSION  # v3.5 起：pandas 代码不再写 import
    assert analysis_row[7] == "caliber-v1.0"
    assert analysis_row[8] is None

    assert len(messages) == 2
    assert [row[1] for row in messages] == ["user", "assistant"]
    assert messages[0][2] == "日报一共有多少行？"
    assert messages[1][3] is not None  # 助手消息关联分析记录

    assert [row[0] for row in calls] == ["slot", "gen_sql", "narrative"]

    assert json.loads(conversation_row[0])["granularity"] == "全期"  # slot 回写
    assert conversation_row[1] == 2
    assert conversation_row[2] == "日报一共有多少行？"


async def test_run_once_executes_against_uploaded_table(client) -> None:
    """上传表 + 真实数据：验证执行、表格快照与解读走通。"""
    token, user_id = await _register(client, "ana_owner_02")
    report_code, table = await _uploaded_report(client, token)
    conversation = await _conversation(client, token)
    switched = await client.post(
        f"/api/v1/reports/{report_code}/activate",
        headers=_auth(token),
        json={"conversation_code": conversation},
    )
    assert switched.status_code == 200, switched.text

    set_llm(
        ScriptedLLM(
            rules={
                "gen_sql": {
                    "sql": f'SELECT COUNT(*) AS rows_cnt, SUM(total_kwh) AS total_kwh FROM "{table}"'
                }
            }
        )
    )
    result = await analysis_service.run_once(user_id, conversation, "总用电量是多少？")

    assert result.status == "success", result.error_message
    assert result.table_name == table
    assert result.row_count == 1
    assert result.rows[0]["total_kwh"] == pytest.approx(15.0)
    assert {column["name"] for column in result.columns} == {"rows_cnt", "total_kwh"}
    assert result.truncated is False


# ── 失败留痕（决策 6）───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("sql", "error_code", "rule_id"),
    [
        ("DROP TABLE report_daily", "E_SQL_FORBIDDEN", "sqlglot:non_select"),
        ("SELECT * FROM users", "E_SQL_FORBIDDEN", "sqlglot:table_not_allowed"),
    ],
)
async def test_run_once_blocks_dangerous_sql(client, sql: str, error_code: str, rule_id: str) -> None:
    token, user_id = await _register(client, f"ana_block_{abs(hash(sql)) % 1000}")
    conversation = await _conversation(client, token)
    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": sql}}))

    result = await analysis_service.run_once(user_id, conversation, "帮我删掉这张表")

    assert result.status == "failed"
    assert result.error_code == error_code
    assert result.narrative == ""  # P4：不编造
    # 失败事件序列：error 之后仍发 done（携带 record_id，前端据此停止 loading 并可跳详情）
    assert [event["event"] for event in result.events][-2:] == ["error", "done"]
    assert result.events[-2]["data"]["detail"].endswith(f"rule={rule_id}")

    conn = agent_connection()
    try:
        row = conn.execute(
            "SELECT status, error_code, narrative, result_rows_json FROM analyses "
            "WHERE record_code = ?",
            (result.record_code,),
        ).fetchone()
        assistant = conn.execute(
            "SELECT content FROM messages WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        audit = conn.execute(
            "SELECT event_type, severity, action, rule_id FROM audit_logs "
            "WHERE event_type = 'sql_blocked' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == "failed"
    assert row[1] == error_code
    assert row[2] is None  # narrative 不写
    assert row[3] is None  # 没执行过，无结果快照
    assert "未生成结果" in assistant
    assert audit[0] == "sql_blocked"
    assert audit[1] == "high"
    assert audit[2] == "blocked"
    assert audit[3] == rule_id


async def test_run_once_fails_when_model_output_unparsable(client) -> None:
    token, user_id = await _register(client, "ana_owner_03")
    conversation = await _conversation(client, token)
    set_llm(ScriptedLLM(rules={"gen_sql": "这不是 JSON，也不是 SQL"}))

    result = await analysis_service.run_once(user_id, conversation, "随便问点什么")

    assert result.status == "failed"
    assert result.error_code == "E_LLM"
    assert [event["event"] for event in result.events][-2:] == ["error", "done"]

    conn = agent_connection()
    try:
        status = conn.execute(
            "SELECT status, error_code FROM analyses WHERE record_code = ?",
            (result.record_code,),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(status) == ("failed", "E_LLM")


async def test_run_once_rejects_missing_conversation(client) -> None:
    token, user_id = await _register(client, "ana_owner_04")
    with pytest.raises(BizError) as excinfo:
        await analysis_service.run_once(user_id, "c_19700101_0001", "会话不存在")
    assert excinfo.value.http_status == 404


# ── SSE 端到端（决策 3）────────────────────────────────────────────────────

async def test_chat_sse_event_frames(client) -> None:
    token, _ = await _register(client, "ana_owner_05")
    conversation = await _conversation(client, token)
    set_llm(
        ScriptedLLM(
            rules={"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}}
        )
    )

    events: list[tuple[str, dict]] = []
    async with client.stream(
        "POST",
        "/api/v1/analysis/chat",
        headers=_auth(token),
        json={"conversation_code": conversation, "message": "日报有多少行？"},
    ) as response:
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("text/event-stream")
        name: str | None = None
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and name:
                events.append((name, json.loads(line.split(":", 1)[1].strip())))
                name = None

    names = [event for event, _ in events]
    assert names == [
        "status", "slot", "code", "status", "table", "chart", "narrative", "meta", "done",
    ]
    assert events[0][1] == {"stage": "parsing"}
    assert events[3][1] == {"stage": "executing"}
    table_payload = events[4][1]
    assert table_payload["row_count"] == 1
    # 单值 KPI（COUNT(*)）画不出图 → chart 事件仍发，但 figure=null + fallback（决策 ⑧）
    chart_payload = events[5][1]
    assert chart_payload["figure"] is None
    assert chart_payload["fallback"] == "table"
    assert chart_payload["reason"]
    assert events[-1][1]["record_id"].startswith("a_")


async def test_chat_sse_reports_error_in_stream(client) -> None:
    """上下文级失败（会话不存在）也要以 error 事件收尾，而不是空流。"""
    token, _ = await _register(client, "ana_owner_06")
    async with client.stream(
        "POST",
        "/api/v1/analysis/chat",
        headers=_auth(token),
        json={"conversation_code": "c_19700101_0002", "message": "在吗"},
    ) as response:
        body = "".join([line async for line in response.aiter_lines()])
    assert "event: error" in body
    assert "E_NOT_FOUND" in body


async def test_chat_requires_auth(client) -> None:
    response = await client.post(
        "/api/v1/analysis/chat",
        json={"conversation_code": "c_20260915_0001", "message": "hi"},
    )
    assert response.status_code == 401


# ── 离线脚本模型的两条可用性保障（前端联调实测倒逼）────────────────────────

class _SlowLLM:
    """包装 `ScriptedLLM`：指定 `purpose` 上人为变慢，用于验证**阶段超时兜底**（v1.19.11）。

    真实的超时是服务端长尾（实测 slot 最长 21s、narrative 17s），测试里没法稳定构造，
    所以用"睡眠 + 把超时阈值调到极小"来触发同一条分支。
    """

    def __init__(self, inner, slow_purpose: str, delay: float) -> None:
        self._inner = inner
        self._slow_purpose = slow_purpose
        self._delay = delay

    def __getattr__(self, item):  # 透传 name / configured / supports_model_tiers 等
        return getattr(self._inner, item)

    async def chat(self, messages, **kwargs):
        if kwargs.get("purpose") == self._slow_purpose:
            await anyio.sleep(self._delay)
        return await self._inner.chat(messages, **kwargs)


async def test_slot_timeout_falls_back_to_rule(client, monkeypatch) -> None:
    """slot 超时**不干等**：按规则兜底继续（实测 slot 最长 21s，是 P95 的主要来源）。

    问题刻意选「按月份汇总…」——它**不走规则快通道**（含分组词），所以必然落到 LLM 路径，
    才能验证"LLM 超时 → 宽松规则兜底"这条分支。
    """
    token, user_id = await _register(client, "ana_slot_timeout")
    conversation = await _conversation(client, token)
    set_llm(
        _SlowLLM(
            ScriptedLLM(rules={"gen_sql": {"sql": "SELECT COUNT(*) AS days FROM report_daily"}}),
            slow_purpose="slot",
            delay=5.0,
        )
    )
    monkeypatch.setattr(analysis_service, "SLOT_TIMEOUT_SECONDS", 0.05)

    result = await analysis_service.run_once(user_id, conversation, "按月份汇总全期的总用电量。")

    assert result.status == "success", result.error_message
    assert result.slot["metric"] == "总用电量"  # 规则兜底拿到的指标
    detail = await client.get(
        f"/api/v1/analysis/records/{result.record_code}", headers=_auth(token)
    )
    stages = [item["stage"] for item in detail.json()["data"]["llm_calls"]]
    assert "slot" not in stages, stages


async def test_narrative_timeout_is_not_fatal(client, monkeypatch) -> None:
    """解读超时**不再无解读**：退化为「自动摘要」，表格照给、`status` 仍 success、提示如实说明。

    **2026-10-14 口径变更**（需求方明确：解读是本项目的交付物）：超时不再让 `narrative` 为空，
    而是用**结果表能直接算出来的事实**生成「自动摘要」并如实标注来源；
    表格与图表仍不受影响（原有断言保留）。
    """
    token, user_id = await _register(client, "ana_narr_timeout")
    conversation = await _conversation(client, token)
    set_llm(
        _SlowLLM(
            ScriptedLLM(rules={"gen_sql": {"sql": "SELECT COUNT(*) AS days FROM report_daily"}}),
            slow_purpose="narrative",
            delay=5.0,
        )
    )
    monkeypatch.setattr(analysis_service, "NARRATIVE_TIMEOUT_SECONDS", 0.05)

    result = await analysis_service.run_once(user_id, conversation, "全期总用电量是多少？")

    assert result.status == "success", result.error_message
    assert result.narrative.startswith("自动摘要"), result.narrative
    assert result.table_ready is True  # 表格与图表不受影响
    assert any("解读" in item and "超时" in item for item in result.anomalies), result.anomalies


async def test_slot_fastpath_skips_llm_call(client) -> None:
    """规则快通道：简单明确的问题**不调 slot LLM**，slot 结果仍与 LLM 路径同构（v1.19.11 延迟专项）。

    断言走**接口**（`GET /analysis/records/{code}` 的 `llm_calls`）而不是直连临时库——
    实测踩到过：某次 pytest 运行里 conftest 被导入两次、产生**两个临时库**，
    直连的助手函数连的库和应用写的库不是同一个（表现为"库里什么都没有"）。
    """
    token, user_id = await _register(client, "ana_fast_01")
    conversation = await _conversation(client, token)

    result = await analysis_service.run_once(user_id, conversation, "全期总用电量是多少？")

    assert result.status == "success", result.error_message
    assert result.slot["metric"] == "总用电量"
    assert result.slot["time_range"] == "全期"

    detail = await client.get(
        f"/api/v1/analysis/records/{result.record_code}", headers=_auth(token)
    )
    assert detail.status_code == 200, detail.text
    stages = [item["stage"] for item in detail.json()["data"]["llm_calls"]]

    assert "slot" not in stages, f"快通道命中却仍调了 slot：{stages}"
    assert set(stages) == {"gen_sql", "narrative"}, stages


async def test_scripted_narrative_fills_placeholders(client) -> None:
    """脚本模型的解读模板必须**填成完整句子**，不能把 `{{row_count}}` 原样丢给用户。

    背景：`ScriptedLLM` 的规则里写的是模板（`共 {{row_count}} 天`）。真实模型不会这样输出，
    因此离线链路要自己按【结果行数】与首行同名列把话说完——否则无 Key 演示时页面上就是
    `共 {{row_count}} 个月份。` 这种半成品（前端截图验收时抓到的）。
    """
    token, user_id = await _register(client, "ana_script_01")
    conversation = await _conversation(client, token)

    result = await analysis_service.run_once(user_id, conversation, "看看每天的用电趋势")

    assert result.status == "success", result.error_message
    assert result.narrative
    assert "{{" not in result.narrative
    assert "共 0 天" in result.narrative  # 临时库预置表为空 → 行数 0 也如实写进文案


async def test_messages_expose_record_code_for_replay(client) -> None:
    """消息历史要带 `record_code`：前端刷新后据此回放表格 / 图表 / 执行信息。

    只给 `analysis_id`（数字主键）无法反查业务编号，右栏在刷新后就只能是空的。
    """
    token, user_id = await _register(client, "ana_script_02")
    conversation = await _conversation(client, token)
    set_llm(
        ScriptedLLM(rules={"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}})
    )

    result = await analysis_service.run_once(user_id, conversation, "日报有多少行？")
    assert result.status == "success"

    response = await client.get(
        f"/api/v1/conversations/{conversation}/messages", headers=_auth(token)
    )
    items = response.json()["data"]["items"]
    assistant = [item for item in items if item["role"] == "assistant"]
    user = [item for item in items if item["role"] == "user"]
    assert assistant and assistant[-1]["record_code"] == result.record_code
    assert user and user[-1]["record_code"] is None  # 用户消息没有记录编号


# ── SQL 守卫（纯函数，覆盖 ADR-004 的判定点）────────────────────────────────

_ALLOWED = {"report_daily", "report_monthly"}


@pytest.mark.parametrize(
    ("sql", "rule_id"),
    [
        ("INSERT INTO report_daily (date) VALUES ('2020-01-01')", "sqlglot:non_select"),
        ("UPDATE report_daily SET consumption_kwh = 0", "sqlglot:non_select"),
        ("DELETE FROM report_daily", "sqlglot:non_select"),
        ("DROP TABLE report_daily", "sqlglot:non_select"),
        ("PRAGMA table_info(report_daily)", "sqlglot:non_select"),
        ("ATTACH DATABASE 'x.db' AS x", "sqlglot:non_select"),
        ("SELECT 1; DROP TABLE report_daily", "sqlglot:multiple_statements"),
        ("SELECT * FROM users", "sqlglot:table_not_allowed"),
        ("SELECT * FROM other_db.report_daily", "sqlglot:qualified_table"),
        ("SELECT * FROM (SELECT * FROM users)", "sqlglot:table_not_allowed"),
        ("SELECT load_extension('evil')", "sqlglot:forbidden_function"),
        ("SELECT 1 + 1", "sqlglot:no_table"),
        ("", "sqlguard:empty"),
    ],
)
def test_guard_rejects(sql: str, rule_id: str) -> None:
    result = sql_guard.guard_sql(sql, _ALLOWED)
    assert result.allowed is False
    assert result.rule_id == rule_id


def test_guard_allows_select_and_applies_limit() -> None:
    result = sql_guard.guard_sql(
        "SELECT year_month, SUM(consumption_kwh) FROM report_monthly GROUP BY year_month",
        _ALLOWED,
    )
    assert result.allowed is True
    assert result.tables == ["report_monthly"]
    assert result.limit_applied is True
    assert "LIMIT 1000" in result.sql.upper()

    already = sql_guard.guard_sql("SELECT * FROM report_daily LIMIT 5", _ALLOWED)
    assert already.allowed is True
    assert already.limit_applied is False
    assert "1000" not in already.sql


def test_ratio_shape_issue_fires_only_on_summary_grain_detail() -> None:
    """占比题的**结果形态自检**（v1.19.23）：问"占比"却回明细行 → 触发重写；其余情形不触发。

    实测（速度档 q015）：`SELECT peak_ratio FROM report_daily LIMIT 1000` 语法合法、守卫放行、
    执行不报错 —— **只有看结果行数**才能发现形态错了（gold 要的是 1 行的汇总比例）。
    判定取保守口径（问占比 + 粒度是全期/年 + 行数超阈值），避免误伤"逐日比率"这类合法形态。
    """
    payload = {"columns": [{"name": "peak_ratio"}], "rows": [], "row_count": 1000}

    # ① 问占比 + 汇总粒度 + 大量明细行 → 触发
    assert analysis_service._ratio_shape_issue(
        {"intent": "占比", "granularity": "全期"}, "全期峰段用电量占总用电量的比例是多少？", payload
    )
    # ② 槽位 intent 掉了、但问题词面在问占比 → 仍触发（实测 q115 第一轮 intent 掉成了「查数」）
    assert analysis_service._ratio_shape_issue(
        {"intent": "查数", "granularity": "全期"}, "全期各用途用电量的占比是多少？", payload
    )
    # ③ 粒度是「日」→ 逐日给出比率本身合法（如"每天的峰段占比"），不能动
    assert analysis_service._ratio_shape_issue(
        {"intent": "占比", "granularity": "日"}, "每天的峰段占比", payload
    ) is None
    # ④ 行数不大（正常的单行汇总）→ 不触发
    assert analysis_service._ratio_shape_issue(
        {"intent": "占比", "granularity": "全期"}, "全期峰段占比",
        {"columns": [], "rows": [], "row_count": 1},
    ) is None
    # ⑤ 不是占比题 → 不触发
    assert analysis_service._ratio_shape_issue(
        {"intent": "查数", "granularity": "全期"}, "全期总用电量是多少？", payload
    ) is None


def test_extreme_and_dimension_shape_checks() -> None:
    """另两类形态自检（v1.19.24）：问极值却回明细行、筛选型追问丢掉分组维度。"""
    many = {
        "columns": [{"name": "date"}, {"name": "consumption_kwh"}],
        "rows": [], "row_count": 1000,
    }

    # ① 问极值 + 汇总粒度 + 大量明细行 → 触发
    assert analysis_service._extreme_detail_issue(
        {"granularity": "全期"}, "全期单日最高用电量是多少？", many
    )
    # 逐期极值（「各年度的峰值功率」，5 行）→ 不触发
    assert analysis_service._extreme_detail_issue(
        {"granularity": "年"}, "各年度的峰值功率分别是多少？",
        {"columns": [], "rows": [], "row_count": 5},
    ) is None
    # 不是极值题 → 不触发
    assert analysis_service._extreme_detail_issue(
        {"granularity": "全期"}, "全期总用电量是多少？", many
    ) is None

    # ② 筛选型追问 + 槽位有分组维度 + SQL 没有 GROUP BY → 触发（实测 q119）
    assert analysis_service._dropped_dimension_issue(
        {"dimension": "星期"}, "只看工作日。",
        "SELECT SUM(consumption_kwh) AS total FROM report_daily WHERE is_weekend = 0 LIMIT 1000",
        {"columns": [{"name": "total"}], "rows": [], "row_count": 1},
    )
    # SQL 里保留了分组 → 不触发
    assert analysis_service._dropped_dimension_issue(
        {"dimension": "星期"}, "只看工作日。",
        "SELECT weekday, SUM(consumption_kwh) AS x FROM report_daily"
        " WHERE is_weekend = 0 GROUP BY weekday LIMIT 1000",
        {"columns": [], "rows": [], "row_count": 5},
    ) is None
    # 不是"筛选型"追问（换了提问口径）→ 不触发，避免误伤「那全期合计呢？」这类正当收敛
    assert analysis_service._dropped_dimension_issue(
        {"dimension": "星期"}, "那全期合计呢？",
        "SELECT SUM(consumption_kwh) AS total FROM report_daily LIMIT 1000",
        {"columns": [{"name": "total"}], "rows": [], "row_count": 1},
    ) is None

    nested = sql_guard.guard_sql(
        "WITH m AS (SELECT * FROM report_monthly) SELECT * FROM m", _ALLOWED
    )
    assert nested.allowed is True
    assert nested.tables == ["report_monthly"]


# ── 输出解析三层容错（决策 7）───────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw",
    [
        '{"sql": "SELECT 1", "reason": "ok"}',
        '```json\n{"sql": "SELECT 1"}\n```',
        '好的，结果如下：{"sql": "SELECT 1"} 请查收',
        '{"sql": "SELECT 1",}',  # 尾逗号
        "{'sql': 'SELECT 1'}",  # 单引号
    ],
)
def test_extract_json_tolerates_model_output(raw: str) -> None:
    assert extract_json(raw)["sql"] == "SELECT 1"


@pytest.mark.parametrize("raw", ["", "不是 JSON", "SELECT * FROM t", "<html>错误</html>"])
def test_extract_json_raises_e_llm(raw: str) -> None:
    with pytest.raises(BizError) as excinfo:
        extract_json(raw)
    assert excinfo.value.error_code == "E_LLM"


# ── 健康检查 ────────────────────────────────────────────────────────────────

async def test_health_reports_llm_status(client, monkeypatch) -> None:
    """健康检查如实回报模型状态（**显式模拟未配 Key**）。

    本机 `.env` 可能已配真实模型（本项目实测就配了），若依赖环境状态，断言会在这类机器上反掉。
    """
    from app.core import llm_client
    from app.core.config import Settings

    monkeypatch.setattr(
        llm_client, "get_settings",
        lambda: Settings(_env_file=None, jwt_secret_key="x" * 32),
    )
    llm_client.set_llm(None)
    try:
        response = await client.get("/api/v1/system/health")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "healthy"
        assert data["databases"] == {"reports": "ok", "power_agent": "ok"}
        assert data["llm"]["impl"] == "scripted"  # 无 Key → 离线脚本模型（如实回报）
        assert data["llm"]["configured"] is False
        assert llm_status()["model_main"]
    finally:
        llm_client.set_llm(None)
