"""报表列表接口测试：鉴权、可见性、字段契约、table_exists 自检。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def _token(client, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": "Abcd1234"}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["token"]


async def test_reports_requires_auth(client) -> None:
    response = await client.get("/api/v1/reports")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == 401
    assert body["data"]["error_code"] == "E_AUTH"
    assert body["request_id"]


async def test_reports_list_contract_and_table_exists(client) -> None:
    token = await _token(client, "judy_01")
    response = await client.get("/api/v1/reports", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200

    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["total"] == 2

    expected_keys = {
        "id", "report_code", "filename", "report_type", "table_name", "time_start", "time_end",
        "row_count", "column_count", "status", "is_preset", "is_active", "created_at",
        "table_exists", "is_deletable",
    }
    assert expected_keys == set(data["items"][0].keys())
    # 不含大字段正文（另有 /reports/{id}/schema、/quality 两个接口）
    assert "schema_json" not in data["items"][0]
    assert "quality_json" not in data["items"][0]

    daily = next(item for item in data["items"] if item["report_code"] == "r_daily")
    assert daily["table_exists"] is True
    assert daily["is_active"] == 1  # 系统默认活跃报表（决策语义）
    assert daily["is_preset"] == 1
    assert daily["is_deletable"] is False  # 预置不可删（决策 1）
    assert daily["row_count"] == 1442

    ghost = next(item for item in data["items"] if item["report_code"] == "r_ghost")
    assert ghost["table_exists"] is False  # 元数据说有、物理表没了 → 接口层暴露

    # 预置在前，按 id 升序
    assert [item["report_code"] for item in data["items"]][:2] == ["r_daily", "r_ghost"]


async def test_reports_are_shared_across_users(client) -> None:
    """预置报表 user_id IS NULL，所有用户可见（决策）。"""
    first = await _token(client, "kate_01")
    second = await _token(client, "leo_01")
    for token in (first, second):
        response = await client.get("/api/v1/reports", headers={"Authorization": f"Bearer {token}"})
        codes = {item["report_code"] for item in response.json()["data"]["items"]}
        assert codes == {"r_daily", "r_ghost"}
