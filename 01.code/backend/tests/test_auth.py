"""认证接口测试：注册（自动登录 / 重复 / 校验）、登录（计数 / 锁定）、鉴权、登出审计。"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.core.errors import LOCK_MINUTES, MAX_LOGIN_FAILURES

pytestmark = pytest.mark.anyio

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def agent_connection() -> sqlite3.Connection:
    """直连本会话的临时系统库（路径由 conftest 在导入 app 前写入环境变量）。

    注意：**不要** `from tests.conftest import ...`——那会让 conftest 被二次导入，
    导致重复创建临时库并覆盖环境变量。
    """
    return sqlite3.connect(os.environ["POWER_AGENT_DB_PATH"])


async def _register(client, username: str, password: str = "Abcd1234") -> dict:
    response = await client.post(
        "/api/v1/auth/register", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_register_returns_token_and_lowercases_username(client) -> None:
    data = await _register(client, "Alice_01")
    assert data["user"]["username"] == "alice_01"  # 决策：统一小写存储
    assert data["token_type"] == "Bearer"
    assert data["expires_at"]

    me = await client.get("/api/v1/auth/me", headers=_auth(data["token"]))
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "alice_01"


async def test_register_duplicate_username_is_case_insensitive(client) -> None:
    await _register(client, "Carol_01")
    response = await client.post(
        "/api/v1/auth/register", json={"username": "carol_01", "password": "Abcd1234"}
    )
    assert response.status_code == 409
    assert response.json()["data"]["error_code"] == "E_USERNAME_TAKEN"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"username": "ab", "password": "Abcd1234"}, "用户名过短"),
        ({"username": "bad-name!", "password": "Abcd1234"}, "含非法字符"),
        ({"username": "dave_01", "password": "abcdefgh"}, "密码无数字"),
        ({"username": "dave_02", "password": "12345678"}, "密码无字母"),
        ({"username": "dave_03", "password": "Ab1"}, "密码过短"),
    ],
)
async def test_register_validation(client, payload: dict, reason: str) -> None:
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422, f"{reason}: {response.text}"
    assert response.json()["data"]["error_code"] == "E_VALIDATION"


async def test_login_success_resets_counter(client) -> None:
    await _register(client, "erin_01")
    bad = await client.post(
        "/api/v1/auth/login", json={"username": "erin_01", "password": "Wrong123"}
    )
    assert bad.status_code == 401
    assert bad.json()["data"]["error_code"] == "E_CREDENTIALS"
    with agent_connection() as conn:
        assert conn.execute(
            "SELECT login_fail_count FROM users WHERE username = 'erin_01'"
        ).fetchone()[0] == 1

    good = await client.post(
        "/api/v1/auth/login", json={"username": "erin_01", "password": "Abcd1234"}
    )
    assert good.status_code == 200
    with agent_connection() as conn:
        row = conn.execute(
            "SELECT login_fail_count, locked_until, last_login_at FROM users WHERE username = 'erin_01'"
        ).fetchone()
    assert row[0] == 0 and row[1] is None and row[2] is not None


async def test_unknown_username_uses_same_message(client) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"username": "nobody_01", "password": "Abcd1234"}
    )
    assert response.status_code == 401
    assert response.json()["data"]["error_code"] == "E_CREDENTIALS"
    assert response.json()["message"] == "用户名或密码错误"  # 不泄露账号是否存在


async def test_lock_after_five_failures_then_429(client) -> None:
    await _register(client, "frank_01")
    for attempt in range(1, MAX_LOGIN_FAILURES):
        response = await client.post(
            "/api/v1/auth/login", json={"username": "frank_01", "password": "Wrong123"}
        )
        assert response.status_code == 401, f"第 {attempt} 次应为 401"

    fifth = await client.post(
        "/api/v1/auth/login", json={"username": "frank_01", "password": "Wrong123"}
    )
    assert fifth.status_code == 429
    assert fifth.json()["data"]["error_code"] == "E_AUTH_LOCKED"

    # 锁定期内即使密码正确也直接 429（不校验密码）
    again = await client.post(
        "/api/v1/auth/login", json={"username": "frank_01", "password": "Abcd1234"}
    )
    assert again.status_code == 429
    assert again.json()["data"]["locked_seconds"] > 0


async def test_lock_expiry_does_not_reset_failure_count(client) -> None:
    """决策：锁定到期不清零，只有成功登录才清零 —— 到期后再错一次立即重新锁定。"""
    await _register(client, "grace_01")
    for _ in range(MAX_LOGIN_FAILURES):
        await client.post("/api/v1/auth/login", json={"username": "grace_01", "password": "Wrong123"})

    past = (datetime.now() - timedelta(minutes=LOCK_MINUTES + 1)).strftime(TIME_FMT)
    with agent_connection() as conn:
        conn.execute("UPDATE users SET locked_until = ? WHERE username = 'grace_01'", (past,))
        conn.commit()
        assert conn.execute(
            "SELECT login_fail_count FROM users WHERE username = 'grace_01'"
        ).fetchone()[0] == MAX_LOGIN_FAILURES

    again = await client.post(
        "/api/v1/auth/login", json={"username": "grace_01", "password": "Wrong123"}
    )
    assert again.status_code == 429  # 计数未清零 → 立即再次锁定


async def test_disabled_account_forbidden(client) -> None:
    await _register(client, "henry_01")
    with agent_connection() as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE username = 'henry_01'")
        conn.commit()
    response = await client.post(
        "/api/v1/auth/login", json={"username": "henry_01", "password": "Abcd1234"}
    )
    assert response.status_code == 403
    assert response.json()["data"]["error_code"] == "E_ACCOUNT_DISABLED"


async def test_me_requires_valid_token(client) -> None:
    no_token = await client.get("/api/v1/auth/me")
    assert no_token.status_code == 401
    assert no_token.json()["data"]["error_code"] == "E_AUTH"

    bad_token = await client.get("/api/v1/auth/me", headers=_auth("not-a-jwt"))
    assert bad_token.status_code == 401
    assert bad_token.json()["data"]["error_code"] == "E_AUTH"


async def test_logout_writes_audit_log(client) -> None:
    data = await _register(client, "ivan_01")
    response = await client.post("/api/v1/auth/logout", headers=_auth(data["token"]))
    assert response.status_code == 200
    assert response.json()["data"] is None

    with agent_connection() as conn:
        events = [
            row[0]
            for row in conn.execute(
                "SELECT event_type FROM audit_logs WHERE user_id = ? ORDER BY id", (data["user"]["id"],)
            )
        ]
    assert events == ["auth_register", "auth_logout"]  # 注册只写一条（auth_login 留给登录接口）
