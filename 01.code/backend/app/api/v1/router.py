"""API v1 路由聚合。`app/main.py` 只挂载本对象，不直接挂载各 endpoint。

对应《系统架构设计文档》附录 A：`api/v1/endpoints/` 承载
auth / reports / conversations / analysis / charts / eval / system 七组接口。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import analysis, auth, charts, conversations, eval, reports, system

api_router = APIRouter()
api_router.include_router(system.router)
api_router.include_router(auth.router)
api_router.include_router(reports.router)
api_router.include_router(conversations.router)
api_router.include_router(analysis.router)
api_router.include_router(charts.router)
api_router.include_router(eval.router)
