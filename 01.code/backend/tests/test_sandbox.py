"""沙箱/守卫批次测试（交付物 5：F2.3 pandas 沙箱 + F2.4 自愈重试 + R3 SQL 层）。

覆盖四条线：
1. **AST 预检**：G6 恶意样例集（≥20 条全部拦截）+ 合法 pandas 惯用法放行；
2. **R3 SQL 层**：同一加法表达式内混加用途列与时段列 → 拦截；并列多列放行；
3. **子进程沙箱**：真实执行 / 标量与 DataFrame 序列化 / fig 回传 / 超时强杀 / 执行报错归因；
4. **路由与重试链路**：pandas 首选成功 / SQL 执行失败带错误重写 / 重写仍失败换路 pandas /
   安全拦截不换路。

沙箱用例会 spawn 子进程并真实导入 pandas（首次约 2–4s 冷启动），全量跑一次可接受；
超时用例会丢弃进程池，之后的沙箱用例各付一次冷启动。
"""

from __future__ import annotations

import json

import pytest

from app.core.code_guard import guard_code
from app.core import sql_guard
from app.core.errors import BizError
from app.core.llm_client import ScriptedLLM, set_llm
from app.services import analysis_service, sandbox_service
from app.core.config import get_settings
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


async def _upload_and_activate(
    client, token: str, conversation: str, *, filename: str = "沙箱用报表.csv"
) -> str:
    uploaded = await client.post(
        "/api/v1/reports/upload",
        headers=_auth(token),
        files={"file": (filename, _CSV.encode("utf-8"), "text/csv")},
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


# ══════════════════════════════════════════════════════════════════════════
#  ① AST 预检（G6：恶意样例 100% 拦截）
# ══════════════════════════════════════════════════════════════════════════

_MALICIOUS: list[tuple[str, str]] = [
    # import 面
    ("import os", "import_not_allowed"),
    ("import sys; sys.exit()", "import_not_allowed"),
    ("import subprocess", "import_not_allowed"),
    ("import socket", "import_not_allowed"),
    ("import shutil", "import_not_allowed"),
    ("from os import system", "import_not_allowed"),
    ("from subprocess import run", "import_not_allowed"),
    ("from . import helpers", "import_not_allowed"),
    # v1.19.12：`import pandas` 从"拦截"改为"放行"——它是**冗余而非危险**（`pd` 已预置），
    # 原先一律拒绝会导致"模型多写一句 import → 整题 E_CODE_FORBIDDEN 失败"（用户实测踩到）。
    # 这里改用同为第三方但**未预置**的库来守住 import 白名单这条线。
    ("import requests", "import_not_allowed"),
    ("import matplotlib.pyplot as plt", "import_not_allowed"),
    # 危险内建调用
    ("open('/etc/passwd')", "forbidden_call"),
    ("open('x', 'w').write('a')", "forbidden_call"),
    ("eval('1+1')", "forbidden_call"),
    ("exec('import os')", "forbidden_call"),
    ("compile('x', 'x', 'eval')", "forbidden_call"),
    ("__import__('os')", "forbidden_call"),
    ("getattr(df, 'values')", "forbidden_call"),
    ("setattr(df, 'a', 1)", "forbidden_call"),
    ("globals()", "forbidden_call"),
    ("locals()", "forbidden_call"),
    ("vars(df)", "forbidden_call"),
    ("input()", "forbidden_call"),
    ("breakpoint()", "forbidden_call"),
    ("exit()", "forbidden_call"),
    # 反射逃逸链
    ("().__class__", "dunder"),
    ("().__class__.__mro__[1].__subclasses__()", "dunder"),
    ("df.__dict__", "dunder"),
    ("x.__globals__", "dunder"),
    ("().__builtins__", "dunder"),
    # 危险模块名作裸名 / 属性
    ("os.system('dir')", "forbidden_call"),
    ("sys.path", "forbidden_name"),
    ("subprocess.run(['dir'])", "forbidden_name"),
    ("socket.socket()", "forbidden_name"),
    ("shutil.rmtree('x')", "forbidden_name"),
    ("importlib.import_module('os')", "forbidden_name"),
    ("builtins.open('x')", "forbidden_name"),
    ("ctypes.windll.user32", "forbidden_name"),
    # 属性形式的危险方法
    ("x.popen('dir')", "forbidden_call"),
    ("path.unlink()", "forbidden_call"),
    ("shutil.rmtree('x')  # 裸名兜底", "forbidden_name"),
]

_LEGITIMATE: list[str] = [
    'result = df["total_kwh"].sum()',
    'result = df.groupby("year_month")["total_kwh"].sum()',
    'result = df["total_kwh"].rolling(7).mean()',
    'result = df[df["is_anomaly"] == 1][["date", "total_kwh"]]',
    'import math\nresult = math.sqrt(float(df["total_kwh"].sum()))',
    'from statistics import mean\nresult = mean(df["voltage"].tolist())',
    'import datetime\nresult = str(datetime.date(2020, 1, 1))',
    'result = len(df)',
    'result = df["total_kwh"].pct_change()',
    'fig = px.line(df, x="dt", y="total_kwh")\nresult = df["total_kwh"].sum()',
    # 预置库的冗余 import（v1.19.12 放行）：模型很爱写，放行前会让整题以守卫拦截至失败
    'import pandas as pd\nresult = pd.Series([1, 2]).sum()',
    'import numpy as np\nresult = float(np.sqrt(4))',
    'import plotly.graph_objects as go\nresult = df["total_kwh"].sum()',
]


@pytest.mark.parametrize(("code", "rule_part"), _MALICIOUS)
def test_code_guard_blocks_malicious_samples(code: str, rule_part: str) -> None:
    """G6 安全用例集：恶意代码 100% 被 AST 预检拦截（验收指标）."""
    outcome = guard_code(code)
    assert outcome.allowed is False, f"未被拦截：{code}"
    assert rule_part in (outcome.rule_id or "")


@pytest.mark.parametrize("code", _LEGITIMATE)
def test_code_guard_allows_legitimate_pandas(code: str) -> None:
    """合法 pandas 惯用法不得误杀（误杀会让模型重写无意义地循环）。"""
    outcome = guard_code(code)
    assert outcome.allowed is True, f"误杀：{code} → {outcome.reason}"


def test_code_guard_rejects_empty_and_unparsable() -> None:
    assert guard_code("").allowed is False
    assert guard_code("def f(:\n    pass").rule_id == "codeguard:parse_error"


# ══════════════════════════════════════════════════════════════════════════
#  ② R3 SQL 层（红线：禁止用途列与时段列在同一加法表达式内相加）
# ══════════════════════════════════════════════════════════════════════════

_R3_TABLES = {"report_daily", "report_structure"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(peak_kwh + kitchen_kwh) AS x FROM report_structure",
        "SELECT peak_kwh + kitchen_kwh AS x FROM report_structure",
        "SELECT SUM(peak_kwh) + SUM(kitchen_kwh) AS x FROM report_structure",
        "SELECT SUM((peak_kwh + flat_kwh) + (kitchen_kwh + laundry_kwh)) AS x FROM report_structure",
        "SELECT SUM(kitchen_kwh + peak_kwh + laundry_kwh) AS x FROM report_structure",
    ],
)
def test_guard_blocks_r3_mixed_addition(sql: str) -> None:
    result = sql_guard.guard_sql(sql, _R3_TABLES)
    assert result.allowed is False
    assert result.rule_id == "sqlguard:r3_mixed_partition"
    assert "R3" in (result.reason or "")


# ══════════════════════════════════════════════════════════════════════════
#  ③ Top-N 极值自动排除 NULL（v1.19.23：确定性修复，不消耗模型调用）
# ══════════════════════════════════════════════════════════════════════════


def test_guard_adds_null_exclusion_for_topn_extremes() -> None:
    """`ORDER BY 列 LIMIT 小值` = 求极值 / 排名 → 守卫自动补 `列 IS NOT NULL`。

    依据（实测 q118，速度档）：SQLite 下 `ORDER BY x ASC` 会把 NULL 排最前，"最低的那天"就选中了
    缺失日；`SQL_SYSTEM` 第 15 条**已明文要求**排除 NULL，但速度档不照做
    ⇒ **能在代码里保证的事，就不该指望模型**。
    """
    result = sql_guard.guard_sql(
        "SELECT date, consumption_kwh FROM report_daily"
        " WHERE year = 2007 ORDER BY consumption_kwh ASC LIMIT 1",
        {"report_daily"},
    )
    assert result.allowed is True
    assert result.null_guarded == ["consumption_kwh"]
    assert "IS NULL" in result.sql.upper()  # 渲染为等价的 `NOT x IS NULL`

    already = sql_guard.guard_sql(
        "SELECT date FROM report_daily WHERE consumption_kwh IS NOT NULL"
        " ORDER BY consumption_kwh ASC LIMIT 1",
        {"report_daily"},
    )
    assert already.null_guarded == []  # 已显式排除过 → 不重复补


def test_guard_keeps_listing_sorts_and_aliases_untouched() -> None:
    """三种**不能改**的情形：罗列式排序、无显式 LIMIT、按"输出别名"排序。

    第三种是写这段时**实测踩到的坑**：`ORDER BY total DESC LIMIT 1` 里的 `total` 来自
    `SUM(...) AS total`，若往 WHERE 里加 `NOT total IS NULL`，SQLite 会直接报 no such column。
    """
    listing = sql_guard.guard_sql(
        "SELECT date, consumption_kwh FROM report_daily ORDER BY consumption_kwh ASC LIMIT 1000",
        {"report_daily"},
    )
    assert listing.null_guarded == []  # 罗列式排序：语义是"按序查看"，不补

    no_limit = sql_guard.guard_sql(
        "SELECT date, consumption_kwh FROM report_daily ORDER BY consumption_kwh ASC",
        {"report_daily"},
    )
    assert no_limit.null_guarded == []  # 无显式 LIMIT → 不是 Top-N（自动补 LIMIT 在下一步）

    alias = sql_guard.guard_sql(
        "SELECT month, SUM(consumption_kwh) AS total FROM report_daily"
        " GROUP BY month ORDER BY total DESC LIMIT 1",
        {"report_daily"},
    )
    assert alias.null_guarded == []  # 别名不是表列，不能进 WHERE


@pytest.mark.parametrize(
    "sql",
    [
        # 并列选择多列不违规（结构报表本就两套列共存）
        "SELECT peak_kwh, kitchen_kwh FROM report_structure",
        "SELECT SUM(peak_kwh) AS p, SUM(kitchen_kwh) AS k FROM report_structure",
        # 单套拆分内部求和合法（峰+平+谷=总电量）
        "SELECT SUM(peak_kwh + flat_kwh + valley_kwh) AS t FROM report_daily",
        "SELECT SUM(kitchen_kwh + laundry_kwh) AS t FROM report_structure",
    ],
)
def test_guard_allows_r3_safe_addition(sql: str) -> None:
    assert sql_guard.guard_sql(sql, _R3_TABLES).allowed is True


# ══════════════════════════════════════════════════════════════════════════
#  ③ 子进程沙箱
# ══════════════════════════════════════════════════════════════════════════

async def test_sandbox_executes_dataframe_series_and_figure(client) -> None:
    """真实执行：groupby 结果 / 标量 / fig 序列化（不带 template）。"""
    token, user_id = await _register(client, "sandbox_exec_01")
    conversation = await _conversation(client, token)
    table = await _upload_and_activate(client, token, conversation)
    db = get_settings().reports_db

    outcome = await sandbox_service.run_pandas_code(
        'result = df.groupby("dt")["total_kwh"].sum().round(2)',
        table, db_path=db,
    )
    assert outcome.ok
    assert outcome.row_count == 3
    assert outcome.result_kind == "series"
    assert outcome.rows[0]["total_kwh"] == 4.0

    outcome = await sandbox_service.run_pandas_code(
        'result = df["total_kwh"].sum()', table, db_path=db
    )
    assert outcome.ok and outcome.result_kind == "scalar"
    assert outcome.rows[0]["value"] == pytest.approx(15.0)

    outcome = await sandbox_service.run_pandas_code(
        'fig = go.Figure(go.Bar(x=df["dt"], y=df["total_kwh"]))\n'
        'result = df["total_kwh"].sum()',
        table, db_path=db,
    )
    assert outcome.ok
    assert outcome.figure is not None
    assert outcome.figure["data"][0]["type"] == "bar"
    assert "template" not in (outcome.figure.get("layout") or {})


async def test_sandbox_timeout_kills_worker(client) -> None:
    """CPU 死循环：超时强杀 worker（池丢弃重建），主进程不受影响。"""
    token, user_id = await _register(client, "sandbox_timeout_01")
    conversation = await _conversation(client, token)
    table = await _upload_and_activate(client, token, conversation)

    with pytest.raises(BizError) as excinfo:
        await sandbox_service.run_pandas_code(
            "while True:\n    pass", table, db_path=get_settings().reports_db, timeout=3.0
        )
    assert excinfo.value.error_code == "E_SANDBOX_TIMEOUT"

    # 池被丢弃后，下一次执行应自动重建并正常工作
    outcome = await sandbox_service.run_pandas_code(
        'result = len(df)', table, db_path=get_settings().reports_db, timeout=30.0
    )
    assert outcome.ok and outcome.rows[0]["value"] == 3


async def test_sandbox_exec_error_attributes(client) -> None:
    """执行报错：E_CODE_EXEC + error_type + 行号，供 F2.4 带错误重写。"""
    token, user_id = await _register(client, "sandbox_exec_02")
    conversation = await _conversation(client, token)
    table = await _upload_and_activate(client, token, conversation)

    with pytest.raises(BizError) as excinfo:
        await sandbox_service.run_pandas_code(
            'result = 1\nraise ValueError("boom")', table, db_path=get_settings().reports_db
        )
    assert excinfo.value.error_code == "E_CODE_EXEC"
    assert excinfo.value.data["error_type"] == "ValueError"
    assert excinfo.value.data["line_no"] == 2
    assert excinfo.value.data["trace_tail"]

    with pytest.raises(BizError) as excinfo:
        await sandbox_service.run_pandas_code(
            "x = 1", table, db_path=get_settings().reports_db
        )
    assert excinfo.value.data["error_type"] == "NoResult"


async def test_sandbox_rejects_code_before_spawning(client) -> None:
    """预检不过的代码根本不进沙箱（400 E_CODE_FORBIDDEN，不付出子进程成本）。"""
    token, user_id = await _register(client, "sandbox_guard_01")
    conversation = await _conversation(client, token)
    table = await _upload_and_activate(client, token, conversation)

    with pytest.raises(BizError) as excinfo:
        await sandbox_service.run_pandas_code(
            "import os\nresult = os.getcwd()", table, db_path=get_settings().reports_db
        )
    assert excinfo.value.error_code == "E_CODE_FORBIDDEN"
    assert excinfo.value.http_status == 400


# ══════════════════════════════════════════════════════════════════════════
#  ④ 路由与重试链路
# ══════════════════════════════════════════════════════════════════════════

async def test_run_once_routes_pandas_first(client) -> None:
    """模型自报 route=pandas：代码过预检 → 沙箱执行 → route/code_text 落库。"""
    token, user_id = await _register(client, "route_pandas_01")
    conversation = await _conversation(client, token)
    table = await _upload_and_activate(client, token, conversation)
    set_llm(
        ScriptedLLM(
            rules={
                "gen_sql": {
                    "route": "pandas",
                    "code": 'result = df["total_kwh"].sum()',
                    "reason": "需汇总",
                }
            }
        )
    )

    result = await analysis_service.run_once(user_id, conversation, "总用电量是多少")

    assert result.status == "success", result.error_message
    assert result.route == "pandas"
    assert result.rows[0]["value"] == pytest.approx(15.0)
    names = [event["event"] for event in result.events]
    assert names == [
        "status", "slot", "code", "status", "table", "chart", "narrative", "meta", "done",
    ]
    code_event = next(event["data"] for event in result.events if event["event"] == "code")
    assert code_event["route"] == "pandas"
    assert "total_kwh" in code_event["code"]

    conn = agent_connection()
    try:
        row = conn.execute(
            "SELECT route, code_text, sql_text, path_switched, retries FROM analyses "
            "WHERE record_code = ?",
            (result.record_code,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "pandas"
    assert "total_kwh" in row[1]
    assert row[2] is None and row[3] == 0 and row[4] == 0


async def test_run_once_rewrites_sql_with_error_once(client, monkeypatch) -> None:
    """F2.4：SQL 执行报错（列不存在）→ 带错误重写一次成功；is_retry=1 留痕。"""
    # 本用例断言**完整调用序列**（含 slot）→ 关掉规则快通道（v1.19.26）：
    # 「有多少行」属于行数题，会被快通道命中而省掉 slot 调用（同上一个用例的做法）。
    monkeypatch.setattr(analysis_service.slot_service, "FASTPATH_ENABLED", False)
    token, user_id = await _register(client, "route_retry_01")
    conversation = await _conversation(client, token)
    table = await _upload_and_activate(client, token, conversation)
    set_llm(
        ScriptedLLM(
            rules={
                "gen_sql": [
                    {"sql": f'SELECT no_such_col FROM "{table}"'},
                    {"sql": f'SELECT COUNT(*) AS cnt FROM "{table}"'},
                ]
            }
        )
    )

    result = await analysis_service.run_once(user_id, conversation, "有多少行")

    assert result.status == "success", result.error_message
    assert result.retries == 1
    assert result.path_switched is False
    assert result.rows[0]["cnt"] == 3

    conn = agent_connection()
    try:
        calls = conn.execute(
            "SELECT l.stage, l.is_retry FROM llm_calls l "
            "JOIN analyses a ON a.id = l.analysis_id WHERE a.record_code = ? ORDER BY l.id",
            (result.record_code,),
        ).fetchall()
    finally:
        conn.close()
    assert [(row[0], row[1]) for row in calls] == [
        ("slot", 0), ("gen_sql", 0), ("gen_sql", 1), ("narrative", 0),
    ]


async def test_run_once_switches_to_pandas_after_failed_rewrite(client, monkeypatch) -> None:
    """SQL 重写仍执行失败 → 换路 pandas 成功：path_switched=1、code_text 落库。"""
    # 本用例断言**完整调用序列**（含 slot）→ 关掉规则快通道，避免序列里少一条（v1.19.11）
    monkeypatch.setattr(analysis_service.slot_service, "FASTPATH_ENABLED", False)
    token, user_id = await _register(client, "route_switch_01")
    conversation = await _conversation(client, token)
    table = await _upload_and_activate(client, token, conversation)
    set_llm(
        ScriptedLLM(
            rules={
                "gen_sql": [
                    {"sql": f'SELECT no_such_col FROM "{table}"'},
                    {"sql": f'SELECT also_missing FROM "{table}"'},
                ],
                "gen_code": {"code": 'result = df["total_kwh"].sum()'},
            }
        )
    )

    result = await analysis_service.run_once(user_id, conversation, "总用电量")

    assert result.status == "success", result.error_message
    assert result.route == "pandas"
    assert result.retries == 1 and result.path_switched is True
    assert result.rows[0]["value"] == pytest.approx(15.0)

    conn = agent_connection()
    try:
        row = conn.execute(
            "SELECT route, code_text, path_switched, retries FROM analyses WHERE record_code = ?",
            (result.record_code,),
        ).fetchone()
        calls = conn.execute(
            "SELECT l.stage, l.is_retry FROM llm_calls l "
            "JOIN analyses a ON a.id = l.analysis_id WHERE a.record_code = ? ORDER BY l.id",
            (result.record_code,),
        ).fetchall()
    finally:
        conn.close()
    assert row[0] == "pandas" and "total_kwh" in row[1] and row[2] == 1 and row[3] == 1
    assert [(item[0], item[1]) for item in calls] == [
        ("slot", 0), ("gen_sql", 0), ("gen_sql", 1), ("gen_code", 1), ("narrative", 0),
    ]


async def test_run_once_blocked_sql_never_switches_path(client) -> None:
    """安全拦截不换路：DROP 被拦 → 重写仍被拦 → 直接 failed（不给无关的 pandas 成功）。"""
    token, user_id = await _register(client, "route_blocked_01")
    conversation = await _conversation(client, token)
    bad_sql = "DROP TABLE report_daily"
    set_llm(ScriptedLLM(rules={"gen_sql": {"sql": bad_sql}}))

    result = await analysis_service.run_once(user_id, conversation, "帮我删掉这张表")

    assert result.status == "failed"
    assert result.error_code == "E_SQL_FORBIDDEN"
    assert result.path_switched is False and result.retries == 1
    assert [event["event"] for event in result.events][-2:] == ["error", "done"]
    assert result.events[-2]["data"]["detail"].endswith("rule=sqlglot:non_select")

    conn = agent_connection()
    try:
        calls = conn.execute(
            "SELECT l.stage, l.is_retry FROM llm_calls l "
            "JOIN analyses a ON a.id = l.analysis_id WHERE a.record_code = ? ORDER BY l.id",
            (result.record_code,),
        ).fetchall()
        audits = conn.execute(
            "SELECT rule_id FROM audit_logs WHERE event_type = 'sql_blocked' "
            "ORDER BY id DESC LIMIT 2"
        ).fetchall()
    finally:
        conn.close()
    assert [(item[0], item[1]) for item in calls] == [("slot", 0), ("gen_sql", 0), ("gen_sql", 1)]
    assert all(row[0] == "sqlglot:non_select" for row in audits) and len(audits) == 2


async def test_run_once_blocks_malicious_pandas_code(client) -> None:
    """pandas 路由的恶意代码同样拦截落库（G6 的链路级验证）；重写仍恶意 → 如实失败。"""
    token, user_id = await _register(client, "route_blocked_02")
    conversation = await _conversation(client, token)
    await _upload_and_activate(client, token, conversation)
    set_llm(
        ScriptedLLM(
            rules={
                "gen_sql": {
                    "route": "pandas",
                    "code": "import os\nresult = os.getcwd()",
                },
                "gen_code": {"code": "open('/etc/passwd')"},
            }
        )
    )

    result = await analysis_service.run_once(user_id, conversation, "看看服务器目录")

    assert result.status == "failed"
    assert result.error_code == "E_CODE_FORBIDDEN"
    assert result.last_rule == "codeguard:forbidden_call"
    assert result.narrative == ""  # P4：不编造
    assert [event["event"] for event in result.events][-2:] == ["error", "done"]

    # 审计留痕（v1.19.28）：每次拦截一条 `code_blocked`；逃逸类规则**追加** `injection_attempt`
    # （首轮 `import os` + 重写 `open(...)` 各一次 → 4 条，顺序为 code_blocked → injection_attempt）
    conn = agent_connection()
    try:
        rows = conn.execute(
            "SELECT event_type, severity, action, rule_id FROM audit_logs "
            "WHERE event_type IN ('code_blocked', 'injection_attempt') "
            "ORDER BY id DESC LIMIT 4"
        ).fetchall()[::-1]
    finally:
        conn.close()
    assert [row[0] for row in rows] == [
        "code_blocked", "injection_attempt", "code_blocked", "injection_attempt",
    ]
    assert [row[3] for row in rows] == [
        "codeguard:import_not_allowed", "codeguard:import_not_allowed",
        "codeguard:forbidden_call", "codeguard:forbidden_call",
    ]
    assert all(row[1] == "high" and row[2] == "blocked" for row in rows)


async def test_code_block_audit_same_in_langgraph(client) -> None:
    """两条编排口径一致：LangGraph 路径的代码拦截**同样**落 `code_blocked` + `injection_attempt`。"""
    from app.agent.graph import run_once_graph

    token, user_id = await _register(client, "route_blocked_03")
    conversation = await _conversation(client, token)
    await _upload_and_activate(client, token, conversation)
    set_llm(
        ScriptedLLM(
            rules={
                "gen_sql": {"route": "pandas", "code": "import os\nresult = os.getcwd()"},
                "gen_code": {"code": "open('/etc/passwd')"},
            }
        )
    )

    result = await run_once_graph(user_id, conversation, "看看服务器目录")

    assert result.status == "failed"
    assert result.error_code == "E_CODE_FORBIDDEN"
    assert result.last_rule == "codeguard:forbidden_call"
    conn = agent_connection()
    try:
        rows = conn.execute(
            "SELECT event_type, rule_id FROM audit_logs "
            "WHERE event_type IN ('code_blocked', 'injection_attempt') "
            "ORDER BY id DESC LIMIT 4"
        ).fetchall()[::-1]
    finally:
        conn.close()
    assert [row[0] for row in rows] == [
        "code_blocked", "injection_attempt", "code_blocked", "injection_attempt",
    ]
    assert [row[1] for row in rows] == [
        "codeguard:import_not_allowed", "codeguard:import_not_allowed",
        "codeguard:forbidden_call", "codeguard:forbidden_call",
    ]


async def test_code_block_without_escape_feature_is_not_escalated(client) -> None:
    """非逃逸类拦截（语法错）只记 `code_blocked`——**不得**误报成 `injection_attempt`。

    口径：`injection_attempt ⊆ code_blocked`，前者只覆盖"试图引入沙箱外能力 / 反射逃逸"
    的规则；"模型把代码写跑了"（空代码 / 超长 / 语法错）不进高危子集。
    """
    token, user_id = await _register(client, "route_blocked_04")
    conversation = await _conversation(client, token)
    await _upload_and_activate(client, token, conversation)
    set_llm(
        ScriptedLLM(
            rules={
                "gen_sql": {"route": "pandas", "code": "result = ("},
                "gen_code": {"code": "result = ("},
            }
        )
    )

    # 同一测试库里已有前序用例写的审计行 → 本用例按**增量**断言（不依赖全表历史）
    conn = agent_connection()
    try:
        _before = tuple(
            conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE event_type = ?", (event,)
            ).fetchone()[0]
            for event in ("code_blocked", "injection_attempt")
        )
    finally:
        conn.close()

    result = await analysis_service.run_once(user_id, conversation, "看看服务器目录")

    assert result.status == "failed"
    assert result.error_code == "E_CODE_FORBIDDEN"
    assert result.last_rule == "codeguard:parse_error"
    conn = agent_connection()
    try:
        blocked = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE event_type = 'code_blocked'"
        ).fetchone()[0]
        escalated = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE event_type = 'injection_attempt'"
        ).fetchone()[0]
        last_rule = conn.execute(
            "SELECT rule_id FROM audit_logs WHERE event_type = 'code_blocked' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert blocked - _before[0] == 2  # 首轮 + 重写各一条（与 sql_blocked 的逐次留痕口径一致）
    assert escalated - _before[1] == 0  # 非逃逸类：不升级为 injection_attempt
    assert last_rule == "codeguard:parse_error"


def test_meta_event_reports_retries() -> None:
    """meta.retries 来自真实重试计数（成功重写后为 1）——链路级由上面用例覆盖，
    此处只确认 AnalysisResult 默认值口径。"""
    result = analysis_service.AnalysisResult(
        record_code="", conversation_code="", report_code="", table_name="",
        question="q", status="success",
    )
    assert result.retries == 0 and result.path_switched is False and result.code is None
    assert json.dumps({"retries": result.retries})
