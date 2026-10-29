"""智电析报 · FastAPI 入口：中间件与路由挂载（对应《系统架构设计文档》附录 A）。

启动（在 `01.code/backend/` 下，使用项目根统一虚拟环境）：

    e:\\power_report_agent\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import BizError, E_AUTH, E_FORBIDDEN, E_INTERNAL, E_NOT_FOUND, E_VALIDATION
from app.db.session import dispose_engines
from app.schemas.common import fail
from app.services import report_write_service, sandbox_service

settings = get_settings()
logger = logging.getLogger("zdxb.api")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动清理过期 pending 上传，退出时释放两库连接池。

    启动清理失败**不阻断启动**（属维护性动作，不是服务可用性的前提）：
    例如 `reports.db` 被临时占用时只记 warning，服务照常提供只读查询。
    """
    try:
        removed = await report_write_service.cleanup_expired_pending()
        if removed:
            logger.info("启动清理：回收过期未确认上传 %d 条", removed)
    except Exception:  # noqa: BLE001
        logger.warning("启动清理过期上传失败（不影响启动）", exc_info=True)
    # 预热 pandas 沙箱进程池（spawn + pandas import 的冷启动成本在启动期付掉；失败不阻断）
    await sandbox_service.warm_sandbox()
    yield
    await dispose_engines()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个请求生成 `request_id`，贯穿日志与响应（PRD 6.5 全链路日志）。"""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        # 访问日志：只记方法/路径/状态/耗时/request_id，**不记请求体与 Authorization 头**（PRD S5 日志脱敏）
        logger.info(
            "%s %s -> %s %.0fms rid=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request_id,
        )
        return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def register_exception_handlers(app: FastAPI) -> None:
    """统一异常 → 响应包络（双轨制：`code` = HTTP 状态码，`data.error_code` = `E_*`）。"""

    @app.exception_handler(BizError)
    async def _handle_biz_error(request: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=fail(exc.http_status, exc.error_code, exc.message, exc.data, _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first.get("loc", []) if part != "body")
        message = f"请求参数不合法：{field} {first.get('msg', '')}".strip()
        return JSONResponse(
            status_code=422,
            content=fail(422, E_VALIDATION, message, None, _request_id(request)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {401: E_AUTH, 403: E_FORBIDDEN, 404: E_NOT_FOUND, 422: E_VALIDATION}
        error_code = mapping.get(exc.status_code, E_INTERNAL)
        return JSONResponse(
            status_code=exc.status_code,
            content=fail(exc.status_code, error_code, str(exc.detail), None, _request_id(request)),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 rid=%s", _request_id(request))
        return JSONResponse(
            status_code=500,
            content=fail(500, E_INTERNAL, "服务内部错误，请稍后重试", None, _request_id(request)),
        )


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
