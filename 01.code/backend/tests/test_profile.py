"""个人中心接口测试（PRD F6.4）：编辑资料 / 修改密码 / 清空历史记录 / 删除账号。

四个接口此前是"按钮置灰 + 后端暂无该接口"的**交付缺口**（PRD F6.4 未闭环），本轮补齐后
这里把**每条契约**钉住：字段白名单、未提交字段不动、密码强度与旧密码校验、
清空口径（记录 vs 会话）、删除账号的双确认与"安全日志保留（user_id 置空）"。

数据造法：直接用 sqlite3 往临时库插行（不经 LLM），断言的落库效果最直接。
"""

from __future__ import annotations

import os
import sqlite3

import pytest

pytestmark = pytest.mark.anyio


def agent_connection() -> sqlite3.Connection:
    """直连本会话的临时系统库（路径由 conftest 在导入 app 前写入环境变量）。"""
    return sqlite3.connect(os.environ["POWER_AGENT_DB_PATH"])


async def _register(client, username: str, password: str = "Abcd1234") -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password, "display_name": "初始名", "email": "a@b.com"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _delete_me(client, headers: dict[str, str], payload: dict) -> object:
    """`DELETE /auth/me` 需要请求体（密码 + 用户名双确认）。

    httpx 的 `client.delete()` **不接受 `json=`**（这也是很多客户端库的现状），
    所以走通用 `request("DELETE", ...)` —— 前端用 axios 的 `data` 传体，等价。
    """
    return await client.request("DELETE", "/api/v1/auth/me", headers=headers, json=payload)


def _seed_activity(username: str, *, code: str = "c_20260918_9001") -> dict[str, int]:
    """造一份"有会话 / 消息 / 分析记录 / 模型调用"的历史数据，返回各表行数基线。"""
    with agent_connection() as conn:
        user_id = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()[0]
        conv_id = conn.execute(
            "INSERT INTO conversations (conversation_code, user_id, title, message_count)"
            " VALUES (?, ?, '历史会话', 2)",
            (code, user_id),
        ).lastrowid
        analysis_id = conn.execute(
            "INSERT INTO analyses (record_code, user_id, conversation_id, question, status)"
            " VALUES (?, ?, ?, '全期总用电量是多少？', 'success')",
            (f"a_{code}", user_id, conv_id),
        ).lastrowid
        conn.execute(
            "INSERT INTO messages (conversation_id, user_id, seq_no, role, content, analysis_id)"
            " VALUES (?, ?, 1, 'user', '全期总用电量是多少？', ?)",
            (conv_id, user_id, analysis_id),
        )
        conn.execute(
            "INSERT INTO messages (conversation_id, user_id, seq_no, role, content, analysis_id)"
            " VALUES (?, ?, 2, 'assistant', '总用电量 1000 kWh', ?)",
            (conv_id, user_id, analysis_id),
        )
        conn.execute(
            "INSERT INTO llm_calls (analysis_id, stage, model_name, status)"
            " VALUES (?, 'gen_sql', 'scripted', 'success')",
            (analysis_id,),
        )
        conn.execute(
            "INSERT INTO eval_runs (run_code, user_id, question_total, status)"
            " VALUES (?, ?, 3, 'finished')",
            (f"run_{code}", user_id),
        )
        conn.commit()
    return {"user_id": user_id, "conversation_id": conv_id, "analysis_id": analysis_id}


def _counts(username: str) -> dict[str, int]:
    with agent_connection() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            return {"user": 0}
        user_id = row[0]
        return {
            "user": 1,
            "conversations": conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE user_id = ?", (user_id,)
            ).fetchone()[0],
            "analyses": conn.execute(
                "SELECT COUNT(*) FROM analyses WHERE user_id = ?", (user_id,)
            ).fetchone()[0],
            "messages": conn.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,)
            ).fetchone()[0],
            "llm_calls": conn.execute(
                "SELECT COUNT(*) FROM llm_calls c JOIN analyses a ON a.id = c.analysis_id"
                " WHERE a.user_id = ?",
                (user_id,),
            ).fetchone()[0],
            "eval_runs": conn.execute(
                "SELECT COUNT(*) FROM eval_runs WHERE user_id = ?", (user_id,)
            ).fetchone()[0],
        }


# ── 编辑资料 ────────────────────────────────────────────────────────────────


async def test_update_profile_only_touches_submitted_fields(client) -> None:
    """只改显示名 → 邮箱原样；显式传 null → 清空；什么都不传 → 400。"""
    data = await _register(client, "prof_01")
    headers = _auth(data["token"])

    only_name = await client.patch(
        "/api/v1/auth/me", headers=headers, json={"display_name": "新显示名"}
    )
    assert only_name.status_code == 200
    assert only_name.json()["data"]["display_name"] == "新显示名"
    assert only_name.json()["data"]["email"] == "a@b.com"  # 未提交 → 不动

    cleared = await client.patch("/api/v1/auth/me", headers=headers, json={"email": ""})
    assert cleared.status_code == 200
    assert cleared.json()["data"]["email"] is None  # 空串视为清空
    assert cleared.json()["data"]["display_name"] == "新显示名"

    empty = await client.patch("/api/v1/auth/me", headers=headers, json={})
    assert empty.status_code == 400
    assert empty.json()["data"]["error_code"] == "E_VALIDATION"


async def test_update_profile_rejects_bad_email_and_audits(client) -> None:
    data = await _register(client, "prof_02")
    response = await client.patch(
        "/api/v1/auth/me", headers=_auth(data["token"]), json={"email": "not-an-email"}
    )
    assert response.status_code == 422

    with agent_connection() as conn:
        events = [
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM audit_logs WHERE user_id = ?", (data["user"]["id"],)
            )
        ]
    assert "auth_profile_updated" not in events  # 校验失败不该留下"已更新"的假日志


# ── 修改密码 ────────────────────────────────────────────────────────────────


async def test_change_password_requires_correct_old_password(client) -> None:
    data = await _register(client, "prof_03")
    headers = _auth(data["token"])

    wrong = await client.post(
        "/api/v1/auth/password",
        headers=headers,
        json={"old_password": "Wrong123", "new_password": "Newpass123"},
    )
    assert wrong.status_code == 400
    assert "当前密码不正确" in wrong.json()["message"]

    with agent_connection() as conn:
        rows = [
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM audit_logs WHERE user_id = ?", (data["user"]["id"],)
            )
        ]
    assert "auth_password_change_failed" in rows  # 失败也留痕，但不含密码明文
    with agent_connection() as conn:
        payloads = [
            row[0] or ""
            for row in conn.execute("SELECT payload FROM audit_logs WHERE user_id = ?", (data["user"]["id"],))
        ]
    assert all("Wrong123" not in text and "Newpass123" not in text for text in payloads)


async def test_change_password_then_login_with_new_one(client) -> None:
    data = await _register(client, "prof_04", password="Abcd1234")
    changed = await client.post(
        "/api/v1/auth/password",
        headers=_auth(data["token"]),
        json={"old_password": "Abcd1234", "new_password": "Zxcv9876"},
    )
    assert changed.status_code == 200

    old_login = await client.post(
        "/api/v1/auth/login", json={"username": "prof_04", "password": "Abcd1234"}
    )
    assert old_login.status_code == 401
    new_login = await client.post(
        "/api/v1/auth/login", json={"username": "prof_04", "password": "Zxcv9876"}
    )
    assert new_login.status_code == 200

    # 强度规则与注册同一套：纯数字/纯字母都不行
    weak = await client.post(
        "/api/v1/auth/password",
        headers=_auth(data["token"]),
        json={"old_password": "Zxcv9876", "new_password": "12345678"},
    )
    assert weak.status_code == 422


async def test_change_password_rejects_same_as_old(client) -> None:
    data = await _register(client, "prof_05", password="Abcd1234")
    same = await client.post(
        "/api/v1/auth/password",
        headers=_auth(data["token"]),
        json={"old_password": "Abcd1234", "new_password": "Abcd1234"},
    )
    assert same.status_code == 400
    assert "不能与当前密码相同" in same.json()["message"]


# ── 清空历史记录 ────────────────────────────────────────────────────────────


async def test_clear_records_scope_records_keeps_conversation(client) -> None:
    """`records`：清分析记录（含模型调用流水），**会话与对话文本保留**。"""
    data = await _register(client, "prof_06")
    _seed_activity("prof_06", code="c_20260918_9002")

    response = await client.post(
        "/api/v1/auth/clear-records", headers=_auth(data["token"]), json={"scope": "records"}
    )
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["deleted_records"] == 1
    assert payload["deleted_llm_calls"] == 1
    assert payload["deleted_conversations"] == 0

    counts = _counts("prof_06")
    assert counts["analyses"] == 0 and counts["llm_calls"] == 0
    assert counts["conversations"] == 1 and counts["messages"] == 2  # 对话文本保留
    with agent_connection() as conn:
        # 消息的分析引用被外键置空（不删消息本身）
        assert conn.execute("SELECT analysis_id FROM messages LIMIT 1").fetchone()[0] is None


async def test_clear_records_scope_all_wipes_conversations_and_eval_runs_kept(client) -> None:
    """`all`：连会话与对话一起清；**评测批次行不受清空历史影响**（属评测资产）。"""
    data = await _register(client, "prof_07")
    _seed_activity("prof_07", code="c_20260918_9003")

    response = await client.post(
        "/api/v1/auth/clear-records", headers=_auth(data["token"]), json={"scope": "all"}
    )
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["deleted_conversations"] == 1
    assert payload["deleted_messages"] == 2

    counts = _counts("prof_07")
    assert counts["conversations"] == 0 and counts["analyses"] == 0 and counts["messages"] == 0
    assert counts["eval_runs"] == 1  # 清历史记录不删评测批次


async def test_clear_records_rejects_unknown_scope(client) -> None:
    data = await _register(client, "prof_08")
    response = await client.post(
        "/api/v1/auth/clear-records", headers=_auth(data["token"]), json={"scope": "everything"}
    )
    assert response.status_code == 422


# ── 删除账号 ────────────────────────────────────────────────────────────────


async def test_delete_account_needs_password_and_username(client) -> None:
    data = await _register(client, "prof_09")
    headers = _auth(data["token"])

    wrong_password = await _delete_me(
        client, headers, {"password": "Wrong123", "confirm_username": "prof_09"}
    )
    assert wrong_password.status_code == 400
    assert "密码不正确" in wrong_password.json()["message"]
    assert _counts("prof_09")["user"] == 1  # 账号还在

    wrong_name = await _delete_me(
        client, headers, {"password": "Abcd1234", "confirm_username": "prof_99"}
    )
    assert wrong_name.status_code == 400
    assert "用户名不一致" in wrong_name.json()["message"]
    assert _counts("prof_09")["user"] == 1


async def test_delete_account_cascades_but_keeps_audit_logs(client) -> None:
    """删除账号：业务数据全清（含评测批次），**安全日志保留且 user_id 置空**。"""
    data = await _register(client, "prof_10")
    user_id = data["user"]["id"]
    _seed_activity("prof_10", code="c_20260918_9004")

    response = await _delete_me(
        client,
        _auth(data["token"]),
        {"password": "Abcd1234", "confirm_username": "prof_10"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["username"] == "prof_10"
    assert payload["deleted_conversations"] == 1
    assert payload["deleted_records"] == 1
    assert payload["deleted_eval_runs"] == 1
    assert payload["kept_audit_logs"] >= 2  # 注册 + 删除账号本身

    counts = _counts("prof_10")
    assert counts == {"user": 0}  # 用户行已删（其余表随之外键级联清空）

    with agent_connection() as conn:
        orphaned = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE user_id IS NULL"
            " AND event_type = 'auth_account_deleted'"
        ).fetchone()[0]
        assert orphaned == 1  # 日志保留，归属置空
        assert (
            conn.execute("SELECT COUNT(*) FROM conversations WHERE user_id = ?", (user_id,)).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM eval_runs WHERE user_id = ?", (user_id,)).fetchone()[0]
            == 0
        )


async def test_delete_account_requires_token(client) -> None:
    response = await client.request(
        "DELETE",
        "/api/v1/auth/me",
        json={"password": "Abcd1234", "confirm_username": "nobody_01"},
    )
    assert response.status_code == 401
