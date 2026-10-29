"""安全模块测试：bcrypt 哈希/校验、72 字节守卫、JWT 签发/校验/过期。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import get_settings
from app.core.errors import BizError
from app.core.security import (
    BCRYPT_MAX_BYTES,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_hash_is_bcrypt_and_verifiable() -> None:
    hashed = hash_password("Abcd1234")
    assert len(hashed) == 60  # bcrypt 固定 60 字符
    assert hashed.startswith("$2b$")
    assert verify_password("Abcd1234", hashed) is True
    assert verify_password("Abcd12345", hashed) is False


def test_same_password_yields_different_hash() -> None:
    """加盐：同一明文两次哈希结果不同（PRD F6.1 要求加盐哈希）。"""
    assert hash_password("Abcd1234") != hash_password("Abcd1234")


def test_password_over_72_bytes_is_rejected() -> None:
    """中文密码 25 字 = 75 字节，必须显式拒绝而不是静默截断。"""
    assert len(("中" * 25).encode("utf-8")) == 75 > BCRYPT_MAX_BYTES
    with pytest.raises(BizError) as exc:
        hash_password("中" * 25)
    assert exc.value.error_code == "E_VALIDATION"


def test_password_exactly_72_bytes_is_accepted() -> None:
    assert len(("中" * 24).encode("utf-8")) == 72
    hashed = hash_password("中" * 24)
    assert verify_password("中" * 24, hashed) is True


def test_verify_password_never_raises_on_broken_hash() -> None:
    assert verify_password("Abcd1234", "not-a-bcrypt-hash") is False


def test_access_token_roundtrip() -> None:
    token, expires_at = create_access_token(7, "alice")
    claims = decode_access_token(token)
    assert claims["sub"] == "7"
    assert claims["username"] == "alice"
    assert expires_at > datetime.now(timezone.utc)


def test_expired_token_rejected() -> None:
    settings = get_settings()
    expired = jwt.encode(
        {
            "sub": "1",
            "username": "alice",
            "exp": int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp()),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(BizError) as exc:
        decode_access_token(expired)
    assert (exc.value.http_status, exc.value.error_code) == (401, "E_AUTH")


def test_token_signed_with_other_key_rejected() -> None:
    forged = jwt.encode(
        {"sub": "1", "exp": 4102444800},
        "another-key-of-sufficient-length-for-hs256-use",
        algorithm="HS256",
    )
    with pytest.raises(BizError) as exc:
        decode_access_token(forged)
    assert exc.value.http_status == 401
