"""报表读路径接口测试：详情 / 预览 / Schema / 质量 / 可见性。

对应决策：URL 用业务编号；预览默认 50 行上限 200；Schema 文本取自 schema_text.md；
上传表回退渲染；他人上传的报表返回 403。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def _token(client, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": "Abcd1234"}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_all_read_endpoints_require_auth(client) -> None:
    for path in (
        "/api/v1/reports/r_daily",
        "/api/v1/reports/r_daily/preview",
        "/api/v1/reports/r_daily/schema",
        "/api/v1/reports/r_daily/quality",
    ):
        response = await client.get(path)
        assert response.status_code == 401, path
        assert response.json()["data"]["error_code"] == "E_AUTH"


async def test_detail_returns_metadata_and_quality_summary(client) -> None:
    token = await _token(client, "mike_01")
    response = await client.get("/api/v1/reports/r_daily", headers=_auth(token))
    assert response.status_code == 200

    data = response.json()["data"]
    assert data["report_code"] == "r_daily"
    assert data["table_exists"] is True
    assert data["caliber_version"] == "caliber-v1.0"
    assert data["is_active"] == 1  # 系统默认活跃报表

    summary = data["quality_summary"]
    assert summary["missing_rows"] == 25979
    assert summary["missing_ratio"] == pytest.approx(0.012518)
    assert summary["time_axis_gap"] == 0
    assert summary["checks"] == {"A1_levels_equal": True, "A5_row_counts": True}


async def test_detail_not_found(client) -> None:
    token = await _token(client, "nina_01")
    response = await client.get("/api/v1/reports/r_not_exist", headers=_auth(token))
    assert response.status_code == 404
    assert response.json()["data"]["error_code"] == "E_NOT_FOUND"


async def test_detail_of_others_upload_is_forbidden(client) -> None:
    """他人上传的报表：403，且文案不泄露资源是否存在（决策）。"""
    token = await _token(client, "oscar_01")
    response = await client.get("/api/v1/reports/r_up_9999", headers=_auth(token))
    assert response.status_code == 403
    body = response.json()
    assert body["data"]["error_code"] == "E_FORBIDDEN"
    assert body["message"] == "无权访问该资源"


async def test_preview_contract(client) -> None:
    token = await _token(client, "peter_01")
    response = await client.get("/api/v1/reports/r_daily/preview", headers=_auth(token))
    assert response.status_code == 200

    data = response.json()["data"]
    assert data["report_code"] == "r_daily"
    assert data["table_name"] == "report_daily"
    assert data["limit"] == 50  # 默认值
    assert data["order_by"] == "date"  # 时间列正序
    assert data["total_rows"] == 1442  # 取自元数据，未跑 COUNT(*)
    assert {column["name"] for column in data["columns"]} >= {"date", "consumption_kwh"}
    assert all({"name", "type"} == set(column.keys()) for column in data["columns"])
    # 自增主键被过滤（决策：保证预览列与 Schema 字典列一致）
    assert "id" not in {column["name"] for column in data["columns"]}
    assert data["excluded_columns"] == ["id"]
    # 临时库的 report_daily 表为空 → 无数据行，但契约字段齐备
    assert data["rows"] == []
    assert data["returned_rows"] == 0
    assert data["truncated"] is True


async def test_preview_limit_override(client) -> None:
    token = await _token(client, "quinn_01")
    response = await client.get("/api/v1/reports/r_daily/preview?limit=5", headers=_auth(token))
    assert response.json()["data"]["limit"] == 5


@pytest.mark.parametrize("limit", [0, -1, 201, 999])
async def test_preview_limit_out_of_range_returns_400(client, limit: int) -> None:
    """越界由业务层抛 400（决策），不是框架的 422。"""
    # 每个参数用独立用户名（临时库为会话级共享，重复注册会 409）
    token = await _token(client, f"rita{abs(limit)}")
    response = await client.get(
        f"/api/v1/reports/r_daily/preview?limit={limit}", headers=_auth(token)
    )
    assert response.status_code == 400
    assert response.json()["data"]["error_code"] == "E_VALIDATION"


async def test_schema_text_matches_injected_file(client) -> None:
    """预置表的文本必须取自 schema_text.md 原文（前端字典 = 模型注入文本）。"""
    token = await _token(client, "sam_01")
    response = await client.get("/api/v1/reports/r_daily/schema", headers=_auth(token))
    assert response.status_code == 200

    data = response.json()["data"]
    assert data["text_source"] == "schema_text.md"
    assert data["structured"]["table"] == "report_daily"
    assert len(data["structured"]["columns"]) == 2
    assert "report_daily" in data["text"]
    assert "consumption_kwh" in data["text"]


async def test_schema_generates_text_for_unknown_table(client) -> None:
    """`schema_text.md` 中没有该表段落（等同上传表）→ 由生成器产出完整注入文本。

    交付物 3 起：不再是简易回退渲染，而是补齐六段（含口径未知声明与 SQLite 函数清单），
    因此标签由 `rendered` 改为 `generated`，并带上 `est_tokens` / `truncated`。
    """
    token = await _token(client, "tina_01")
    response = await client.get("/api/v1/reports/r_ghost/schema", headers=_auth(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["text_source"] == "generated"
    assert data["est_tokens"] > 0
    assert data["truncated"] is False
    assert "report_ghost_missing" in data["text"]
    assert "U1（口径未知）" in data["text"]
    assert "## 三、SQLite 可用函数" in data["text"]


async def test_quality_returns_backfilled_payload(client) -> None:
    token = await _token(client, "uma_01")
    response = await client.get("/api/v1/reports/r_daily/quality", headers=_auth(token))
    assert response.status_code == 200

    quality = response.json()["data"]["quality"]
    assert quality["level"] == "dataset"
    assert quality["dataset"]["missing_rows"] == 25979
    assert quality["dataset"]["voltage_out_of_range_minutes"] == 4010
    assert set(quality["checks"]) == {"A1_levels_equal", "A5_row_counts"}
