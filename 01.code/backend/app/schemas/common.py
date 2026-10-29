"""统一响应包络（对应 PRD 9.1）。

除注册 / 登录 / 健康检查外均需 JWT；所有接口响应统一为：

    { "code": 0, "message": "ok", "data": {...}, "request_id": "<16 位十六进制>" }

`request_id` 由 `app.main.RequestIdMiddleware` 生成并同时写入响应头 `X-Request-ID`。
"""

from __future__ import annotations

from typing import Any


def ok(data: Any = None, message: str = "ok", request_id: str | None = None) -> dict[str, Any]:
    """构造成功响应包络（code = 0）。"""
    return {"code": 0, "message": message, "data": data, "request_id": request_id or "-"}


def fail(
    http_status: int,
    error_code: str,
    message: str,
    data: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """构造失败响应包络（双轨制：`code` = HTTP 状态码，业务错误码放 `data.error_code`）。"""
    payload: dict[str, Any] = {"error_code": error_code}
    if data:
        payload.update(data)
    return {
        "code": http_status,
        "message": message,
        "data": payload,
        "request_id": request_id or "-",
    }
