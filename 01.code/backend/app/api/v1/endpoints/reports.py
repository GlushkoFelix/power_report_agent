"""报表接口（对应 PRD 9.1 报表模块）。

读路径：列表 / 详情 / 预览 / Schema / 质量。
写路径：上传（两段式第一步）/ 确认入库 / 删除（本批实现）。
`POST /reports/{report_code}/activate` 归会话模块（写 `conversations.active_report_id`）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Query, Request, UploadFile

from app.api.v1.deps import CurrentUser, client_ip, client_user_agent, request_id
from app.schemas.common import ok
from app.schemas.conversation import ActivateReportIn
from app.services import conversation_service, report_service, report_write_service

router = APIRouter(tags=["reports"])


@router.get("/reports", summary="报表列表（预置共享 + 本人上传）")
async def list_reports(request: Request, user: CurrentUser) -> dict[str, Any]:
    """当前用户可见的报表列表（预置 5 张共享 + 本人上传），含物理表存在性自检。"""
    result = await report_service.list_reports(user.id)
    return ok(result.model_dump(), request_id=request_id(request))


@router.get("/reports/{report_code}", summary="报表详情（元数据 + 质量摘要）")
async def get_report(report_code: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    """报表详情：元数据 + 质量摘要。"""
    result = await report_service.get_report(user.id, report_code)
    return ok(result.model_dump(), request_id=request_id(request))


@router.get("/reports/{report_code}/preview", summary="数据预览（前 N 行 + 排序依据）")
async def preview_report(
    report_code: str,
    request: Request,
    user: CurrentUser,
    # 不在 Query 上做范围校验：越界统一由 service 抛 400 `E_VALIDATION`（决策），
    # 而非 FastAPI 默认的 422（保持错误码语义一致）
    limit: int | None = Query(default=None, description="返回行数，默认 50，上限 200"),
) -> dict[str, Any]:
    """数据预览（前 N 行，按时间列正序）。"""
    result = await report_service.preview_report(user.id, report_code, limit)
    return ok(result.model_dump(), request_id=request_id(request))


@router.get("/reports/{report_code}/schema", summary="报表 Schema（结构化 + 注入文本原文）")
async def get_schema(report_code: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    """Schema 描述：结构化（`schema_json`）+ 文本（注入文本原文）。"""
    result = await report_service.get_schema(user.id, report_code)
    return ok(result.model_dump(), request_id=request_id(request))


@router.get("/reports/{report_code}/quality", summary="数据质量报告（数据集级 + 本表口径校验）")
async def get_quality(report_code: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    """质量报告详情（数据集级质量 + 与本报表相关的口径校验）。"""
    result = await report_service.get_quality(user.id, report_code)
    return ok(result.model_dump(), request_id=request_id(request))


@router.post("/reports/{report_code}/activate", summary="设为会话当前数据源")
async def activate_report(
    report_code: str,
    payload: ActivateReportIn,
    request: Request,
    user: CurrentUser,
) -> dict[str, Any]:
    """把指定会话的活跃报表切到本报表（报表视角语法糖，与 PATCH /conversations/... 等价）。"""
    result = await conversation_service.set_active_report(
        user.id,
        payload.conversation_code,
        report_code,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
    )
    return ok(result.model_dump(), "已切换活跃报表", request_id(request))


# ── 写路径（上传两段式 + 删除）──────────────────────────────────────────────

@router.post("/reports/upload", summary="上传报表（解析 + 建表，待确认）")
async def upload_report(
    request: Request,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="CSV/TXT 报表文件，≤200MB")],
) -> dict[str, Any]:
    """上传 CSV/TXT：校验 → 全量解析一次 → 登记为 `pending` 报表并返回预览与质量报告。

    两段式第一步；确认入库请再调用 `POST /reports/{report_code}/confirm`。
    若同一文件（sha256 相同）此前已上传且未删除，则**直接复用**既有报表（`is_duplicate=true`）。
    """
    result = await report_write_service.upload_report(
        user.id,
        file,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
    )
    message = "该文件已上传过，已复用现有报表" if result.is_duplicate else "解析完成，请确认入库"
    return ok(result.model_dump(), message, request_id(request))


@router.post("/reports/{report_code}/confirm", summary="确认上传入库（提交写锁）")
async def confirm_report(
    report_code: str,
    request: Request,
    user: CurrentUser,
) -> dict[str, Any]:
    """确认入库：把 `pending` 报表的解析结果建成 `reports.db` 中的动态表。"""
    result = await report_write_service.confirm_report(
        user.id,
        report_code,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
    )
    message = "该报表此前已入库" if result.already_confirmed else "入库完成，可切换为活跃报表开始分析"
    return ok(result.model_dump(), message, request_id(request))


@router.delete("/reports/{report_code}", summary="删除报表（含物理表与落盘文件）")
async def delete_report(
    report_code: str,
    request: Request,
    user: CurrentUser,
) -> dict[str, Any]:
    """删除报表：级联清理动态表、元数据与分析记录（预置报表返回 403）。"""
    result = await report_write_service.delete_report(
        user.id,
        report_code,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
    )
    return ok(result.model_dump(), "报表已删除", request_id(request))
