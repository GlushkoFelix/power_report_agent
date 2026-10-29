"""评测模块测试（交付物 8 / M5，PRD F5.1–F5.3）。

覆盖四条线：
1. **判分引擎**（纯函数）：三级兑底（列位置 → 列名归一 → 单列数值集合）、容差（1e-6 / 比率 ±0.01 个百分点）、
   `diff_type` 归因、结论关键数值命中判定；
2. **题库资产**：`02.data/eval/eval_questions.json` 的题量/题型/陷阱题占比（G4 验收口径）；
3. **批次接口**：发起（自检/真实/参数校验）、轮询详情、报告导出、归属隔离与鉴权；
4. **端到端自检**：把题库灌进临时库 → 跑两题（含多轮题）→ 断言 100% 正确、报告含自检警示。

自检批次在 `_run_worker` 里会**逐题注入回放模型**（全局 LLM 客户端），因此测试内的批次
串行轮询等待完成，不做并发。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app.core.config import DATA_DIR, get_settings
from app.services import eval_service
from tests.conftest import agent_connection

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _eval_isolation():
    """评测批次是**后台任务**：用例结束前收尾并释放连接池，避免把"上一个事件循环里的连接"
    留给后面的用例（否则会出现 `MissingGreenlet` 这类跨循环污染）。

    刻意用**同步**夹具：本模块既有同步的判分单测也有 async 的接口用例，anyio 不支持给
    同步用例挂 async autouse 夹具；这里用 `asyncio.run` 做尽力清理，清理失败不影响用例结论。
    """
    yield
    for task in list(eval_service._TASKS):
        if not task.done():
            task.cancel()
    from app.db.session import dispose_engines

    try:
        asyncio.run(dispose_engines())
    except Exception:  # noqa: BLE001 —— 清理失败不该让用例变红（连接池下次调用时重建）
        pass

#: 题库快照（仓库产物，由 `scripts/04_build_eval_set.py` 生成）
QUESTIONS_JSON = DATA_DIR / "eval" / "eval_questions.json"


# ══════════════════════════════════════════════════════════════════════════
#  ① 判分引擎（纯函数）
# ══════════════════════════════════════════════════════════════════════════

def _gold(columns: list[str], rows: list[dict]) -> dict:
    return {"columns": columns, "rows": rows}


def test_score_tier1_position_and_row_order() -> None:
    """第一档：列位置对齐、**行序无关**、列名不同也算过（位置即语义）。"""
    gold = _gold(["month", "kwh"], [{"month": 1, "kwh": 10.0}, {"month": 2, "kwh": 20.0}])
    same = eval_service.score_result_sets(
        gold, _gold(["month", "kwh"], [{"month": 2, "kwh": 20.0}, {"month": 1, "kwh": 10.0}])
    )
    assert same["is_correct"] and same["tier"] == 1

    renamed = eval_service.score_result_sets(
        gold, _gold(["m", "v"], [{"m": 1, "v": 10.0}, {"m": 2, "v": 20.0}])
    )
    assert renamed["is_correct"] and renamed["tier"] == 1


def test_score_tier2_column_name_normalized() -> None:
    """第二档：列**顺序**不同但列名可归一（`SUM(x) AS total` ≈ `total_kwh`）。"""
    gold = _gold(["total_kwh", "avg_kw"], [{"total_kwh": 100.0, "avg_kw": 1.5}])
    gen = _gold(["avg_kw", "total"], [{"avg_kw": 1.5, "total": 100.0}])
    result = eval_service.score_result_sets(gold, gen)
    assert result["is_correct"] and result["tier"] == 2


def test_score_tier_single_column_and_format_leniency() -> None:
    """单列结果：位置即身份（第一档）；日期/数值写法差异由第三档兜住；多列不放宽。"""
    # 单列、列名不同、值一致 → 第一档（位置对齐）
    single = eval_service.score_result_sets(
        _gold(["total_kwh"], [{"total_kwh": 37283.7477}]),
        _gold(["whatever_name"], [{"whatever_name": 37283.7477}]),
    )
    assert single["is_correct"] and single["tier"] == 1

    # 单列日期：gold `2006-12-17` vs 生成 `2006-12-17 00:00:00` → 第三档（格式宽松）
    dated = eval_service.score_result_sets(
        _gold(["date"], [{"date": "2006-12-17"}]),
        _gold(["dt"], [{"dt": "2006-12-17 00:00:00"}]),
    )
    assert dated["is_correct"] and dated["tier"] == 3

    # 多列：忽略列身份会把"列搞反"当正确，因此不启用宽松档
    multi = eval_service.score_result_sets(
        _gold(["peak_kwh", "valley_kwh"], [{"peak_kwh": 100.0, "valley_kwh": 200.0}]),
        _gold(["a", "b"], [{"a": 200.0, "b": 100.0}]),
    )
    assert multi["is_correct"] is False


def test_score_tolerance_numeric_and_percent() -> None:
    """容差（v1.19.6 调整）：`max(绝对下限, |值|×1e-3, 展示级舍入 5e-3)`。

    背景：真实模型评测发现模型习惯 `ROUND(x, 2)` —— gold `26.0179676902` vs 模型 `26.02`（差 0.002）、
    `79.5564333333` vs `79.56`，原来都被 1e-6 绝对容差判成 `value_mismatch`，但那只是"展示级舍入"。
    放宽后仍能拦住真实错误：偏差超过 0.005（半个末位）且超过 0.1% 相对误差的，一律判错。
    """
    tiny = 5e-7
    ok = eval_service.score_result_sets(
        _gold(["total_kwh"], [{"total_kwh": 100.0}]),
        _gold(["total_kwh"], [{"total_kwh": 100.0 + tiny}]),
    )
    assert ok["is_correct"]

    # 展示级舍入（模型 ROUND 到两位小数，|Δ| ≤ 0.005）→ 判对
    rounded = eval_service.score_result_sets(
        _gold(["total_kwh"], [{"total_kwh": 100.0}]),
        _gold(["total_kwh"], [{"total_kwh": 100.004}]),
    )
    assert rounded["is_correct"]

    # 真实偏差（0.5%）→ 仍判错
    too_big = eval_service.score_result_sets(
        _gold(["total_kwh"], [{"total_kwh": 100.0}]),
        _gold(["total_kwh"], [{"total_kwh": 100.5}]),
    )
    assert too_big["is_correct"] is False and too_big["diff_type"] == "value_mismatch"

    # 比率列同样容忍展示级舍入：0.4 → 0.403 判对；0.4 → 0.41（偏差 0.01）判错
    ratio_ok = eval_service.score_result_sets(
        _gold(["peak_ratio"], [{"peak_ratio": 0.4}]),
        _gold(["peak_ratio"], [{"peak_ratio": 0.403}]),
    )
    assert ratio_ok["is_correct"]
    ratio_bad = eval_service.score_result_sets(
        _gold(["peak_ratio"], [{"peak_ratio": 0.4}]),
        _gold(["peak_ratio"], [{"peak_ratio": 0.41}]),
    )
    assert ratio_bad["is_correct"] is False


@pytest.mark.parametrize(
    ("gold", "gen", "diff_type"),
    [
        (_gold(["a"], [{"a": 1.0}]), _gold(["a", "b"], [{"a": 1.0, "b": 2.0}]), "column_count_mismatch"),
        (_gold(["a"], [{"a": 1.0}, {"a": 2.0}]), _gold(["a"], [{"a": 1.0}]), "row_count_mismatch"),
        (_gold(["a"], [{"a": 1.0}]), _gold(["a"], [{"a": "怎么不算" + "?"}]), "type_mismatch"),
        (_gold(["a"], [{"a": 1.0}]), _gold([], []), "empty_result"),
    ],
)
def test_score_diff_types(gold: dict, gen: dict, diff_type: str) -> None:
    """差异归因：列数 / 行数 / 类型 / 空结果（用于评测报告的失败分布）。"""
    result = eval_service.score_result_sets(gold, gen)
    assert result["is_correct"] is False
    assert result["diff_type"] == diff_type


def test_score_key_numbers_hit_and_miss() -> None:
    """结论命中判定：数值（含千分位/单位）与标签项分别判定，输出命中率与漏项。"""
    keys = [
        {"label": "总用电量", "value": 37283.7477, "unit": "kWh", "kind": "number"},
        {"label": "最高月份", "value": "1", "unit": "月", "kind": "label"},
    ]
    narrative = "全期总用电量为 37,283.75 kWh；用电量最高的月份为 1 月。"
    scored = eval_service.score_key_numbers(keys, narrative)
    assert scored["total"] == 2 and scored["hit"] == 2 and scored["hit_rate"] == 1.0

    missed = eval_service.score_key_numbers(keys, "全期用电量较高，主要集中在冬季。")
    assert missed["hit"] == 0 and len(missed["missed"]) == 2


# ══════════════════════════════════════════════════════════════════════════
#  ② 题库资产（G4：120 题 / 6 类题型 / 约 15% 陷阱题）
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not QUESTIONS_JSON.exists(), reason="题库未构建（先跑 scripts/04_build_eval_set.py）")
def test_question_bank_distribution() -> None:
    payload = json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))
    questions = payload["questions"]
    assert len(questions) == 120
    by_type: dict[str, int] = {}
    traps = 0
    for item in questions:
        by_type[item["qtype"]] = by_type.get(item["qtype"], 0) + 1
        traps += item["is_trap"]
        # 每题都必须有 gold 答案与关键数值（判分的两个输入）
        gold = json.loads(item["gold_answer_json"])
        assert gold["rows"], f"{item['question_code']} gold 为空"
        assert json.loads(item["key_numbers_json"]), f"{item['question_code']} 缺关键数值"
        assert item["report_table"]
    assert set(by_type) == {"stat", "agg", "compare", "anomaly", "structure", "multi_turn"}
    assert 0.12 <= traps / len(questions) <= 0.20  # 约 15%（PRD F5.1）
    # 陷阱题必须登记到具体红线
    for item in questions:
        if item["is_trap"]:
            assert item["trap_rule"] in ("R1", "R3", "R6", "R7")


def _insert_questions(rows: list[dict]) -> None:
    """把题目写进**临时库**（测试库只建表不灌题）；按 question_code 覆盖式写入。

    兼容两种输入：题库快照的 JSON 字符串字段（`gold_answer_json`/`key_numbers_json`）
    与测试自造的 Python 对象（`gold_answer`/`key_numbers`）。
    """
    conn: sqlite3.Connection = agent_connection()
    try:
        for item in rows:
            gold = item.get("gold_answer")
            if gold is None:
                gold_json = item.get("gold_answer_json")
            else:
                gold_json = json.dumps(gold, ensure_ascii=False)
            keys = item.get("key_numbers")
            keys_json = (
                json.dumps(keys, ensure_ascii=False) if isinstance(keys, list)
                else item.get("key_numbers_json")
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO eval_questions
                    (question_code, question, qtype, difficulty, report_table, gold_sql,
                     gold_answer_json, key_numbers_json, is_trap, trap_rule, tags, sort_no)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["question_code"], item["question"], item["qtype"], item["difficulty"],
                    item["report_table"], item["gold_sql"], gold_json, keys_json,
                    item["is_trap"], item["trap_rule"], item["tags"], item["sort_no"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_questions_into_temp_db(codes: list[str]) -> None:
    """按题号从题库快照挑题灌进临时库（列表类用例用）。"""
    payload = json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))
    _insert_questions([item for item in payload["questions"] if item["question_code"] in codes])


async def _register(client, username: str) -> tuple[str, int]:
    response = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": "Abcd1234"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    return payload["token"], payload["user"]["id"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _wait_run(client, token: str, run_code: str, timeout: float = 60.0) -> dict:
    """轮询批次直到结束（评测批次是异步的，接口层没有进度事件）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        response = await client.get(f"/api/v1/eval/runs/{run_code}", headers=_auth(token))
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        if data["status"] in ("finished", "failed"):
            return data
        await asyncio.sleep(0.3)
    raise AssertionError(f"批次 {run_code} 在 {timeout}s 内未结束")


_EVAL_CSV = (
    "日期,时间,总用电量(kWh),电压\n"
    "16/12/2006,17:24:00,4.000,234.0\n"
    "17/12/2006,17:25:00,5.000,235.0\n"
    "18/12/2006,17:26:00,6.000,236.0\n"
)


async def _upload_eval_table(client, token: str, *, filename: str = "评测用报表.csv") -> str:
    """上传并确认一份 3 行 CSV（总电量 15.0 kWh、3 行），返回物理表名。

    评测按 `report_table` 激活活跃报表，因此**不需要**预先设置会话活跃报表
    （上传表会进 `_report_code_map`，新会话由 run_once 自动切过去）。
    """
    uploaded = await client.post(
        "/api/v1/reports/upload",
        headers=_auth(token),
        files={"file": (filename, _EVAL_CSV.encode("utf-8"), "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    data = uploaded.json()["data"]
    confirmed = await client.post(
        f"/api/v1/reports/{data['report_code']}/confirm", headers=_auth(token)
    )
    assert confirmed.status_code == 200, confirmed.text
    return data["table_name"]


def _synthetic_questions(table: str, prefix: str = "s") -> list[dict]:
    """构造两道**自洽**题（gold 由测试数据算出），用于验证自检的"正常应 100%"判据。"""
    return [
        {
            "question_code": f"{prefix}001",
            "question": "总用电量是多少？",
            "qtype": "stat",
            "difficulty": "easy",
            "report_table": table,
            "gold_sql": f'SELECT SUM(total_kwh) AS total_kwh FROM "{table}"',
            "gold_answer": {"columns": ["total_kwh"], "rows": [{"total_kwh": 15.0}], "row_count": 1},
            "key_numbers": [{"label": "总用电量", "value": 15.0, "unit": "kWh", "kind": "number"}],
            "is_trap": 0,
            "trap_rule": None,
            "tags": "自检",
            "sort_no": 1,
        },
        {
            "question_code": f"{prefix}002",
            "question": "先看看总用电量。\n一共有多少天数据？",
            "qtype": "multi_turn",
            "difficulty": "medium",
            "report_table": table,
            "gold_sql": f'SELECT COUNT(*) AS days FROM "{table}"',
            "gold_answer": {"columns": ["days"], "rows": [{"days": 3}], "row_count": 1},
            "key_numbers": [{"label": "数据天数", "value": 3, "unit": "天", "kind": "number"}],
            "is_trap": 0,
            "trap_rule": None,
            "tags": "自检/多轮",
            "sort_no": 2,
        },
    ]


# ══════════════════════════════════════════════════════════════════════════
#  ③ 批次接口
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not QUESTIONS_JSON.exists(), reason="题库未构建")
async def test_questions_endpoint_filters(client) -> None:
    """题库列表 + 筛选（题型/是否陷阱）；题库为全局资产，需登录但不做用户隔离。"""
    payload = json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))
    traps = [item for item in payload["questions"] if item["qtype"] == "structure" and item["is_trap"]]
    assert traps, "题库里应有结构类陷阱题（R7）"
    _insert_questions(traps)

    token, _ = await _register(client, "eval_q_01")
    response = await client.get(
        "/api/v1/eval/questions?qtype=structure&is_trap=true&page_size=5", headers=_auth(token)
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] >= 1
    assert all(item["qtype"] == "structure" and item["is_trap"] for item in data["items"])
    assert all(item["trap_rule"] == "R7" for item in data["items"])

    anonymous = await client.get("/api/v1/eval/questions")
    assert anonymous.status_code == 401


async def test_run_rejects_real_mode_without_key(client, monkeypatch) -> None:
    """真实模式无 Key → 400（**不静默回落**到脚本模型）。

    **显式构造"无 Key"配置**：本机 `.env` 已配真实模型（本项目实测），
    不能让环境状态决定断言结果——否则这条用例在这台机器上会拿 200 并**真的跑一次真实批次**。
    """
    from app.core.config import Settings
    from app.services import eval_service

    monkeypatch.setattr(
        eval_service, "get_settings",
        lambda: Settings(_env_file=None, jwt_secret_key="x" * 32),
    )
    token, _ = await _register(client, "eval_real_01")
    response = await client.post(
        "/api/v1/eval/runs", headers=_auth(token), json={"impl": "real", "limit": 1}
    )
    assert response.status_code == 400
    body = response.json()
    assert body["data"]["error_code"] == "E_VALIDATION"
    assert "LLM_API_KEY" in body["message"]
    assert "selfcheck" in body["data"]["hint"]  # 给出可执行的下一步


async def test_run_rejects_unknown_impl_and_empty_bank(client) -> None:
    """参数校验：未定义 impl → 400；题库为空 → 400 且 hint 给出构建命令。"""
    token, _ = await _register(client, "eval_param_01")
    bad_impl = await client.post(
        "/api/v1/eval/runs", headers=_auth(token), json={"impl": "magic"}
    )
    assert bad_impl.status_code == 400
    assert bad_impl.json()["data"]["error_code"] == "E_VALIDATION"

    conn = agent_connection()
    try:
        conn.execute("DELETE FROM eval_questions")  # 确保题库为空，验证兜底提示
        conn.commit()
    finally:
        conn.close()
    empty = await client.post("/api/v1/eval/runs", headers=_auth(token), json={"impl": "selfcheck"})
    assert empty.status_code == 400
    body = empty.json()
    assert body["data"]["error_code"] == "E_VALIDATION"
    assert "题库为空" in body["message"]
    assert "04_build_eval_set.py" in body["data"]["hint"]


async def test_run_requires_auth(client) -> None:
    response = await client.post("/api/v1/eval/runs", json={"impl": "selfcheck"})
    assert response.status_code == 401


# ══════════════════════════════════════════════════════════════════════════
#  ④ 端到端自检（两题：普通题 + 多轮题）
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not QUESTIONS_JSON.exists(), reason="题库未构建")
async def test_selfcheck_run_end_to_end(client, monkeypatch, tmp_path) -> None:
    """自检批次端到端：链路全通 → 100% 正确、报告含自检警示且逐题明细完整。

    用**上传表 + 自洽合成题**（gold 由 CSV 真实值算出）：临时库的预置表是空的，
    若直接沿用真实题库的 gold，生成结果必然对不上，无法验证"正常应为 100%"这一判据。
    """
    monkeypatch.setattr(eval_service, "REPORT_DIR", tmp_path)
    token, _ = await _register(client, "eval_run_01")
    table = await _upload_eval_table(client, token)
    _insert_questions(_synthetic_questions(table))

    created = await client.post(
        "/api/v1/eval/runs",
        headers=_auth(token),
        json={"impl": "selfcheck", "name": "冒烟批次", "question_codes": ["s001", "s002"]},
    )
    assert created.status_code == 200, created.text
    run_code = created.json()["data"]["run_code"]
    assert run_code.startswith("run_")

    data = await _wait_run(client, token, run_code)
    assert data["status"] == "finished", data.get("error_message")
    assert data["question_total"] == 2
    # gold 回放：预期 100%（任何偏差都说明链路坏了）
    assert data["exec_correct_count"] == 2 and data["exec_correct_rate"] == 1.0
    assert data["narrative_hit_rate"] == 1.0
    assert data["model_name"] == "scripted"
    assert data["retry_total"] == 0

    details = {item["question_code"]: item for item in data["questions"]}
    assert details["s001"]["matched_tier"] == 1 and details["s001"]["is_correct"]
    # 多轮题：跑了两轮，判分基于最后一轮
    assert details["s002"]["rounds"] == 2 and details["s002"]["is_correct"]
    assert details["s001"]["route"] == "sql"
    assert data["metrics"]["by_type"]["stat"]["total"] == 1

    # 报告：自检警示置顶 + 指标与逐题明细齐全
    report = await client.get(f"/api/v1/eval/runs/{run_code}/report", headers=_auth(token))
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/markdown")
    text = report.text
    assert "自检模式" in text and "不代表模型能力" in text
    assert "执行正确率" in text and "s001" in text and "错题复盘" in text

    # 列表接口能看到该批次
    listed = await client.get("/api/v1/eval/runs", headers=_auth(token))
    assert listed.status_code == 200
    assert run_code in [item["run_code"] for item in listed.json()["data"]["items"]]

    # 批次关联的模型调用留痕（llm_calls.eval_run_id 回填）
    conn = agent_connection()
    try:
        linked = conn.execute(
            "SELECT COUNT(*) FROM llm_calls l JOIN eval_runs r ON r.id = l.eval_run_id "
            "WHERE r.run_code = ?",
            (run_code,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert linked > 0


async def test_selfcheck_restores_llm_client_on_cold_start(client, monkeypatch, tmp_path) -> None:
    """自检批次结束必须**恢复**全局 LLM 客户端 —— 也包括「进程冷启动、`_CLIENT` 仍为 `None`」。

    历史缺陷（2026-10-16 真模型评测踩到、2026-10-19 修）：`previous_llm` 直接读
    `llm_client._CLIENT`，冷启动时取到 `None` ⇒ `finally: if previous_llm is not None`
    **跳过恢复** ⇒ 逐题注入的 `ScriptedLLM` 残留为全局客户端；此后同进程的 `impl=real` 批次
    **静默**变成脚本回放（`eval_runs.model_name` 仍写真实模型名，只有 `llm_calls.model_name`
    能看出是 `scripted-*`）—— **不花钱、但读数完全无意义**，是最难察觉的一类坑。
    """
    from app.core import llm_client as lc

    monkeypatch.setattr(eval_service, "REPORT_DIR", tmp_path)
    token, _ = await _register(client, "eval_run_cold")
    table = await _upload_eval_table(client, token)
    _insert_questions(_synthetic_questions(table))

    # 模拟服务冷启动：进程刚起来、还没调用过任何 LLM
    monkeypatch.setattr(lc, "_CLIENT", None)

    created = await client.post(
        "/api/v1/eval/runs",
        headers=_auth(token),
        json={"impl": "selfcheck", "question_codes": ["s001"]},
    )
    assert created.status_code == 200, created.text
    data = await _wait_run(client, token, created.json()["data"]["run_code"])
    assert data["status"] == "finished", data.get("error_message")

    # `status=finished` 先落库、`finally` 恢复紧随其后 ⇒ 留一个短窗口再断言（最多 5 s）
    configured = get_settings().llm_configured
    restored = None
    for _ in range(50):
        await asyncio.sleep(0.1)
        current = lc._CLIENT  # noqa: SLF001
        if current is not None and (not configured or current.name != "scripted"):
            restored = current
            break
    assert restored is not None, (
        "自检批次结束后全局 LLM 客户端没有恢复："
        f"当前为 {getattr(lc._CLIENT, 'name', lc._CLIENT)!r}"
        "（冷启动时 previous_llm 读到 None ⇒ finally 跳过恢复 ⇒ 后续 impl=real 会静默变成脚本回放）"
    )


@pytest.mark.skipif(not QUESTIONS_JSON.exists(), reason="题库未构建")
async def test_run_isolation_and_report_states(client, monkeypatch, tmp_path) -> None:
    """归属隔离：他人批次一律 404；不存在的批次/报告同理。"""
    monkeypatch.setattr(eval_service, "REPORT_DIR", tmp_path)
    token, _ = await _register(client, "eval_iso_01")
    other_token, _ = await _register(client, "eval_iso_02")
    table = await _upload_eval_table(client, token, filename="隔离用报表.csv")
    _insert_questions(_synthetic_questions(table, prefix="i"))

    created = await client.post(
        "/api/v1/eval/runs",
        headers=_auth(token),
        json={"impl": "selfcheck", "question_codes": ["i001"]},
    )
    run_code = created.json()["data"]["run_code"]

    assert (await client.get(f"/api/v1/eval/runs/{run_code}", headers=_auth(other_token))).status_code == 404
    assert (await client.get("/api/v1/eval/runs/run_19700101_99", headers=_auth(token))).status_code == 404
    assert (
        await client.get(f"/api/v1/eval/runs/{run_code}/report", headers=_auth(other_token))
    ).status_code == 404
    assert (
        await client.get("/api/v1/eval/runs/run_19700101_98/report", headers=_auth(token))
    ).status_code == 404

    await _wait_run(client, token, run_code)


def test_clarify_round_is_attributed_separately() -> None:
    """澄清轮单独归因（v1.19.26）：它既不是"执行失败"，也不是"结果口径偏差"。

    澄清轮**不落 analyses**（见 `analysis_service` 的 `clarify` 字段注释），评测端只看到"没有结果"，
    此前一律记成 `empty_result` +「生成结果为空（未执行成功或没有数据）」——读报告的人会以为是
    "SQL 查空了"。实测排查 q096 时就被这条描述带偏过一次（真因是槽位 metric 没解析出来 → 澄清）。
    """
    assert eval_service._attribute({"status": "clarify"}) == "澄清未执行"
    # 其余归因保持不变（不要因为新增分支而串味）
    assert eval_service._attribute({"status": "success"}) == "结果口径偏差"
    assert eval_service._attribute({"status": "failed"}) == "执行失败未重试成功"
    assert eval_service._attribute({"status": "clarify", "is_trap": True}) == "澄清未执行"


def test_model_call_failure_is_attributed_before_slot_inherit() -> None:
    """`E_LLM` 必须单独归因（2026-10-19，q107 的诊断结论）。

    旧实现只按 `qtype == "multi_turn"` 就判「slot 继承错误」，于是 q107
    （多轮题，`E_LLM 槽位解析超时`，两轮各 20.0 s）被记成了"多轮合并规则的 bug"——**假象在
    报表里挂了三天**，期间一直在查 merge_slot / 提示词，真因却是上游端点抖动。
    """
    q107 = {
        "status": "failed",
        "error_code": "E_LLM",
        "qtype": "multi_turn",
        "is_trap": False,
        "retries": 0,
    }
    assert eval_service._attribute(q107) == "模型调用失败"
    # 优先级高于"陷阱题"：模型**根本没答上来**时，不存在"理解错口径"这回事
    assert eval_service._attribute({**q107, "is_trap": True}) == "模型调用失败"
    # 澄清卡仍最优先（它压根没发起调用）
    assert eval_service._attribute({"status": "clarify", "error_code": "E_LLM"}) == "澄清未执行"
    # 其他错误码的既有归因不受影响
    assert eval_service._attribute({**q107, "error_code": "E_SQL_EXEC"}) == "SQL 语法与函数"
    assert eval_service._attribute({**q107, "error_code": "E_CODE_FORBIDDEN"}) == "SQL 语法与函数"
    # 多轮题**没有** error_code 才是真的继承/切换出错
    assert (
        eval_service._attribute({"status": "success", "qtype": "multi_turn", "retries": 0})
        == "slot 继承错误"
    )


def test_attribution_order_covers_every_bucket() -> None:
    """渲染顺序表必须**等于** `_attribute()` 的值域 —— 否则那一类错题会在报告里静静消失。

    v1.19.26 加「澄清未执行」时就漏了这一步：`_attribute()` 会返回它，但 Markdown 报告的
    渲染元组里没有这个键 ⇒ 一个"全是澄清卡"的批次，归因表看起来是空的。
    """
    samples = [
        {"status": "clarify"},
        {"status": "failed", "error_code": "E_LLM"},
        {"status": "success", "is_trap": True},
        {"status": "failed", "error_code": "E_SQL_EXEC"},
        {"status": "success", "qtype": "multi_turn", "retries": 0},
        {"status": "failed", "error_code": "E_GUARD_LIMIT", "retries": 2},
        {"status": "success"},
    ]
    produced = {eval_service._attribute(item) for item in samples}
    assert produced == set(eval_service.ATTRIBUTION_ORDER)
    # 顺序本身也是规格：澄清 / 模型调用失败排在最前（"没跑成"的先说）
    assert eval_service.ATTRIBUTION_ORDER[0] == "澄清未执行"
    assert eval_service.ATTRIBUTION_ORDER[1] == "模型调用失败"
    assert eval_service.ATTRIBUTION_ORDER[-1] == "结果口径偏差"
