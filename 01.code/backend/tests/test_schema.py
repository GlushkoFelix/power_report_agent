"""Schema 模块测试（交付物 3）：注入文本生成、预算裁剪、活跃报表取值。

覆盖本轮 5 条决策：
- 决策 1 边界：`/system/schema-text` + 上传表文本生成器
- 决策 2 活跃报表：会话优先 + 缺省回落（`source` 回显）
- 决策 3 预算：字符估算 + 固定顺序裁剪（字段骨架永不裁）
- 决策 4 示例问题：列名启发式 + 兜底
- 决策 5 红线：结构事实 + **口径未知声明**（不得套用预置 R1–R7）
"""

from __future__ import annotations

import json

import pytest

from app.services import schema_service
from tests.conftest import agent_connection

pytestmark = pytest.mark.anyio

_CSV = (
    "日期,时间,总用电量(kWh),峰段用电量,平段用电量,谷段用电量,电压,覆盖率\n"
    "16/12/2006,17:24:00,4.216,1.500,1.700,1.016,234.840,1.0\n"
    "16/12/2006,17:25:00,4.500,1.600,1.800,1.100,233.630,1.0\n"
    "16/12/2006,17:26:00,5.100,1.700,2.000,1.400,270.000,0.5\n"
)

PRESET_MARK = "R6"  # 预置红线条款编号（上传表文本中不得出现）


async def _register(client, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": "Abcd1234"}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload_and_confirm(client, token: str, filename: str = "schema_probe.csv") -> str:
    uploaded = await client.post(
        "/api/v1/reports/upload",
        headers=_auth(token),
        files={"file": (filename, _CSV.encode("utf-8"), "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    code = uploaded.json()["data"]["report_code"]
    confirmed = await client.post(f"/api/v1/reports/{code}/confirm", headers=_auth(token))
    assert confirmed.status_code == 200, confirmed.text
    return code


# ── 预置表：注入文本组装 ────────────────────────────────────────────────────

async def test_preset_schema_returns_full_injection_text(client) -> None:
    """预置表文本 = 全局头 + 本表字段 + 口径红线 + SQLite 函数 + 常用问题示例。"""
    token = await _register(client, "schema_owner_01")
    response = await client.get("/api/v1/reports/r_daily/schema", headers=_auth(token))
    assert response.status_code == 200, response.text

    data = response.json()["data"]
    text = data["text"]
    assert data["text_source"] == "schema_text.md"
    assert data["truncated"] is False
    assert data["est_tokens"] > 0

    # 六段结构俱在
    assert "### report_daily" in text
    assert "## 二、口径红线" in text
    assert PRESET_MARK in text
    assert "## 三、SQLite 可用函数" in text
    assert "## 四、常用问题示例" in text
    # 全局头与其它表段落：头部保留、无关表被切掉
    assert "口径版本" in text
    assert "### report_minute" not in text
    assert "### report_energy_structure" not in text
    # 数据结构化描述仍在（前端字段面板用）
    assert data["structured"]["table"] == "report_daily"


# ── 上传表：生成器 ──────────────────────────────────────────────────────────

async def test_upload_schema_generated_with_unknown_caliber_notice(client) -> None:
    token = await _register(client, "schema_owner_02")
    code = await _upload_and_confirm(client, token)

    response = await client.get(f"/api/v1/reports/{code}/schema", headers=_auth(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    text = data["text"]

    assert data["text_source"] == "generated"
    assert data["est_tokens"] < schema_service.EST_TOKEN_BUDGET
    assert data["truncated"] is False

    # 决策 5：口径未知声明（U1）且**不得套用**预置红线
    assert "U1（口径未知）" in text
    assert "不得" in text and "预置报表的红线 R1–R7" in text
    assert f"**{PRESET_MARK}**" not in text
    # U2 比率列清单（覆盖率 → agg=none 且数值型）
    assert "U2（不可求和的列）" in text and "`coverage`" in text
    # U3 时间列用法
    assert "U3（时间列）" in text and "`dt`" in text
    # U4 本表真实质量数字（1 条电压越界）
    assert "U4（本表特有的质量提示）" in text and "电压越界 1 行" in text
    # U5 单位与量纲
    assert "U5（单位与量纲）" in text and "kWh" in text
    # 后半段：SQLite 函数 + 示例问题（决策 4 启发式：峰平谷命中）
    assert "## 三、SQLite 可用函数" in text
    assert "峰段、平段、谷段各自占比多少？" in text
    assert "全期总用电量是多少？" in text
    assert text.count("\n- ") <= 40  # 文本整体仍然紧凑


async def test_upload_schema_text_persisted_and_reused(client) -> None:
    """生成结果随 `schema_json` 落库（零 DDL），再次访问直接复用同一文本。"""
    token = await _register(client, "schema_owner_03")
    code = await _upload_and_confirm(client, token)

    first = await client.get(f"/api/v1/reports/{code}/schema", headers=_auth(token))
    second = await client.get(f"/api/v1/reports/{code}/schema", headers=_auth(token))
    assert first.json()["data"]["text"] == second.json()["data"]["text"]

    conn = agent_connection()
    try:
        raw = conn.execute(
            "SELECT schema_json FROM uploaded_reports WHERE report_code = ?", (code,)
        ).fetchone()[0]
    finally:
        conn.close()
    schema = json.loads(raw)
    assert schema[schema_service.INJECTION_TEXT_KEY].startswith("# 智电析报")
    assert schema["injection_tokens"] > 0
    assert schema["injection_truncated"] is False
    assert schema["columns"][0]["origin_name"] == "日期"  # 原始列名保留


# ── 预算与裁剪（纯函数单测）────────────────────────────────────────────────

def _wide_schema(columns: int = 200) -> dict:
    return {
        "table": "wide_table",
        "title": "宽表.csv",
        "row_count": 10,
        "time_range": ["2020-01-01 00:00:00", "2020-12-31 23:59:00"],
        "columns": [
            {
                "name": f"col_{index}",
                "origin_name": f"列{index}",
                "type": "REAL",
                "unit": "kWh",
                "agg": "sum",
                "description": "这是一段较长的口径说明，" * 3,
                "null_count": 0,
                "sample": [1.5, 2.5, 3.5],
            }
            for index in range(columns)
        ],
    }


def test_estimate_tokens_is_character_based() -> None:
    assert schema_service.estimate_tokens("") == 0
    assert schema_service.estimate_tokens("中文四字") == 4
    assert schema_service.estimate_tokens("abcdefgh") == 2  # 8 / 4


def test_fit_to_budget_keeps_column_skeleton() -> None:
    """决策 3：超预算时裁剪说明/样例/问题，但字段清单骨架（字段名）必须保留。"""
    schema = _wide_schema()
    baseline = schema_service.build_upload_schema_text(schema, {}, verbosity=0)

    text, truncated = schema_service.fit_to_budget(schema, {"dataset": {"total_rows": 10}})
    assert truncated is True
    assert schema_service.estimate_tokens(text) < schema_service.estimate_tokens(baseline)
    # 全部 200 个字段名仍在（骨架不裁）
    assert all(f"`col_{index}`" in text for index in range(200))
    # 说明被截断（40 字符上限）
    assert "这是一段较长的口径说明" in text
    assert ("这是一段较长的口径说明，" * 3) not in text


def test_fit_to_budget_no_trim_when_within_budget() -> None:
    small = _wide_schema(columns=3)
    text, truncated = schema_service.fit_to_budget(small, {"dataset": {"total_rows": 10}})
    assert truncated is False
    assert schema_service.estimate_tokens(text) < schema_service.EST_TOKEN_BUDGET
    assert all(f"`col_{index}`" in text for index in range(3))


def test_sample_questions_are_feature_filtered() -> None:
    questions = schema_service._sample_questions(["dt", "year", "voltage", "总用电量"])
    assert "电压越界的情况如何？" in questions
    assert "全期总用电量是多少？" in questions
    assert len(questions) <= schema_service._MAX_QUESTIONS

    # 无任何电力特征命中 → 时间基线 + 三条通用兜底（且不超上限）
    generic = schema_service._sample_questions(["x"])
    assert all(question in generic for question in schema_service.FALLBACK_QUESTIONS)
    assert len(generic) <= schema_service._MAX_QUESTIONS
    # 连列名都没有 → 只给兜底
    assert schema_service._sample_questions([]) == list(schema_service.FALLBACK_QUESTIONS)


# ── /system/schema-text ────────────────────────────────────────────────────

async def test_system_schema_text_requires_auth(client) -> None:
    response = await client.get("/api/v1/system/schema-text")
    assert response.status_code == 401
    assert response.json()["data"]["error_code"] == "E_AUTH"


async def test_system_schema_text_falls_back_to_system_default(client) -> None:
    """决策 2 缺省分支：不传会话 → 系统默认活跃报表（r_daily, is_active=1）。"""
    token = await _register(client, "schema_owner_04")
    response = await client.get("/api/v1/system/schema-text", headers=_auth(token))
    assert response.status_code == 200, response.text

    data = response.json()["data"]
    assert data["source"] == schema_service.SYSTEM_DEFAULT_SOURCE
    assert data["report_code"] == "r_daily"
    assert data["is_preset"] == 1
    assert data["caliber_version"] == "caliber-v1.0"
    assert "### report_daily" in data["text"]
    assert PRESET_MARK in data["text"]


async def test_system_schema_text_follows_conversation_active_report(client) -> None:
    """决策 2 会话分支：传会话 → 该会话的活跃报表（会话级唯一权威）。"""
    token = await _register(client, "schema_owner_05")
    code = await _upload_and_confirm(client, token, filename="conv_probe.csv")

    created = await client.post(
        "/api/v1/conversations", headers=_auth(token), json={}
    )
    assert created.status_code == 200, created.text
    conversation = created.json()["data"]["conversation_code"]
    assert created.json()["data"]["active_report_code"] == "r_daily"

    switched = await client.post(
        f"/api/v1/reports/{code}/activate",
        headers=_auth(token),
        json={"conversation_code": conversation},
    )
    assert switched.status_code == 200, switched.text

    response = await client.get(
        f"/api/v1/system/schema-text?conversation_code={conversation}", headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == schema_service.CONVERSATION_SOURCE
    assert data["report_code"] == code
    assert data["caliber_version"] is None
    assert "U1（口径未知）" in data["text"]
    assert "### conv_probe" in data["text"]


async def test_system_schema_text_conversation_guards(client) -> None:
    owner = await _register(client, "schema_owner_06")
    other = await _register(client, "schema_other_06")
    created = await client.post("/api/v1/conversations", headers=_auth(owner), json={})
    conversation = created.json()["data"]["conversation_code"]

    missing = await client.get(
        "/api/v1/system/schema-text?conversation_code=c_19700101_0001", headers=_auth(owner)
    )
    assert missing.status_code == 404

    foreign = await client.get(
        f"/api/v1/system/schema-text?conversation_code={conversation}", headers=_auth(other)
    )
    assert foreign.status_code == 403
    assert foreign.json()["data"]["error_code"] == "E_FORBIDDEN"
