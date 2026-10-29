"""会话模块测试：新建（三级回落）/ 列表（置顶与筛选）/ 详情 / 修改 / 删除 / 消息游标分页。"""

from __future__ import annotations

import os
import sqlite3

import pytest

from app.services.conversation_service import MAX_PINNED

pytestmark = pytest.mark.anyio


def agent_connection() -> sqlite3.Connection:
    return sqlite3.connect(os.environ["POWER_AGENT_DB_PATH"])


async def _token(client, username: str) -> str:
    response = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": "Abcd1234"}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create(client, token: str, **body) -> dict:
    response = await client.post("/api/v1/conversations", json=body, headers=_auth(token))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _insert_messages(conversation_code: str, count: int) -> None:
    with agent_connection() as conn:
        row = conn.execute(
            "SELECT id, user_id FROM conversations WHERE conversation_code = ?",
            (conversation_code,),
        ).fetchone()
        for seq in range(1, count + 1):
            conn.execute(
                "INSERT INTO messages (conversation_id, user_id, seq_no, role, content, created_at)"
                " VALUES (?, ?, ?, 'user', ?, datetime('now','localtime'))",
                (row[0], row[1], seq, f"msg {seq}"),
            )
        conn.commit()


# ── 新建：三级回落 ──────────────────────────────────────────────────────────

async def test_create_conversation_uses_system_default_first(client) -> None:
    token = await _token(client, "zoe_01")
    data = await _create(client, token)
    assert data["conversation_code"].startswith("c_")
    assert data["active_report_code"] == "r_daily"  # 系统默认活跃报表
    assert data["active_report_source"] == "system_default"
    assert data["is_pinned"] == 0
    assert data["message_count"] == 0


async def test_create_conversation_inherits_last_conversation_report(client) -> None:
    token = await _token(client, "yves_01")
    await _create(client, token, active_report_code="r_ghost")  # explicit
    inherited = await _create(client, token)  # 不传 → 延续上一个会话
    assert inherited["active_report_code"] == "r_ghost"
    assert inherited["active_report_source"] == "last_conversation"


async def test_create_conversation_with_explicit_report(client) -> None:
    token = await _token(client, "xena_01")
    data = await _create(client, token, title="看看日报", active_report_code="r_ghost")
    assert data["title"] == "看看日报"
    assert data["active_report_code"] == "r_ghost"
    assert data["active_report_source"] == "explicit"


async def test_create_conversation_rejects_unknown_or_foreign_report(client) -> None:
    token = await _token(client, "wade_01")
    missing = await client.post(
        "/api/v1/conversations",
        json={"active_report_code": "r_not_exist"},
        headers=_auth(token),
    )
    assert missing.status_code == 404

    foreign = await client.post(
        "/api/v1/conversations",
        json={"active_report_code": "r_up_9999"},
        headers=_auth(token),
    )
    assert foreign.status_code == 403


# ── 列表：置顶分组与筛选 ────────────────────────────────────────────────────

async def test_list_orders_pinned_first(client) -> None:
    token = await _token(client, "vic_01")
    first = await _create(client, token, title="第一个")
    second = await _create(client, token, title="第二个")
    third = await _create(client, token, title="第三个")

    pinned = await client.patch(
        f"/api/v1/conversations/{first['conversation_code']}",
        json={"is_pinned": True},
        headers=_auth(token),
    )
    assert pinned.status_code == 200

    listed = await client.get("/api/v1/conversations", headers=_auth(token))
    codes = [item["conversation_code"] for item in listed.json()["data"]["items"]]
    assert codes[0] == first["conversation_code"]  # 置顶在最前
    assert set(codes[1:]) == {second["conversation_code"], third["conversation_code"]}


async def test_list_supports_keyword_and_date_filter(client) -> None:
    token = await _token(client, "ulysses_01")
    await _create(client, token, title="月度用电趋势")
    await _create(client, token, title="峰谷占比分析")

    hit = await client.get("/api/v1/conversations?q=月度", headers=_auth(token))
    assert [i["title"] for i in hit.json()["data"]["items"]] == ["月度用电趋势"]

    none = await client.get("/api/v1/conversations?date_from=2099-01-01", headers=_auth(token))
    assert none.json()["data"]["total"] == 0


# ── 详情 / 归属 ─────────────────────────────────────────────────────────────

async def test_detail_returns_slot(client) -> None:
    token = await _token(client, "tam_01")
    created = await _create(client, token)
    with agent_connection() as conn:
        conn.execute(
            "UPDATE conversations SET slot_json = ? WHERE conversation_code = ?",
            ('{"period":"peak","granularity":"month"}', created["conversation_code"]),
        )
        conn.commit()

    response = await client.get(
        f"/api/v1/conversations/{created['conversation_code']}", headers=_auth(token)
    )
    assert response.status_code == 200
    assert response.json()["data"]["slot"] == {"period": "peak", "granularity": "month"}


async def test_other_users_conversation_is_forbidden(client) -> None:
    owner = await _token(client, "sammy_01")
    created = await _create(client, owner)

    intruder = await _token(client, "ralph_01")
    response = await client.get(
        f"/api/v1/conversations/{created['conversation_code']}", headers=_auth(intruder)
    )
    assert response.status_code == 403
    assert response.json()["data"]["error_code"] == "E_FORBIDDEN"


async def test_unknown_conversation_is_not_found(client) -> None:
    token = await _token(client, "quill_01")
    response = await client.get("/api/v1/conversations/c_20200101_0001", headers=_auth(token))
    assert response.status_code == 404


# ── 修改：重命名 / 置顶 / 切换 ──────────────────────────────────────────────

async def test_patch_requires_at_least_one_field(client) -> None:
    token = await _token(client, "pam_01")
    created = await _create(client, token)
    response = await client.patch(
        f"/api/v1/conversations/{created['conversation_code']}", json={}, headers=_auth(token)
    )
    assert response.status_code == 400
    assert response.json()["data"]["error_code"] == "E_VALIDATION"


async def test_patch_rename_and_switch_report(client) -> None:
    token = await _token(client, "olive_01")
    created = await _create(client, token)
    response = await client.patch(
        f"/api/v1/conversations/{created['conversation_code']}",
        json={"title": "改个名字", "active_report_code": "r_ghost"},
        headers=_auth(token),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["title"] == "改个名字"
    assert data["active_report_code"] == "r_ghost"
    assert data["evicted_pinned_code"] is None


async def test_pin_over_limit_evicts_oldest(client) -> None:
    """决策：每用户最多 5 条置顶，第 6 条置顶时自动挤出最早置顶的那条。"""
    token = await _token(client, "nate_01")
    codes = [(await _create(client, token, title=f"会话{i}"))["conversation_code"] for i in range(MAX_PINNED + 1)]

    evicted = None
    for index, code in enumerate(codes):
        response = await client.patch(
            f"/api/v1/conversations/{code}", json={"is_pinned": True}, headers=_auth(token)
        )
        assert response.status_code == 200
        if index == MAX_PINNED:
            evicted = response.json()["data"]["evicted_pinned_code"]

    assert evicted == codes[0]  # 最早置顶的被挤出
    with agent_connection() as conn:
        # 只统计该用户名下的置顶数（临时库为会话级共享，其他用例也会置顶）
        pinned_total = conn.execute(
            "SELECT COUNT(*) FROM conversations c JOIN users u ON u.id = c.user_id"
            " WHERE c.is_pinned = 1 AND u.username = 'nate_01'"
        ).fetchone()[0]
    assert pinned_total == MAX_PINNED


async def test_unpin_resets_flag(client) -> None:
    token = await _token(client, "mona_01")
    created = await _create(client, token)
    await client.patch(
        f"/api/v1/conversations/{created['conversation_code']}",
        json={"is_pinned": True},
        headers=_auth(token),
    )
    response = await client.patch(
        f"/api/v1/conversations/{created['conversation_code']}",
        json={"is_pinned": False},
        headers=_auth(token),
    )
    assert response.json()["data"]["is_pinned"] == 0
    assert response.json()["data"]["pinned_at"] is None


# ── 报表视角的 activate（与 PATCH 等价）─────────────────────────────────────

async def test_activate_endpoint_equals_patch(client) -> None:
    token = await _token(client, "lena_01")
    created = await _create(client, token)

    response = await client.post(
        "/api/v1/reports/r_ghost/activate",
        json={"conversation_code": created["conversation_code"]},
        headers=_auth(token),
    )
    assert response.status_code == 200
    assert response.json()["data"]["active_report_code"] == "r_ghost"

    detail = await client.get(
        f"/api/v1/conversations/{created['conversation_code']}", headers=_auth(token)
    )
    assert detail.json()["data"]["active_report_code"] == "r_ghost"


# ── 删除 ────────────────────────────────────────────────────────────────────

async def test_delete_cascades_messages_and_writes_audit(client) -> None:
    token = await _token(client, "kim_01")
    created = await _create(client, token)
    code = created["conversation_code"]
    _insert_messages(code, 3)

    with agent_connection() as conn:
        conversation_id = conn.execute(
            "SELECT id FROM conversations WHERE conversation_code = ?", (code,)
        ).fetchone()[0]

    response = await client.delete(f"/api/v1/conversations/{code}", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["data"]["deleted_messages"] == 3

    with agent_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE conversation_code = ?", (code,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,)
        ).fetchone()[0] == 0
        # 临时库为全测试会话共享，`cascade_delete` 也可能由报表删除写入 →
        # 断言需精确到本用例的规则号，否则会被其它用例的行抢先命中
        audit = conn.execute(
            "SELECT event_type, rule_id FROM audit_logs "
            "WHERE event_type = 'cascade_delete' AND rule_id = 'delete_conversation'"
        ).fetchone()
    assert audit == ("cascade_delete", "delete_conversation")


# ── 消息历史：游标分页 ──────────────────────────────────────────────────────

async def test_messages_cursor_paging(client) -> None:
    token = await _token(client, "jane_01")
    created = await _create(client, token)
    code = created["conversation_code"]
    _insert_messages(code, 120)

    # 默认最近 50 条，正序返回
    first = await client.get(f"/api/v1/conversations/{code}/messages", headers=_auth(token))
    assert first.status_code == 200
    data = first.json()["data"]
    assert data["limit"] == 50
    assert len(data["items"]) == 50
    assert data["newest_seq"] == 120
    assert data["oldest_seq"] == 71
    assert data["has_more"] is True
    assert [item["seq_no"] for item in data["items"]] == list(range(71, 121))

    # 向历史翻页：取 seq_no < 71 的更早消息
    second = await client.get(
        f"/api/v1/conversations/{code}/messages?before_seq=71", headers=_auth(token)
    )
    data2 = second.json()["data"]
    assert data2["newest_seq"] == 70
    assert data2["oldest_seq"] == 21
    assert data2["has_more"] is True

    # 最后一页
    third = await client.get(
        f"/api/v1/conversations/{code}/messages?before_seq=21", headers=_auth(token)
    )
    assert third.json()["data"]["has_more"] is False
    assert len(third.json()["data"]["items"]) == 20


async def test_messages_limit_out_of_range(client) -> None:
    token = await _token(client, "iris_01")
    created = await _create(client, token)
    response = await client.get(
        f"/api/v1/conversations/{created['conversation_code']}/messages?limit=500",
        headers=_auth(token),
    )
    assert response.status_code == 400
    assert response.json()["data"]["error_code"] == "E_VALIDATION"
