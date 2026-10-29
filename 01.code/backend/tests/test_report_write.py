"""报表写路径测试：上传两段式（upload / confirm）、删除与预置保护、pending 生命周期。

覆盖本轮 8 条决策中可在无外部依赖下验证的部分：
- 决策 1 预置删除保护（403 + is_deletable）
- 决策 2 删除回显（dropped_table / freed_pages / 不 VACUUM）
- 决策 3 两段式（upload 返回真实行数与质量报告 → confirm 才建表）
- 决策 4 列名规范化（中文词典映射 + origin_name）+ 决策 5 sha256 复用
- 决策 6 列表不返回 pending + 决策 7 上传错误码与 hint
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.conftest import _REPORTS_DB

pytestmark = pytest.mark.anyio

_CSV_CN = (
    "日期,时间,总用电量(kWh),有功功率,电压,天气情况\n"
    "16/12/2006,17:24:00,4.216,0.418,234.840,晴\n"
    "17/12/2006,17:25:00,?,0.500,233.630,阴\n"
    "18/12/2006,17:26:00,5.360,-0.100,270.000,雨\n"
)
_CSV_NO_TIME = "项目,数值\nalpha,1\nbeta,2\n"
_CSV_LARGE_OK = (
    "日期,时间,用电量\n"
    "16/12/2006,17:24:00,1.0\n"
    "17/12/2006,17:25:00,2.0\n"
)


async def _register(client, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": "Abcd1234"}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload(client, token: str, filename: str, content: str):
    return await client.post(
        "/api/v1/reports/upload",
        headers=_auth(token),
        files={"file": (filename, content.encode("utf-8"), "text/csv")},
    )


def _reports_tables() -> set[str]:
    conn = sqlite3.connect(_REPORTS_DB)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


# ── 上传：解析预览与契约 ────────────────────────────────────────────────────

async def test_upload_requires_auth(client) -> None:
    response = await client.post(
        "/api/v1/reports/upload", files={"file": ("a.csv", b"a,b\n1,2\n", "text/csv")}
    )
    assert response.status_code == 401
    assert response.json()["data"]["error_code"] == "E_AUTH"


async def test_upload_parses_and_maps_chinese_columns(client) -> None:
    token = await _register(client, "up_owner_01")
    response = await _upload(client, token, "中文用电报表.csv", _CSV_CN)
    assert response.status_code == 200, response.text

    data = response.json()["data"]
    assert data["status"] == "pending"
    assert data["report_code"].startswith("r_up_")
    assert data["row_count"] == 3
    assert data["encoding"] == "utf-8"
    assert data["delimiter"] == ","
    assert data["time_column"] == "dt"
    assert data["time_start"] == "2006-12-16 17:24:00"
    assert data["is_duplicate"] is False

    # 决策 4：词典映射 + origin_name 保留中文原名；未命中词典的列回退 col_n
    names = [column["name"] for column in data["columns"]]
    assert names[:5] == ["date", "time", "total_kwh", "active_power_kw", "voltage"]
    assert "col_6" in names  # 「天气情况」不在词典 → 回退 col_n
    assert "dt" in names and {"year", "month", "day", "hour", "weekday", "is_weekend"} <= set(names)
    mapping = data["column_mapping"]
    assert mapping["总用电量(kWh)"] == "total_kwh"
    assert mapping["天气情况"] == "col_6"
    assert data["columns"][2]["origin_name"] == "总用电量(kWh)"
    # 比率列不可 SUM；普通数值列可 SUM；维度/文本列不可 SUM
    assert data["columns"][2]["agg"] == "sum"
    assert next(c for c in data["columns"] if c["name"] == "year")["agg"] == "none"

    # 决策 3：upload 返回真实行数、前 20 行预览与质量报告
    assert data["preview"]["returned_rows"] == 3
    assert data["preview"]["rows"][0]["total_kwh"] == 4.216
    dataset = data["quality"]["dataset"]
    assert dataset["total_rows"] == 3
    assert dataset["missing_rows"] == 0
    assert dataset["partially_missing_rows"] == 1
    assert dataset["missing_by_column"]["total_kwh"] == 1
    assert dataset["voltage_out_of_range_rows"] == 1  # 270V 超 200–260
    assert dataset["negative_power_rows"] == 1  # -0.1 kW
    assert data["quality"]["checks"] == {}  # 上传数据无 A1–A5 口径
    assert data["pending_expires_at"]

    # 两段式第一步**不建表**，且 pending 不出现在列表
    assert data["table_name"] not in _reports_tables()
    listing = await client.get("/api/v1/reports", headers=_auth(token))
    assert data["report_code"] not in {
        item["report_code"] for item in listing.json()["data"]["items"]
    }
    # pending 报表的预览接口给出明确指引（而不是含混的 404）
    preview = await client.get(
        f"/api/v1/reports/{data['report_code']}/preview", headers=_auth(token)
    )
    assert preview.status_code == 400
    assert "confirm" in preview.json()["message"]


async def test_upload_duplicate_reuses_existing_report(client) -> None:
    """决策 5：sha256 命中 → 复用（不再解析、不产生第二张表）。"""
    token = await _register(client, "up_owner_02")
    first = await _upload(client, token, "dup.csv", _CSV_LARGE_OK)
    assert first.status_code == 200
    second = await _upload(client, token, "dup_again.csv", _CSV_LARGE_OK)

    assert second.status_code == 200
    data = second.json()["data"]
    assert data["is_duplicate"] is True
    assert data["report_code"] == first.json()["data"]["report_code"]
    assert data["row_count"] == first.json()["data"]["row_count"]
    assert data["preview"]["returned_rows"] == 2
    assert "已复用" in second.json()["message"]


async def test_upload_errors_carry_code_and_hint(client) -> None:
    """决策 7：细分错误码 + `data.hint` 修复建议。"""
    token = await _register(client, "up_owner_03")

    bad_type = await _upload(client, token, "报表.xlsx", _CSV_LARGE_OK)
    assert bad_type.status_code == 400
    assert bad_type.json()["data"]["error_code"] == "E_FILE_TYPE"
    assert bad_type.json()["data"]["hint"]

    no_time = await _upload(client, token, "no_time.csv", _CSV_NO_TIME)
    assert no_time.status_code == 400
    assert no_time.json()["data"]["error_code"] == "E_NO_TIME_COLUMN"
    assert "Date" in no_time.json()["data"]["hint"]

    duplicate_header = await _upload(client, token, "dup_header.csv", "日期,日期\n1,2\n")
    assert duplicate_header.status_code == 400
    body = duplicate_header.json()["data"]
    assert body["error_code"] == "E_HEADER_INVALID"
    assert body["duplicate_columns"] == ["日期"]


# ── 确认入库 ────────────────────────────────────────────────────────────────

async def test_confirm_creates_dynamic_table_and_is_idempotent(client) -> None:
    token = await _register(client, "up_owner_04")
    uploaded = await _upload(client, token, "confirm_me.csv", _CSV_CN)
    code = uploaded.json()["data"]["report_code"]
    table = uploaded.json()["data"]["table_name"]
    assert table == "confirm_me"

    confirmed = await client.post(f"/api/v1/reports/{code}/confirm", headers=_auth(token))
    assert confirmed.status_code == 200, confirmed.text
    data = confirmed.json()["data"]
    assert data["status"] == "ready"
    assert data["inserted_rows"] == 3
    assert data["row_count"] == 3
    assert data["already_confirmed"] is False
    assert data["intermediate_removed"] is True
    assert f"ix_{table}_dt" in data["indexes"]

    # 物理表真的建起来了，且数据可查（时间派生列已写入）
    conn = sqlite3.connect(_REPORTS_DB)
    try:
        rows = conn.execute(
            f'SELECT total_kwh, year, month, day, hour, weekday, is_weekend FROM "{table}" ORDER BY dt'
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 3
    assert rows[0][0] == pytest.approx(4.216)
    assert rows[0][1:] == (2006, 12, 16, 17, 5, 1)  # 2006-12-16 是周六

    # 确认后进入列表，可读可预览
    listing = await client.get("/api/v1/reports", headers=_auth(token))
    item = next(i for i in listing.json()["data"]["items"] if i["report_code"] == code)
    assert item["status"] == "ready"
    assert item["table_exists"] is True
    assert item["is_deletable"] is True

    preview = await client.get(f"/api/v1/reports/{code}/preview", headers=_auth(token))
    assert preview.status_code == 200
    assert preview.json()["data"]["order_by"] == "dt"
    assert preview.json()["data"]["returned_rows"] == 3

    # 幂等：重复确认不报错、不重建
    again = await client.post(f"/api/v1/reports/{code}/confirm", headers=_auth(token))
    assert again.status_code == 200
    assert again.json()["data"]["already_confirmed"] is True
    assert again.json()["data"]["inserted_rows"] == 0


async def test_confirm_rejects_foreign_and_preset_reports(client) -> None:
    owner = await _register(client, "up_owner_05")
    other = await _register(client, "up_other_05")
    uploaded = await _upload(client, owner, "mine.csv", _CSV_LARGE_OK)
    code = uploaded.json()["data"]["report_code"]

    foreign = await client.post(f"/api/v1/reports/{code}/confirm", headers=_auth(other))
    assert foreign.status_code == 404  # 不泄露他人资源是否存在

    preset = await client.post("/api/v1/reports/r_daily/confirm", headers=_auth(owner))
    assert preset.status_code == 400
    assert preset.json()["data"]["error_code"] == "E_VALIDATION"


# ── 删除 ────────────────────────────────────────────────────────────────────

async def test_delete_preset_report_is_forbidden(client) -> None:
    token = await _register(client, "del_owner_01")
    response = await client.delete("/api/v1/reports/r_daily", headers=_auth(token))
    assert response.status_code == 403
    body = response.json()
    assert body["data"]["error_code"] == "E_FORBIDDEN"
    assert body["message"] == "系统预置报表不可删除"
    assert body["data"]["hint"]


async def test_delete_upload_drops_table_and_metadata(client) -> None:
    token = await _register(client, "del_owner_02")
    uploaded = await _upload(client, token, "to_delete.csv", _CSV_LARGE_OK)
    code = uploaded.json()["data"]["report_code"]
    table = uploaded.json()["data"]["table_name"]
    await client.post(f"/api/v1/reports/{code}/confirm", headers=_auth(token))
    assert table in _reports_tables()

    response = await client.delete(f"/api/v1/reports/{code}", headers=_auth(token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dropped_table"] == table
    assert f"ix_{table}_dt" in data["dropped_indexes"]
    assert data["files_removed"] is True
    assert data["affected_conversations"] == 0
    assert data["freed_pages"] >= 0  # 决策 2：报告回收页数，但不做 VACUUM

    assert table not in _reports_tables()
    conn = sqlite3.connect(_REPORTS_DB)
    try:
        # 元数据行已删除（逻辑上不复存在），报表不再出现在列表
        pass
    finally:
        conn.close()
    listing = await client.get("/api/v1/reports", headers=_auth(token))
    assert code not in {item["report_code"] for item in listing.json()["data"]["items"]}
    detail = await client.get(f"/api/v1/reports/{code}", headers=_auth(token))
    assert detail.status_code == 404


async def test_delete_pending_upload_cancels_without_table(client) -> None:
    """未确认的上传可直接删除（撤销上传），此时没有物理表可 DROP。"""
    token = await _register(client, "del_owner_03")
    uploaded = await _upload(client, token, "cancel_me.csv", _CSV_LARGE_OK)
    code = uploaded.json()["data"]["report_code"]

    response = await client.delete(f"/api/v1/reports/{code}", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["data"]["dropped_table"] is None
    assert response.json()["data"]["files_removed"] is True


async def test_delete_writes_audit_and_other_user_gets_404(client) -> None:
    owner = await _register(client, "del_owner_04")
    other = await _register(client, "del_other_04")
    uploaded = await _upload(client, owner, "audit_me.csv", _CSV_LARGE_OK)
    code = uploaded.json()["data"]["report_code"]

    foreign = await client.delete(f"/api/v1/reports/{code}", headers=_auth(other))
    assert foreign.status_code == 404

    assert (await client.delete(f"/api/v1/reports/{code}", headers=_auth(owner))).status_code == 200
    conn = sqlite3.connect(_REPORTS_DB.parent / "power_agent.db")
    try:
        rows = conn.execute(
            "SELECT event_type, action, rule_id, payload FROM audit_logs "
            "WHERE event_type = 'cascade_delete' ORDER BY id DESC LIMIT 1"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "删除应写 cascade_delete 审计"
    event_type, action, rule_id, payload = rows[0]
    assert (event_type, action, rule_id) == ("cascade_delete", "allowed", "report:delete")
    assert code in payload
