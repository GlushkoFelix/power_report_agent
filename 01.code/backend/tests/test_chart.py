"""图表批次测试（交付物 4 第三批）。

本文件覆盖四条线：
1. **分析链路接线**：`chart` 事件位置、figure 落库 `analyses.chart_json`、
   单值降级、图表异常不阻断主流程；
2. **图表接口**：`POST /charts/rerender`（换类型 / 快照重聚合 / 回写）与
   `GET …/chart.png`（预留能力 501）；
3. **图表规则**（纯函数）：列语义识别、类型选择（hint + 兜底）、语义色/R3、
   TopN 合并、抽稀、缺采断开、粒度重聚合、降级分支；
4. **离线预览工具**：`tools/chart_preview.py` 产出自包含 HTML。

全部用例离线（`ScriptedLLM` + 临时库），不联网、不依赖 02.data 下的正式库。
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.core.llm_client import ScriptedLLM, set_llm
from app.services import analysis_service, chart_service
from tests.conftest import agent_connection

#: `tools/` 不是包，手动加进 sys.path（预览工具用例用）
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

pytestmark = pytest.mark.anyio

#: 3 行中文 CSV：`日期 + 时间` 双列 → 解析出标准时间戳列 `dt` 与派生维度列。
#: 三行刻意落在**同一分钟的连续 3 分钟**上（不制造缺口），让接线用例只关注"事件与落库"；
#: 缺口判定另有用例专门覆盖（见任务 5 的规则用例）。
_CSV = (
    "日期,时间,总用电量(kWh),电压\n"
    "16/12/2006,17:24:00,4.000,234.0\n"
    "16/12/2006,17:25:00,5.000,235.0\n"
    "16/12/2006,17:26:00,6.000,236.0\n"
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


#: 跨两天（同一 `date` 列不可解析为 '%Y-%m-%d'，但三行落在两个自然日上）
#: 用于「粒度重聚合」用例：分钟级 `dt` → 日 → 两行
_CSV_TWO_DAYS = (
    "日期,时间,总用电量(kWh),电压\n"
    "16/12/2006,17:24:00,4.000,234.0\n"
    "16/12/2006,17:25:00,5.000,235.0\n"
    "17/12/2006,17:24:00,6.000,236.0\n"
)


async def _upload_and_activate(
    client, token: str, conversation: str, content: str = _CSV, filename: str = "图表用报表.csv"
) -> str:
    """上传 CSV 并设为会话活跃报表，返回其物理表名。"""
    uploaded = await client.post(
        "/api/v1/reports/upload",
        headers=_auth(token),
        files={"file": (filename, content.encode("utf-8"), "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    data = uploaded.json()["data"]
    confirmed = await client.post(
        f"/api/v1/reports/{data['report_code']}/confirm", headers=_auth(token)
    )
    assert confirmed.status_code == 200, confirmed.text
    switched = await client.post(
        f"/api/v1/reports/{data['report_code']}/activate",
        headers=_auth(token),
        json={"conversation_code": conversation},
    )
    assert switched.status_code == 200, switched.text
    return data["table_name"]


async def _active_report(client, token: str, conversation: str) -> str:
    """上传 3 行 CSV 并设为会话活跃报表，返回其物理表名。"""
    return await _upload_and_activate(client, token, conversation)


async def _line_record(
    client, token: str, user_id: int, conversation: str, table: str
) -> tuple[str, dict]:
    """跑一条"时间序列"问答，返回 `(record_code, chart 事件载荷)`。"""
    set_llm(
        ScriptedLLM(rules={"gen_sql": {"sql": f'SELECT dt, total_kwh FROM "{table}" ORDER BY dt'}})
    )
    result = await analysis_service.run_once(user_id, conversation, "每天的用电量趋势")
    assert result.status == "success", result.error_message
    assert result.chart is not None, "该用例需要一条真的画出了图的记录"
    chart = next(event["data"] for event in result.events if event["event"] == "chart")
    return result.record_code, chart


# ══════════════════════════════════════════════════════════════════════════
#  分析链路接线（任务 2）
# ══════════════════════════════════════════════════════════════════════════


async def test_chart_event_between_table_and_narrative_and_persisted(client) -> None:
    """有真实时间序列时：`chart` 事件带 figure，且 figure 落库到 `analyses.chart_json`。"""
    token, user_id = await _register(client, "chart_owner_01")
    conversation = await _conversation(client, token)
    table = await _active_report(client, token, conversation)
    set_llm(
        ScriptedLLM(
            rules={
                "gen_sql": {
                    "sql": f'SELECT dt, total_kwh FROM "{table}" ORDER BY dt'
                }
            }
        )
    )

    result = await analysis_service.run_once(user_id, conversation, "每天的用电量趋势")

    assert result.status == "success", result.error_message
    names = [event["event"] for event in result.events]
    assert names == [
        "status", "slot", "code", "status", "table", "chart", "narrative", "meta", "done",
    ]

    chart = next(event["data"] for event in result.events if event["event"] == "chart")
    assert chart["chart_type"] == "line"
    assert chart["unit"] == "kWh"
    assert chart["fallback"] is None and chart["reason"] is None
    figure = chart["figure"]
    assert figure["data"][0]["type"] == "scatter"
    assert figure["data"][0]["y"] == [4.0, 5.0, 6.0]
    # 主题边界（决策 ⑪）：figure 里不写底色/字体色，也不带 plotly 内置 template
    layout = figure["layout"]
    assert "template" not in layout
    assert "paper_bgcolor" not in layout and "plot_bgcolor" not in layout
    assert "font" not in layout
    # 口径标记随图落库（layout.meta），详情页重新打开也看得到
    assert layout["meta"]["chart_type"] == "line"
    assert layout["meta"]["unit"] == "kWh"

    conn = agent_connection()
    try:
        row = conn.execute(
            "SELECT chart_json, result_row_count FROM analyses WHERE record_code = ?",
            (result.record_code,),
        ).fetchone()
    finally:
        conn.close()
    assert row[1] == 3
    stored = json.loads(row[0])
    assert stored["data"][0]["y"] == [4.0, 5.0, 6.0]

    # 详情接口把 figure 原样返回（前端 `Plotly.react` 直接渲染）
    detail = await client.get(
        f"/api/v1/analysis/records/{result.record_code}", headers=_auth(token)
    )
    assert detail.json()["data"]["chart"]["layout"]["meta"]["unit"] == "kWh"


async def test_chart_degrades_without_blocking_main_flow(client) -> None:
    """单值结果（COUNT(*)）画不出图：`chart` 事件仍发，主流程与解读不受影响。"""
    token, user_id = await _register(client, "chart_owner_02")
    conversation = await _conversation(client, token)
    set_llm(
        ScriptedLLM(
            rules={"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}}
        )
    )

    result = await analysis_service.run_once(user_id, conversation, "日报一共多少行")

    assert result.status == "success"
    assert result.narrative  # 解读照常生成
    chart = next(event["data"] for event in result.events if event["event"] == "chart")
    assert chart["figure"] is None
    assert chart["fallback"] == "table"
    assert chart["reason"]
    assert result.chart is None

    conn = agent_connection()
    try:
        row = conn.execute(
            "SELECT status, chart_json, narrative FROM analyses WHERE record_code = ?",
            (result.record_code,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "success"
    assert row[1] is None  # 降级 → 不写 chart_json（而不是写空对象）
    assert row[2]


async def test_chart_failure_never_breaks_analysis(client, monkeypatch) -> None:
    """决策 ⑧ 的兜底：`build_chart` 直接抛异常时，分析仍成功、仍发 `chart` 事件。"""
    token, user_id = await _register(client, "chart_owner_03")
    conversation = await _conversation(client, token)
    table = await _active_report(client, token, conversation)
    set_llm(
        ScriptedLLM(rules={"gen_sql": {"sql": f'SELECT dt, total_kwh FROM "{table}" ORDER BY dt'}})
    )

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("图表模块炸了")

    monkeypatch.setattr(chart_service, "build_chart", _boom)

    result = await analysis_service.run_once(user_id, conversation, "每天的用电量趋势")

    assert result.status == "success"
    chart = next(event["data"] for event in result.events if event["event"] == "chart")
    assert chart["figure"] is None
    assert chart["fallback"] == "table"
    assert "图表生成失败" in chart["reason"]
    assert chart_service.CHART_TYPES  # 模块未被破坏


# ══════════════════════════════════════════════════════════════════════════
#  图表接口（任务 3）：`POST /charts/rerender` 与 `GET …/chart.png`
# ══════════════════════════════════════════════════════════════════════════


async def _chart_record(
    client, token: str, user_id: int, *, content: str = _CSV, filename: str = "图表接口报表.csv"
) -> str:
    """造一条"真的画出了图"的分析记录，返回 `record_code`。"""
    conversation = await _conversation(client, token)
    table = await _upload_and_activate(client, token, conversation, content, filename)
    record_code, _ = await _line_record(client, token, user_id, conversation, table)
    return record_code


async def test_rerender_switches_type_and_writes_back(client) -> None:
    """换类型不调 LLM，并把新 figure 回写 `analyses.chart_json`（决策 ⑤）。"""
    token, user_id = await _register(client, "chart_api_01")
    record_code = await _chart_record(client, token, user_id)

    response = await client.post(
        "/api/v1/charts/rerender",
        headers=_auth(token),
        json={"record_code": record_code, "chart_type": "bar"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    data = body["data"]
    assert data["chart_type"] == "bar"
    assert data["persisted"] is True
    assert data["reaggregated"] is False
    assert data["figure"]["data"][0]["type"] == "bar"
    assert data["figure"]["layout"]["meta"]["chart_type"] == "bar"
    assert "柱状" in body["message"]

    # 回写生效：详情接口看到的是**切过的那张图**（不会"刷新就变回去"）
    detail = await client.get(
        f"/api/v1/analysis/records/{record_code}", headers=_auth(token)
    )
    assert detail.json()["data"]["chart"]["data"][0]["type"] == "bar"

    conn = agent_connection()
    try:
        stored = conn.execute(
            "SELECT chart_json FROM analyses WHERE record_code = ?", (record_code,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert json.loads(stored)["data"][0]["type"] == "bar"


async def test_rerender_reaggregates_granularity_from_snapshot(client) -> None:
    """分钟级 `dt` → 日粒度：服务端从**结果快照**真重聚合（决策 ②，不改 SQL）。"""
    token, user_id = await _register(client, "chart_api_02")
    record_code = await _chart_record(
        client, token, user_id, content=_CSV_TWO_DAYS, filename="两日用电.csv"
    )

    response = await client.post(
        "/api/v1/charts/rerender",
        headers=_auth(token),
        json={"record_code": record_code, "granularity": "日", "chart_type": "bar"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["reaggregated"] is True
    assert data["granularity"] == "日"
    assert data["figure"]["data"][0]["x"] == ["2006-12-16", "2006-12-17"]
    assert data["figure"]["data"][0]["y"] == [9.0, 6.0]  # 4.0 + 5.0 求和
    assert data["persisted"] is True


async def test_rerender_rejects_unsupported_type(client) -> None:
    """显式指定的类型是**指令**：画不出来就 400 并给出可用类型（不静默兜底）。"""
    token, user_id = await _register(client, "chart_api_03")
    record_code = await _chart_record(client, token, user_id)

    response = await client.post(
        "/api/v1/charts/rerender",
        headers=_auth(token),
        json={"record_code": record_code, "chart_type": "scatter"},
    )
    assert response.status_code == 400
    payload = response.json()["data"]
    assert payload["error_code"] == "E_CHART_UNSUPPORTED"
    assert "折线" in payload["hint"]  # 把可用类型告诉用户，一步即可纠正


async def test_rerender_rejects_finer_granularity(client) -> None:
    """结果已是日粒度 → 不能再拆成小时：400 `E_GRANULARITY_UNAVAILABLE`。"""
    token, user_id = await _register(client, "chart_api_04")
    conversation = await _conversation(client, token)
    table = await _upload_and_activate(
        client, token, conversation, filename="粒度切换.csv"
    )
    set_llm(
        ScriptedLLM(
            rules={"gen_sql": {"sql": f'SELECT date, total_kwh FROM "{table}" ORDER BY date'}}
        )
    )
    result = await analysis_service.run_once(user_id, conversation, "每天的用电量")
    assert result.status == "success"

    response = await client.post(
        "/api/v1/charts/rerender",
        headers=_auth(token),
        json={"record_code": result.record_code, "granularity": "小时"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["data"]["error_code"] == "E_GRANULARITY_UNAVAILABLE"
    assert "无法" in body["message"]  # 文案说清"从日不能拆回小时"


async def test_rerender_ownership_and_auth(client) -> None:
    """归属隔离：他人记录与不存在的记录一律 404；未登录 401。"""
    token, user_id = await _register(client, "chart_api_05")
    record_code = await _chart_record(client, token, user_id)
    other_token, _ = await _register(client, "chart_api_05_other")

    foreign = await client.post(
        "/api/v1/charts/rerender",
        headers=_auth(other_token),
        json={"record_code": record_code, "chart_type": "bar"},
    )
    assert foreign.status_code == 404

    missing = await client.post(
        "/api/v1/charts/rerender",
        headers=_auth(token),
        json={"record_code": "a_999999", "chart_type": "bar"},
    )
    assert missing.status_code == 404

    anonymous = await client.post(
        "/api/v1/charts/rerender", json={"record_code": record_code, "chart_type": "bar"}
    )
    assert anonymous.status_code == 401


@pytest.mark.parametrize(
    "override",
    [
        {"chart_type": "圆环"},           # 未定义的图型
        {"granularity": "半月"},          # 未定义的粒度
        {"top_n": 99},                   # 超出 3–50
        {"sort": "随机"},                 # 未定义的排序
    ],
)
async def test_rerender_validates_parameters(client, override: dict) -> None:
    token, user_id = await _register(client, f"chart_api_06_{abs(hash(str(override))) % 1000}")
    record_code = await _chart_record(client, token, user_id)

    response = await client.post(
        "/api/v1/charts/rerender",
        headers=_auth(token),
        json={"record_code": record_code, **override},
    )
    assert response.status_code == 400, response.text
    assert response.json()["data"]["error_code"] == "E_VALIDATION"


async def test_chart_png_is_reserved_capability(client) -> None:
    """`chart.png` 的两种合法状态：未装 kaleido → 501 + 可执行提示；装了 → 真出图。"""
    token, user_id = await _register(client, "chart_api_07")
    record_code = await _chart_record(client, token, user_id)

    response = await client.get(
        f"/api/v1/analysis/records/{record_code}/chart.png", headers=_auth(token)
    )
    if chart_service.kaleido_available():
        # 预留能力自动生效（决策 ①：装了 kaleido 无需改代码）
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    else:
        assert response.status_code == 501
        payload = response.json()["data"]
        assert payload["error_code"] == "E_NOT_IMPLEMENTED"
        assert "Plotly.toImage" in payload["hint"]  # 给出下一步而不是含糊报错


async def test_chart_png_ownership_and_missing_figure(client) -> None:
    token, user_id = await _register(client, "chart_api_08")
    record_code = await _chart_record(client, token, user_id)
    other_token, _ = await _register(client, "chart_api_08_other")
    path = f"/api/v1/analysis/records/{record_code}/chart.png"

    assert (await client.get(path, headers=_auth(other_token))).status_code == 404
    assert (await client.get(path)).status_code == 401

    # 单值 KPI 记录压根没有图 → 400（先判"有没有图"，再谈"能不能导出"）
    conversation = await _conversation(client, token)
    set_llm(
        ScriptedLLM(rules={"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}})
    )
    plain = await analysis_service.run_once(user_id, conversation, "日报有多少行")
    empty = await client.get(
        f"/api/v1/analysis/records/{plain.record_code}/chart.png", headers=_auth(token)
    )
    assert empty.status_code == 400
    assert empty.json()["data"]["error_code"] == "E_VALIDATION"


# ══════════════════════════════════════════════════════════════════════════
#  图表规则（纯函数）：列语义 / 类型选择 / R3 / 量纲 / TopN / 抽稀 / 缺口 / 重聚合
# ══════════════════════════════════════════════════════════════════════════

#: 列定义取自正式库 `r_daily` / `r_structure` 的 `schema_json`（保持用例与真实口径一致）
R_DAILY_COLUMNS = [
    {"name": "date", "type": "TEXT", "unit": "—", "agg": "—", "description": "'YYYY-MM-DD'"},
    {"name": "year", "type": "INTEGER", "unit": "—", "agg": "none", "description": ""},
    {"name": "weekday", "type": "INTEGER", "unit": "0=周一", "agg": "none", "description": ""},
    {"name": "consumption_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "sm1_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": "厨房"},
    {"name": "sm2_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": "洗衣房"},
    {"name": "sm3_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": "热水器与空调"},
    {"name": "unmetered_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": "未计量"},
    {"name": "peak_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "flat_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "valley_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "avg_voltage", "type": "REAL", "unit": "V", "agg": "—", "description": ""},
    {"name": "coverage", "type": "REAL", "unit": "比率", "agg": "none", "description": ""},
    {"name": "period", "type": "TEXT", "unit": "—", "agg": "—", "description": "peak 峰 / flat 平 / valley 谷"},
]
R_STRUCTURE_COLUMNS = [
    {"name": "period", "type": "TEXT", "unit": "—", "agg": "—", "description": ""},
    {"name": "is_total_row", "type": "INTEGER", "unit": "—", "agg": "none", "description": ""},
    {"name": "kitchen_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "laundry_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "water_heating_ac_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "other_unmetered_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "peak_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "flat_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "valley_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "total_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
]
R_MINUTE_COLUMNS = [
    {"name": "dt", "type": "TEXT", "unit": "—", "agg": "—", "description": ""},
    {"name": "gap_kw", "type": "REAL", "unit": "kW", "agg": "none", "description": ""},
    {"name": "gap_kwh", "type": "REAL", "unit": "kWh", "agg": "sum", "description": ""},
    {"name": "voltage", "type": "REAL", "unit": "V", "agg": "—", "description": ""},
    {"name": "coverage", "type": "REAL", "unit": "比率", "agg": "none", "description": ""},
]


def _kinds(columns: list[str], rows: list[dict], schema: list[dict]) -> dict[str, str]:
    infos = chart_service._profile_columns(columns, rows, schema)
    return {info.name: info.kind for info in infos}


def test_column_semantics() -> None:
    """列语义识别：时间 / 时间维度 / 指标（带拆分归属）/ 比率 / 维度（靠取值判定）。"""
    rows = [
        {
            "date": "2020-01-01", "year": 2020, "weekday": 2, "consumption_kwh": 10.0,
            "sm1_kwh": 1.0, "peak_kwh": 4.0, "avg_voltage": 235.0, "coverage": 0.9,
            "period": "peak",
        },
        {
            "date": "2020-01-02", "year": 2020, "weekday": 3, "consumption_kwh": 11.0,
            "sm1_kwh": 1.5, "peak_kwh": 4.5, "avg_voltage": 236.0, "coverage": 0.8,
            "period": "valley",
        },
    ]
    kinds = _kinds(
        ["date", "year", "weekday", "consumption_kwh", "sm1_kwh", "peak_kwh",
         "avg_voltage", "coverage", "period"],
        rows, R_DAILY_COLUMNS,
    )
    assert kinds == {
        "date": "time", "year": "time_part", "weekday": "time_part",
        "consumption_kwh": "metric", "sm1_kwh": "metric", "peak_kwh": "metric",
        "avg_voltage": "metric", "coverage": "ratio", "period": "dimension",
    }
    infos = {info.name: info for info in chart_service._profile_columns(
        ["sm1_kwh", "peak_kwh", "period"], rows, R_DAILY_COLUMNS
    )}
    assert infos["sm1_kwh"].partition == "purpose" and infos["sm1_kwh"].series == "kitchen"
    assert infos["peak_kwh"].partition == "period" and infos["peak_kwh"].series == "peak"
    # `period` 列是维度，但其**取值**属于时段拆分（配色与 R3 判定的依据）
    assert infos["period"].partition == "period"
    assert infos["sm1_kwh"].label == "厨房"  # schema description 短且无括号 → 当展示名


def test_type_selection_hint_then_fallback() -> None:
    """决策 ③：`chart_hint` 是建议（不支持就兜底），显式传参是指令（不支持就报错）。"""
    rows = [
        {"date": f"2020-01-{day:02d}", "consumption_kwh": 10.0 + day}
        for day in range(1, 6)
    ]
    columns = ["date", "consumption_kwh"]

    # hint=折线 且数据支持 → 折线
    line = chart_service.build_chart(
        columns, rows, schema_columns=R_DAILY_COLUMNS, slot={"chart_hint": "折线"}
    )
    assert line.chart_type == "line"

    # hint=散点 但只有一个数值列 → 兜底折线（不报错）
    fallback = chart_service.build_chart(
        columns, rows, schema_columns=R_DAILY_COLUMNS, slot={"chart_hint": "散点"}
    )
    assert fallback.chart_type == "line"

    # hint=表格 → 明确以表格呈现（figure=None + fallback）
    table = chart_service.build_chart(
        columns, rows, schema_columns=R_DAILY_COLUMNS, slot={"chart_hint": "表格"}
    )
    assert table.figure is None and table.fallback == "table"

    # intent=占比 + 可画饼 → 兜底到饼图（多指标或分类轴时）
    pie = chart_service.build_chart(
        ["sm1_kwh", "sm2_kwh", "sm3_kwh", "unmetered_kwh"],
        [{"sm1_kwh": 1.0, "sm2_kwh": 2.0, "sm3_kwh": 3.0, "unmetered_kwh": 4.0}],
        schema_columns=R_DAILY_COLUMNS, slot={"intent": "占比"},
    )
    assert pie.chart_type == "pie"

    # 显式指定不支持的类型 + strict → 400
    from app.core.errors import BizError

    with pytest.raises(BizError) as excinfo:
        chart_service.build_chart(
            columns, rows, schema_columns=R_DAILY_COLUMNS,
            chart_type="pie", strict_type=True,
        )
    assert excinfo.value.error_code == "E_CHART_UNSUPPORTED"


def test_r3_convergence_keeps_one_partition() -> None:
    """红线 R3：用途 + 时段两套拆分同现 → 只画一套；未进图的列（含合计）如实回显。"""
    row = {
        "period": "2020", "is_total_row": 0,
        "kitchen_kwh": 1200.5, "laundry_kwh": 800.2,
        "water_heating_ac_kwh": 2600.0, "other_unmetered_kwh": 5400.3,
        "peak_kwh": 3000.1, "flat_kwh": 4000.2, "valley_kwh": 3000.7,
        "total_kwh": 10001.0,
    }
    columns = list(row.keys())

    # 默认（dimension/metric 都没说）→ 保留「按用途」，合计项不进图
    outcome = chart_service.build_chart(
        columns, [row], schema_columns=R_STRUCTURE_COLUMNS,
        slot={"intent": "占比", "chart_hint": "饼图"},
    )
    assert outcome.chart_type == "pie"
    assert outcome.r3_adjusted is True
    assert outcome.dropped_columns == ["peak_kwh", "flat_kwh", "valley_kwh"]
    assert outcome.excluded_columns == ["total_kwh"]
    assert outcome.series == ["未计量", "热水器与空调", "厨房", "洗衣房"]
    colors = outcome.figure["data"][0]["marker"]["colors"]
    # 饼图改用**蓝色系渐阶**（用户要求，2026-09-21）：取色阶前 4 位，与扇区顺序一一对应
    assert colors == ["#1D4ED8", "#60A5FA", "#0B7285", "#93C5FD"]
    assert "按用途" in (outcome.r3_note or "")

    # 用户明确问"按时段" → 保留时段那套
    by_period = chart_service.build_chart(
        columns, [row], schema_columns=R_STRUCTURE_COLUMNS,
        slot={"intent": "占比", "chart_hint": "饼图", "dimension": "时段"},
    )
    assert by_period.dropped_columns == [
        "kitchen_kwh", "laundry_kwh", "water_heating_ac_kwh", "other_unmetered_kwh",
    ]
    # 扇区按值降序：平 4000.2 > 谷 3000.7 > 峰 3000.1
    assert by_period.series == ["平段电量", "谷段电量", "峰段电量"]


def test_unit_filter_and_semantic_colors() -> None:
    """决策 ⑨：跨量纲只画主指标；单系列用平段蓝、多系列不与语义色撞色。"""
    rows = [
        {"date": f"2020-01-{day:02d}", "consumption_kwh": 10.0 + day, "avg_voltage": 235.0}
        for day in range(1, 6)
    ]
    single = chart_service.build_chart(
        ["date", "consumption_kwh", "avg_voltage"], rows,
        schema_columns=R_DAILY_COLUMNS, slot={"metric": "总用电量", "chart_hint": "折线"},
    )
    assert single.chart_type == "line"
    assert single.unit == "kWh"
    assert single.excluded_columns == ["avg_voltage"]
    assert single.figure["data"][0]["line"]["color"] == "#1D4ED8"  # UIUX 7.2 主趋势线

    # `slot.metric` 也可能是英文列名（模型偶尔直接给物理名）——同样要能匹配
    by_physical = chart_service.build_chart(
        ["date", "consumption_kwh", "avg_voltage"], rows,
        schema_columns=R_DAILY_COLUMNS,
        slot={"metric": "consumption_kwh", "chart_hint": "折线"},
    )
    assert by_physical.chart_type == "line"
    assert by_physical.unit == "kWh"
    assert by_physical.series == ["总用电量"]

    multi = chart_service.build_chart(
        ["date", "consumption_kwh", "peak_kwh", "flat_kwh", "valley_kwh"],
        [{**row, "peak_kwh": 3.0, "flat_kwh": 4.0, "valley_kwh": 2.0} for row in rows],
        schema_columns=R_DAILY_COLUMNS, slot={"chart_hint": "折线"},
    )
    colors = [trace["line"]["color"] for trace in multi.figure["data"][:4]]
    semantic = {"#E8590C", "#1D4ED8", "#0CA678", "#7048E8", "#E03131", "#ADB5BD"}
    # 总用电量（无语义归属）不能撞上平段蓝；峰/平/谷各用语义色
    assert colors[0] not in semantic
    assert colors[1:] == ["#E8590C", "#1D4ED8", "#0CA678"]


def test_topn_merge_and_time_axis_not_merged() -> None:
    """决策 ⑥：分类类图 TopN=12 合并「其他」；**时间轴柱状走抽稀，不合并**。"""
    rows = [{"weekday": index, "consumption_kwh": 100.0 - index} for index in range(20)]
    merged = chart_service.build_chart(
        ["weekday", "consumption_kwh"], rows,
        schema_columns=R_DAILY_COLUMNS, slot={"intent": "排行", "chart_hint": "柱状"},
    )
    assert merged.merged_count == 8 and merged.top_n == 12
    labels = merged.figure["data"][0]["x"]
    assert len(labels) == 13 and labels[-1] == "其他"
    # 合并不是截断：各柱之和 == 全量之和
    values = merged.figure["data"][0]["y"]
    assert sum(value for value in values if value is not None) == pytest.approx(
        sum(100.0 - index for index in range(20))
    )

    days = [
        {"date": f"2020-01-{day:02d}", "consumption_kwh": 10.0 + day}
        for day in range(1, 41)
    ]
    time_bar = chart_service.build_chart(
        ["date", "consumption_kwh"], days,
        schema_columns=R_DAILY_COLUMNS, chart_type="bar",
    )
    assert time_bar.merged_count == 0 and time_bar.top_n is None
    assert len(time_bar.figure["data"][0]["x"]) == 40


def test_downsample_keeps_endpoints() -> None:
    """决策 ⑥：1000 点折线抽稀到 500，保留首末点（日期必须真实连续，否则会误判缺口）。"""
    start = date(2020, 1, 1)
    rows = [
        {
            "date": (start + timedelta(days=index)).strftime("%Y-%m-%d"),
            "consumption_kwh": float(index),
        }
        for index in range(1000)
    ]
    outcome = chart_service.build_chart(
        ["date", "consumption_kwh"], rows,
        schema_columns=R_DAILY_COLUMNS, slot={"chart_hint": "折线"},
    )
    assert outcome.downsampled_from == 1000
    assert outcome.gap_count == 0  # 连续日期不该被误判成缺口
    y = outcome.figure["data"][0]["y"]
    assert len(y) == chart_service.DOWNSAMPLE_LIMIT
    assert y[0] == 0.0 and y[-1] == 999.0


def test_gap_breaks_insert_null_and_connector() -> None:
    """决策 ⑦：缺口处插 `null` 断点（不插值）+ 灰色虚线连接段 + customdata 对齐。"""
    rows = [
        {"date": "2020-01-01", "consumption_kwh": 10.0},
        {"date": "2020-01-02", "consumption_kwh": 11.0},
        {"date": "2020-01-03", "consumption_kwh": 12.0},
        # 3 天缺口
        {"date": "2020-01-07", "consumption_kwh": 16.0},
        {"date": "2020-01-08", "consumption_kwh": 17.0},
    ]
    outcome = chart_service.build_chart(
        ["date", "consumption_kwh"], rows,
        schema_columns=R_DAILY_COLUMNS, slot={"chart_hint": "折线"},
    )
    assert outcome.gap_count == 1
    primary = outcome.figure["data"][0]
    assert primary["y"] == [10.0, 11.0, 12.0, None, 16.0, 17.0]
    assert primary["x"][3] == "2020-01-05"  # 缺口中点
    assert primary.get("connectgaps") is False
    # customdata 与 x/y 等长，断点处为 None
    assert len(primary["customdata"]) == len(primary["x"])
    assert primary["customdata"][3] == [None]
    connector = next(
        trace for trace in outcome.figure["data"] if trace.get("name") == "缺采区间"
    )
    assert connector["line"] == {"color": "#ADB5BD", "width": 1, "dash": "dot"}
    assert connector["x"] == ["2020-01-03", "2020-01-07"]


def test_reaggregate_by_unit_caliber() -> None:
    """重聚合口径：kWh 求和、kW/V 取均值、比率列丢弃（R6 不许拿均值顶替）。"""
    rows = [
        {"dt": "2020-01-01 10:30:00", "gap_kwh": 1.0, "gap_kw": 2.0, "voltage": 230.0, "coverage": 0.9},
        {"dt": "2020-01-01 11:30:00", "gap_kwh": 2.0, "gap_kw": 4.0, "voltage": 234.0, "coverage": 0.8},
    ]
    columns, aggregated = chart_service._reaggregate(
        ["dt", "gap_kwh", "gap_kw", "voltage", "coverage"], rows, R_MINUTE_COLUMNS, "月"
    )
    assert [column["name"] for column in columns] == [
        "year_month", "gap_kwh", "gap_kw", "voltage",
    ]  # coverage（比率）被丢弃
    assert aggregated == [{
        "year_month": "2020-01", "gap_kwh": 3.0, "gap_kw": 3.0, "voltage": 232.0,
    }]

    # 全期：单行
    columns, aggregated = chart_service._reaggregate(
        ["dt", "gap_kwh"], rows, R_MINUTE_COLUMNS, "全期"
    )
    assert columns[0]["name"] == "all_period"
    assert aggregated == [{"all_period": "全期", "gap_kwh": 3.0}]


def test_reaggregate_rejects_invalid_requests() -> None:
    """只能由细变粗；无时间列 / 只有比率列时给出可执行的提示。"""
    from app.core.errors import BizError

    rows = [{"date": "2020-01-01", "consumption_kwh": 1.0}]

    with pytest.raises(BizError) as finer:
        chart_service._reaggregate(
            ["date", "consumption_kwh"], rows, R_DAILY_COLUMNS, "小时"
        )
    assert finer.value.error_code == "E_GRANULARITY_UNAVAILABLE"
    assert "无法再拆分" in finer.value.message

    with pytest.raises(BizError) as no_time:
        chart_service._reaggregate(
            ["cnt"], [{"cnt": 1}], [{"name": "cnt", "type": "INTEGER", "unit": "—", "agg": "none"}], "月"
        )
    assert "不含时间列" in no_time.value.message

    with pytest.raises(BizError) as ratio_only:
        chart_service._reaggregate(
            ["dt", "coverage"],
            [{"dt": "2020-01-01 10:00:00", "coverage": 0.9}],
            R_MINUTE_COLUMNS, "月",
        )
    assert "比率" in ratio_only.value.message


def test_degrade_branches() -> None:
    """决策 ⑧：空结果 / 无数值列 / 单值 KPI 都降级为表格并说明原因。"""
    empty = chart_service.build_chart(["date"], [], schema_columns=R_DAILY_COLUMNS)
    assert empty.figure is None and "结果为空" in empty.reason

    text_only = chart_service.build_chart(
        ["anomaly_reason"], [{"anomaly_reason": "电压越界"}],
        schema_columns=[{"name": "anomaly_reason", "type": "TEXT", "unit": "—", "agg": "—"}],
    )
    assert text_only.figure is None and "数值指标列" in text_only.reason

    kpi = chart_service.build_chart(
        ["cnt"], [{"cnt": 1442}],
        schema_columns=[{"name": "cnt", "type": "INTEGER", "unit": "—", "agg": "sum"}],
    )
    assert kpi.figure is None and kpi.fallback == "table"


def test_figure_theme_boundary() -> None:
    """决策 ⑪：figure 不带主题（无 template/底色/字体色），口径标记在 `layout.meta`。"""
    rows = [
        {"date": f"2020-01-{day:02d}", "consumption_kwh": 10.0 + day} for day in range(1, 6)
    ]
    outcome = chart_service.build_chart(
        ["date", "consumption_kwh"], rows,
        schema_columns=R_DAILY_COLUMNS, slot={"chart_hint": "折线"},
    )
    layout = outcome.figure["layout"]
    for forbidden in ("template", "paper_bgcolor", "plot_bgcolor", "font"):
        assert forbidden not in layout
    assert layout["meta"]["chart_type"] == "line"
    assert layout["meta"]["unit"] == "kWh"
    # 峰值标注用语义色（峰段橙红），不是主题字体色
    assert layout["annotations"][0]["font"]["color"] == "#E8590C"


# ══════════════════════════════════════════════════════════════════════════
#  离线预览工具（tools/chart_preview.py）
# ══════════════════════════════════════════════════════════════════════════


async def test_preview_tool_renders_selfcontained_html(client, tmp_path) -> None:
    """直调 service 生成自包含 HTML：有图、无外部脚本、含结果表与记录信息。"""
    import chart_preview  # type: ignore[import-not-found]  # tools/ 不是包，已在文件头加进 sys.path

    token, user_id = await _register(client, "chart_prev_01")
    conversation = await _conversation(client, token)
    table = await _active_report(client, token, conversation)
    record_code, _ = await _line_record(client, token, user_id, conversation, table)

    out = tmp_path / "preview.html"
    args = chart_preview.parse_args(
        ["--record", record_code, "--type", "line,bar", "--out", str(out)]
    )
    exit_code = await chart_preview.run(args)

    assert exit_code == 0
    page = out.read_text(encoding="utf-8")
    assert "Plotly.newPlot" in page                     # 三张图（自动 + 折线 + 柱状）
    assert "<script src=" not in page                   # 自包含：没有任何外部脚本
    assert record_code in page and "结果表" in page
    assert "SELECT dt, total_kwh" in page               # SQL 可复核
    assert "量纲排除" not in page                        # 该记录只有一个指标列


async def test_preview_tool_reports_unsupported_type(client, tmp_path) -> None:
    """画不出来的类型在页面上如实展示 400 文案，而不是静默空块。"""
    import chart_preview  # type: ignore[import-not-found]

    token, user_id = await _register(client, "chart_prev_02")
    conversation = await _conversation(client, token)
    table = await _active_report(client, token, conversation)
    record_code, _ = await _line_record(client, token, user_id, conversation, table)

    out = tmp_path / "preview.html"
    args = chart_preview.parse_args(
        ["--record", record_code, "--type", "scatter", "--out", str(out)]
    )
    assert await chart_preview.run(args) == 0
    page = out.read_text(encoding="utf-8")
    # 区块 ①（自动选型）仍正常出图；散点区块如实展示 400 文案
    assert "Plotly.newPlot" in page
    assert "E_CHART_UNSUPPORTED" in page
    assert "散点" in page


def test_single_row_multi_metric_without_axis_prefers_bar() -> None:
    """无轴的单行多指标（`SELECT SUM(peak), SUM(flat), SUM(valley)`）→ 柱状，不画散点。

    前端联调实测踩到过：这类结果原会被兜底选成散点，画出一个孤零零的点；
    正确表达是"指标列本身当分类"的横向对比（`_axis_frame` 无轴分支正是为此准备）。
    """
    row = {"peak_kwh": 13045.14, "flat_kwh": 16677.42, "valley_kwh": 7561.33}
    outcome = chart_service.build_chart(
        ["peak_kwh", "flat_kwh", "valley_kwh"], [row], schema_columns=R_DAILY_COLUMNS, slot={},
    )

    assert outcome.chart_type == "bar"
    figure = outcome.figure
    assert figure["data"][0]["type"] == "bar"
    # 三根柱 = 三个指标列，用时段语义色（峰/平/谷）
    assert list(figure["data"][0]["x"]) == ["峰段电量", "平段电量", "谷段电量"]
    assert list(figure["data"][0]["y"]) == [13045.14, 16677.42, 7561.33]
    # 逐柱语义色（峰橙红 / 平蓝 / 谷青绿），不是单一颜色
    assert figure["data"][0]["marker"]["color"] == ["#E8590C", "#1D4ED8", "#0CA678"]
