"""LangGraph 编排 ⇄ 既有显式 async 编排的**等价性回归**（大修阶段 1 的验收）。

验收标准（见 `大修工作进度/01-*`）：
- 同一场景走两条编排路径，**关键字段逐项一致**（status / route / 表格 / retries / path_switched / 事件序列）；
- 三条典型路径都覆盖：成功链、澄清分支、失败留痕；
- 全程零额度：用 `ScriptedLLM`（离线脚本模型），不联网、不花 token、结果可复现。
"""

from __future__ import annotations

import pytest

from app.agent.graph import build_graph, graph_mermaid, run_once_graph
from app.core.llm_client import ScriptedLLM, set_llm
from app.services import analysis_service

#: 与既有测试同范式：异步用例走 anyio（`conftest.anyio_backend` 指定 asyncio）
pytestmark = pytest.mark.anyio

# ── 夹具与工具（与 test_analysis.py 同范式）──────────────────────────────


@pytest.fixture(autouse=True)
def _scripted_llm():
    """每个用例都从离线脚本模型起步。"""
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


_COMPARE_FIELDS = (
    "status",
    "route",
    "table_name",
    "row_count",
    "truncated",
    "retries",
    "path_switched",
)


def _diff_hint(legacy, graph) -> str:
    """不一致时把"证据"一并打出来（事件序列 / SQL / 代码），省去再跑一遍猜。"""

    def codes(result) -> list[dict]:
        out = []
        for event in result.events:
            if event["event"] != "code":
                continue
            data = event["data"]
            out.append(
                {"route": data.get("route"), "text": (data.get("sql") or data.get("code") or "")[:70]}
            )
        return out

    return (
        f"\n  graph .events = {[e['event'] for e in graph.events]}"
        f"\n  legacy.events = {[e['event'] for e in legacy.events]}"
        f"\n  graph .codes  = {codes(graph)}"
        f"\n  legacy.codes  = {codes(legacy)}"
        f"\n  graph .sql    = {graph.sql!r}"
        f"\n  legacy.sql    = {legacy.sql!r}"
        f"\n  graph .code   = {graph.code!r}"
        f"\n  legacy.code   = {legacy.code!r}"
        f"\n  graph .slot   = {graph.slot}"
        f"\n  legacy.slot   = {legacy.slot}"
    )


def _assert_same(legacy, graph) -> None:
    for field in _COMPARE_FIELDS:
        assert getattr(graph, field) == getattr(legacy, field), (
            f"字段不一致 {field}: graph={getattr(graph, field)!r} legacy={getattr(legacy, field)!r}"
            + _diff_hint(legacy, graph)
        )
    assert graph.columns == legacy.columns
    assert graph.rows == legacy.rows
    assert graph.slot == legacy.slot
    assert [item["event"] for item in graph.events] == [
        item["event"] for item in legacy.events
    ], "SSE 事件序列必须一致（含 code / table / chart / narrative / meta / done）"


# ── 图结构本身 ───────────────────────────────────────────────────────────


def test_graph_compiles_and_matches_architecture_doc() -> None:
    """图能编译，且节点/条件边与《系统架构设计文档》4.2 的控制流一致（阶段 2 拓扑）。

    阶段 2 起，**重写环与形态自检环是真正的条件边**：
    生成 → 守卫 → 执行 → 形态自检 → 发表格，任一步失败都回到 `rewrite`，
    `rewrite` 再决定"带错误重写 / 换路 / 额度用尽（→ persist）"。
    """
    build_graph()
    mermaid = graph_mermaid()
    for node in (
        "inject_schema", "extract_slot", "clarify_card", "clarify_wait", "generate",
        "guard", "execute", "shape_check", "publish_table", "rewrite", "chart", "narrate", "persist",
    ):
        assert node in mermaid, f"图缺少节点 {node}"
    # 主干分支
    assert "extract_slot -.-> clarify_card" in mermaid   # 缺关键条件 → 建澄清卡（HITL 第一步）
    assert "clarify_card --> clarify_wait" in mermaid    # → 中断等待（HITL 第二步）
    assert "clarify_wait --> extract_slot" in mermaid    # 回填恢复后回到 slot 节点合并补丁
    assert "generate -.-> guard" in mermaid              # sql → 守卫
    assert "generate -.-> execute" in mermaid            # pandas → 直接执行
    # 两个环
    assert "guard -.-> rewrite" in mermaid           # 拦截 → 重写环
    assert "execute -.-> rewrite" in mermaid         # 执行失败 → 重写环
    assert "shape_check -.-> rewrite" in mermaid     # 形态不对 → 重写环
    assert "rewrite -.-> persist" in mermaid         # 额度用尽 → 留痕失败
    # 成功路径
    assert "publish_table --> chart" in mermaid


# ── 成功链等价 ───────────────────────────────────────────────────────────


async def test_parity_success_chain(client, monkeypatch) -> None:
    """成功链：同样的脚本模型规则下，两条编排结果逐项一致。"""
    token, user_id = await _register(client, "lg_parity_ok")
    legacy_conv = await _conversation(client, token)
    graph_conv = await _conversation(client, token)

    monkeypatch.setattr(
        analysis_service.slot_service, "FASTPATH_ENABLED", False, raising=False
    )
    rules = {"gen_sql": {"sql": "SELECT COUNT(*) AS cnt FROM report_daily"}}

    set_llm(ScriptedLLM(rules=rules))
    legacy = await analysis_service.run_once(
        user_id=user_id, conversation_code=legacy_conv, question="日报一共有多少行？"
    )

    set_llm(ScriptedLLM(rules=rules))
    graph = await run_once_graph(
        user_id=user_id, conversation_code=graph_conv, question="日报一共有多少行？"
    )

    assert legacy.status == "success", legacy.error_message
    _assert_same(legacy, graph)
    assert graph.record_code.startswith("a_")


# ── 澄清分支等价 ─────────────────────────────────────────────────────────


async def test_parity_clarify_branch(client, monkeypatch) -> None:
    """澄清分支：缺关键条件时两条编排都出澄清卡，且 slot 展示一致。"""
    token, user_id = await _register(client, "lg_parity_clarify")
    legacy_conv = await _conversation(client, token)
    graph_conv = await _conversation(client, token)

    monkeypatch.setattr(
        analysis_service.slot_service, "FASTPATH_ENABLED", False, raising=False
    )
    # 与 test_clarify_slot.py 的「模型澄清信号」场景同构（**确定性**）：
    # 模型显式报 `missing_slot=granularity` → 澄清卡按"粒度"提问。
    # 不用"缺指标"那条路径：它会被**词面兜底**（guess_metric_from_question）补上指标而不再澄清。
    slot_signal = {
        "time_range": "全期", "granularity": None, "metric": "总用电量",
        "dimension": None, "filters": None, "compare": None,
        "intent": "澄清", "chart_hint": None, "missing_slot": "granularity",
    }
    set_llm(ScriptedLLM(rules={"slot": slot_signal}))
    legacy = await analysis_service.run_once(
        user_id=user_id, conversation_code=legacy_conv, question="看看趋势"
    )

    set_llm(ScriptedLLM(rules={"slot": slot_signal}))
    graph = await run_once_graph(
        user_id=user_id, conversation_code=graph_conv, question="看看趋势"
    )

    # 既有语义：澄清轮的 `status` 仍是 success（`_finish_clarify` 不改它），靠 `clarify` 字段区分；
    # 且**不落 analyses**（`record_id=null`）——两条编排必须一致
    assert legacy.clarify is not None
    assert graph.clarify is not None
    assert graph.status == legacy.status == "success"
    assert (graph.clarify or {}).get("missing_slot") == (
        legacy.clarify or {}
    ).get("missing_slot") == "granularity"
    assert graph.record_code in ("", None)
    assert [opt["value"] for opt in graph.clarify["options"]] == [
        opt["value"] for opt in legacy.clarify["options"]
    ]
    assert graph.slot == legacy.slot
    assert [item["event"] for item in graph.events] == [
        item["event"] for item in legacy.events
    ]


# ── 失败留痕等价 ─────────────────────────────────────────────────────────


async def test_parity_blocked_sql_leaves_failed_record(client, monkeypatch) -> None:
    """守卫拦截：两条编排都落 `status='failed'` 的记录，错误码与守卫规则一致。"""
    from tests.conftest import agent_connection

    token, user_id = await _register(client, "lg_parity_blocked")
    legacy_conv = await _conversation(client, token)
    graph_conv = await _conversation(client, token)

    monkeypatch.setattr(
        analysis_service.slot_service, "FASTPATH_ENABLED", False, raising=False
    )
    rules = {"gen_sql": {"sql": "SELECT * FROM users"}}

    set_llm(ScriptedLLM(rules=rules))
    legacy = await analysis_service.run_once(
        user_id=user_id, conversation_code=legacy_conv, question="帮我删掉这张表"
    )

    set_llm(ScriptedLLM(rules=rules))
    graph = await run_once_graph(
        user_id=user_id, conversation_code=graph_conv, question="帮我删掉这张表"
    )

    assert legacy.status == "failed"
    assert graph.status == "failed"
    assert graph.error_code == legacy.error_code == "E_SQL_FORBIDDEN"
    assert graph.error_message == legacy.error_message
    _assert_same(legacy, graph)

    # 两条编排都必须**留下失败记录**（决策 4：失败全留痕）。
    # 走接口断言：`GET /analysis/records?status=failed` —— 与前端"记录列表"同一取数路径，
    # 比直连临时库更贴近调用方，也不依赖 `analyses` 的物理列名。
    response = await client.get(
        "/api/v1/analysis/records", headers=_auth(token), params={"status": "failed"}
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    failed_convs = {item["conversation_code"] for item in items}
    assert {legacy_conv, graph_conv} <= failed_convs, (legacy_conv, graph_conv, failed_convs)
    codes = {item["record_code"] for item in items if item["conversation_code"] in (legacy_conv, graph_conv)}
    assert codes == {legacy.record_code, graph.record_code}


# ── 阶段 2：重写环 / 换路 / pandas 路径的等价性 ────────────────────────────

_CSV = "date,total_kwh,peak_kwh\n2020-01-01,5.0,2.0\n2020-01-02,10.0,4.0\n"


async def _upload_and_activate(client, token: str, conversation: str, csv: str = _CSV) -> str:
    """上传一份小 CSV 并设为会话活跃报表（pandas / 形态自检用例需要真实列与真实行数）。"""
    uploaded = await client.post(
        "/api/v1/reports/upload",
        headers=_auth(token),
        files={"file": ("parity.csv", csv.encode("utf-8"), "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    report_code = uploaded.json()["data"]["report_code"]
    confirmed = await client.post(
        f"/api/v1/reports/{report_code}/confirm", headers=_auth(token), json={}
    )
    assert confirmed.status_code == 200, confirmed.text
    table_name = confirmed.json()["data"]["table_name"]
    activated = await client.post(
        f"/api/v1/reports/{report_code}/activate",
        headers=_auth(token),
        json={"conversation_code": conversation},
    )
    assert activated.status_code == 200, activated.text
    return table_name


async def test_parity_sql_rewrite_once(client, monkeypatch) -> None:
    """**带错误重写一次**就成功：两条编排的 retries / 行数 / 事件序列必须一致。"""
    monkeypatch.setattr(analysis_service.slot_service, "FASTPATH_ENABLED", False, raising=False)
    token, user_id = await _register(client, "lg_parity_rewrite")
    table = await _upload_and_activate(client, token, await _conversation(client, token))
    legacy_conv = await _conversation(client, token)
    graph_conv = await _conversation(client, token)
    rules = {
        "gen_sql": [
            {"sql": f'SELECT no_such_col FROM "{table}"'},          # 第 1 次：执行必失败
            {"sql": f'SELECT COUNT(*) AS cnt FROM "{table}"'},      # 重写：成功
        ]
    }

    set_llm(ScriptedLLM(rules=rules))
    legacy = await analysis_service.run_once(
        user_id=user_id, conversation_code=legacy_conv, question="有多少行"
    )
    set_llm(ScriptedLLM(rules=rules))
    graph = await run_once_graph(
        user_id=user_id, conversation_code=graph_conv, question="有多少行"
    )

    assert legacy.status == "success", legacy.error_message
    assert legacy.retries == 1 and not legacy.path_switched
    _assert_same(legacy, graph)
    assert graph.retries == 1 and not graph.path_switched
    assert graph.rows[0]["cnt"] == 2


async def test_parity_switch_to_pandas_after_failed_rewrite(client, monkeypatch) -> None:
    """**重写仍失败 → 换路 pandas 成功**：path_switched=1、route=pandas，两条编排一致。"""
    monkeypatch.setattr(analysis_service.slot_service, "FASTPATH_ENABLED", False, raising=False)
    token, user_id = await _register(client, "lg_parity_switch")
    table = await _upload_and_activate(client, token, await _conversation(client, token))
    legacy_conv = await _conversation(client, token)
    graph_conv = await _conversation(client, token)
    rules = {
        "gen_sql": [
            {"sql": f'SELECT no_such_col FROM "{table}"'},
            {"sql": f'SELECT also_missing FROM "{table}"'},   # 重写仍失败 → 触发换路
        ],
        "gen_code": {"code": 'result = df["total_kwh"].sum()'},
    }

    set_llm(ScriptedLLM(rules=rules))
    legacy = await analysis_service.run_once(
        user_id=user_id, conversation_code=legacy_conv, question="总用电量"
    )
    set_llm(ScriptedLLM(rules=rules))
    graph = await run_once_graph(
        user_id=user_id, conversation_code=graph_conv, question="总用电量"
    )

    assert legacy.status == "success", legacy.error_message
    assert legacy.route == "pandas" and legacy.path_switched and legacy.retries == 1
    _assert_same(legacy, graph)
    assert graph.route == "pandas" and graph.path_switched and graph.retries == 1
    assert graph.rows[0]["value"] == pytest.approx(15.0)


async def test_parity_pandas_first(client, monkeypatch) -> None:
    """模型**首选 pandas**：沙箱执行 + `code` 事件；两条编排一致（无守卫、无形态度自检）。"""
    monkeypatch.setattr(analysis_service.slot_service, "FASTPATH_ENABLED", False, raising=False)
    token, user_id = await _register(client, "lg_pd_first")  # 用户名上限 20 字符
    table = await _upload_and_activate(client, token, await _conversation(client, token))
    legacy_conv = await _conversation(client, token)
    graph_conv = await _conversation(client, token)
    rules = {
        "gen_sql": {
            "route": "pandas",
            "code": f'frame = df.groupby("date")["total_kwh"].sum().reset_index()\nresult = frame',
        }
    }

    set_llm(ScriptedLLM(rules=rules))
    legacy = await analysis_service.run_once(
        user_id=user_id, conversation_code=legacy_conv, question="按天汇总总用电量"
    )
    set_llm(ScriptedLLM(rules=rules))
    graph = await run_once_graph(
        user_id=user_id, conversation_code=graph_conv, question="按天汇总总用电量"
    )

    assert legacy.status == "success", legacy.error_message
    assert legacy.route == "pandas" and legacy.retries == 0
    _assert_same(legacy, graph)
    assert graph.route == "pandas" and graph.retries == 0
    assert [event["event"] for event in graph.events][:3] == ["status", "slot", "code"]


async def test_parity_shape_issue_triggers_rewrite(client, monkeypatch) -> None:
    """**结果形态自检**命中 → 带原因重写一次：问"占比"却回明细行，两条编排都必须纠正。

    注意：必须用"有真实数据的表"（预置报表在测试库里只有元数据、物理表是空的，
    `row_count` 恒为 0 → 行数条件永不满足，自检观察不到）。
    """
    monkeypatch.setattr(analysis_service.slot_service, "FASTPATH_ENABLED", False, raising=False)
    token, user_id = await _register(client, "lg_shape")
    #: 150 行明细（> SHAPE_DETAIL_ROW_LIMIT=100，才能触发"占比却回明细"的自检）
    big_csv = "date,total_kwh,peak_kwh\n" + "".join(
        f"2020-01-{i % 28 + 1:02d},{1.0 + i},{0.5 + i}\n" for i in range(150)
    )
    table = await _upload_and_activate(
        client, token, await _conversation(client, token), csv=big_csv
    )
    legacy_conv = await _conversation(client, token)
    graph_conv = await _conversation(client, token)
    rules = {
        # `_ratio_shape_issue` 的三个条件：问占比（intent=占比 / 词面）+ **粒度是全期/年** + 明细行数超限
        "slot": {
            "time_range": "全期", "granularity": "全期", "metric": "峰段用电量",
            "dimension": None, "filters": None, "compare": None,
            "intent": "占比", "chart_hint": None,
        },
        "gen_sql": [
            # 第 1 次：明细行（形态错——问占比却给了逐行数据）
            {"sql": f'SELECT date, peak_kwh FROM "{table}" ORDER BY date'},
            # 重写：给出比率汇总（形态对；红线 R6：分子求和 ÷ 分母求和）
            {"sql": f'SELECT SUM(peak_kwh) / SUM(total_kwh) AS ratio FROM "{table}"'},
        ],
    }

    set_llm(ScriptedLLM(rules=rules))
    legacy = await analysis_service.run_once(
        user_id=user_id, conversation_code=legacy_conv, question="峰段用电量占比是多少"
    )
    set_llm(ScriptedLLM(rules=rules))
    graph = await run_once_graph(
        user_id=user_id, conversation_code=graph_conv, question="峰段用电量占比是多少"
    )

    assert legacy.status == "success", legacy.error_message
    _assert_same(legacy, graph)
    # 自检命中 ⇒ 两条编排都必须重写过一次，且最终给的是"一行比例"
    assert legacy.retries == graph.retries == 1
    assert graph.row_count == 1


# ── 上下文级失败必须上抛（与 run_once 同构）────────────────────────────────


async def test_parity_missing_conversation_raises(client) -> None:
    """会话不存在 → 两条编排都抛 BizError（无处留痕，交端点按 HTTP 语义处理）。"""
    from app.core.errors import BizError

    token, user_id = await _register(client, "lg_parity_404")

    with pytest.raises(BizError) as legacy_exc:
        await analysis_service.run_once(user_id, "c_19700101_0001", "会话不存在")
    with pytest.raises(BizError) as graph_exc:
        await run_once_graph(user_id, "c_19700101_0001", "会话不存在")

    assert graph_exc.value.http_status == legacy_exc.value.http_status == 404
    assert graph_exc.value.error_code == legacy_exc.value.error_code
