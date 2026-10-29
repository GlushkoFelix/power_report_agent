"""报表服务（PRD 9.1 报表模块的读路径）。

已实现：列表 / 详情 / 预览 / Schema / 质量。
未实现（后续批次）：上传、确认入库（写路径）；激活（归会话模块，需改 conversations.active_report_id）。

可见性（决策）：预置报表 `user_id IS NULL` 且 `is_preset = 1`，**所有用户共享可见**；
用户上传的报表仅本人可见（他人访问返回 403，不泄露资源是否存在）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import forbidden, not_found, validation_error
from app.db.models import UploadedReport
from app.db.session import control_session, report_ro_conn
from app.services import schema_service
from app.schemas.report import (
    PreviewColumn,
    PreviewOut,
    QualityOut,
    QualitySummary,
    ReportDetailOut,
    ReportListOut,
    ReportOut,
    SchemaOut,
)

#: 预览行数限制（决策：默认 50，上限 200）
PREVIEW_DEFAULT_LIMIT = 50
PREVIEW_MAX_LIMIT = 200

#: 表名合法性（防御性校验：表名无法参数化，必须白名单式约束）
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 各报表的时间列（用于确定性排序；分钟表走 dt 索引）
_TIME_COLUMNS: dict[str, str] = {
    "report_minute": "dt",
    "report_hourly": "dt_hour",
    "report_daily": "date",
    "report_monthly": "year_month",
    "report_energy_structure": "year",
}

#: 未知表（用户上传）探测时间列时的候选名（按优先级）
_TIME_COLUMN_CANDIDATES = (
    "dt", "datetime", "timestamp", "date", "dt_hour", "year_month", "month", "year",
)


# ── 报表查找与可见性 ────────────────────────────────────────────────────────

async def fetch_visible_report(
    session: AsyncSession, user_id: int, report_code: str
) -> UploadedReport:
    """按 `report_code` 取报表并校验可见性。

    - 不存在 / 已删除 → 404 `E_NOT_FOUND`
    - 他人上传的报表 → 403 `E_FORBIDDEN`（不泄露资源是否存在）
    """
    report = await session.scalar(
        select(UploadedReport).where(UploadedReport.report_code == report_code)
    )
    if report is None or report.status == "deleted":
        raise not_found(f"报表不存在：{report_code}")
    if not report.is_preset and report.user_id != user_id:
        raise forbidden()
    return report


async def _existing_tables() -> set[str]:
    """一次查询取回 `reports.db` 的全部物理表名（供 `table_exists` 自检）。"""
    async with report_ro_conn() as conn:
        rows = (
            await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
        ).scalars().all()
    return set(rows)


def _require_physical_table(report: UploadedReport, existing: set[str]) -> str:
    """校验物理表存在且表名合法，返回表名。"""
    table = report.table_name
    if report.status == "pending":
        # 已解析未确认（两段式第一步）：表还没建，明确告知而不是让前端猜 "表不存在"
        raise validation_error(
            f"报表 {report.report_code} 尚未确认入库，请先调用 "
            f"POST /reports/{report.report_code}/confirm"
        )
    if not _TABLE_NAME_RE.match(table):
        raise validation_error(f"非法表名：{table}")
    if table not in existing:
        raise not_found(
            f"报表 {report.report_code} 的物理表 {table} 不存在于 reports.db，"
            "请重新上传或运行建库脚本"
        )
    return table


# ── 列表 / 详情 ─────────────────────────────────────────────────────────────

async def list_reports(user_id: int) -> ReportListOut:
    """当前用户可见的报表：预置 5 张（共享）+ 本人上传（倒序）。

    排除 `deleted`（已删）与 `pending`（已解析未确认，决策 6：不入列表，
    重传同一文件可由 sha256 复用找回）。
    """
    async with control_session() as session:
        rows = (
            await session.scalars(
                select(UploadedReport)
                .where(or_(UploadedReport.is_preset == 1, UploadedReport.user_id == user_id))
                .where(~UploadedReport.status.in_(("deleted", "pending")))
            )
        ).all()

    existing = await _existing_tables()

    presets = sorted((row for row in rows if row.is_preset), key=lambda row: row.id)
    uploads = sorted(
        (row for row in rows if not row.is_preset), key=lambda row: row.created_at, reverse=True
    )

    items: list[ReportOut] = []
    for row in (*presets, *uploads):
        item = ReportOut.model_validate(row)
        item.table_exists = row.table_name in existing
        item.is_deletable = not bool(row.is_preset)  # 决策 1：预置不可删
        items.append(item)
    return ReportListOut(total=len(items), items=items)


def _quality_summary(quality: dict[str, Any] | None) -> QualitySummary:
    """从 `quality_json` 抽关键指标 + 校验通过标记（详情接口用）。"""
    if not quality:
        return QualitySummary()
    dataset = quality.get("dataset") or {}
    checks = quality.get("checks") or {}
    return QualitySummary(
        missing_rows=dataset.get("missing_rows"),
        missing_ratio=dataset.get("missing_ratio"),
        time_axis_gap=dataset.get("time_axis_gap"),
        duplicate_timestamps=dataset.get("duplicate_timestamps"),
        voltage_out_of_range_minutes=dataset.get("voltage_out_of_range_minutes"),
        checks={name: bool(detail.get("passed", True)) for name, detail in checks.items()},
    )


async def get_report(user_id: int, report_code: str) -> ReportDetailOut:
    """报表详情：元数据 + 质量摘要（PRD 9.1）。"""
    async with control_session() as session:
        report = await fetch_visible_report(session, user_id, report_code)
        existing = await _existing_tables()

        detail = ReportDetailOut.model_validate(report)
        detail.table_exists = report.table_name in existing
        detail.is_deletable = not bool(report.is_preset)
        detail.caliber_version = report.caliber_version
        detail.quality_summary = _quality_summary(
            json.loads(report.quality_json) if report.quality_json else None
        )
        return detail


# ── 预览 ────────────────────────────────────────────────────────────────────

async def preview_report(user_id: int, report_code: str, limit: int | None = None) -> PreviewOut:
    """数据预览：默认 50 行，`limit` 上限 200，对象数组返回。"""
    effective = PREVIEW_DEFAULT_LIMIT if limit is None else limit
    if effective < 1 or effective > PREVIEW_MAX_LIMIT:
        raise validation_error(f"limit 取值范围为 1–{PREVIEW_MAX_LIMIT}，当前为 {effective}")

    async with control_session() as session:
        report = await fetch_visible_report(session, user_id, report_code)

    existing = await _existing_tables()
    table = _require_physical_table(report, existing)

    async with report_ro_conn() as conn:
        info = (await conn.execute(text(f"PRAGMA table_info({table})"))).mappings().all()
        # 过滤自增主键（决策）：代理键对分析无意义，且 schema_json 本就不含它 ——
        # 保留会导致「预览列数 ≠ Schema 字典列数」，前端两处对不上
        hidden = [row["name"] for row in info if row["pk"]]
        columns = [
            PreviewColumn(name=row["name"], type=row["type"] or "TEXT")
            for row in info
            if not row["pk"]
        ]

        # 排序依据仍在**完整列**中解析（主键列参与排序也无妨）
        order_by = _resolve_order_by(table, [row["name"] for row in info])
        rows = (
            await conn.execute(text(f"SELECT * FROM {table} ORDER BY {order_by} LIMIT {effective}"))
        ).mappings().all()

    payload = [{key: value for key, value in row.items() if key not in hidden} for row in rows]
    return PreviewOut(
        report_code=report.report_code,
        table_name=table,
        columns=columns,
        rows=payload,
        total_rows=report.row_count,  # 取元数据，不跑 COUNT(*)
        returned_rows=len(payload),
        limit=effective,
        truncated=bool(report.row_count and report.row_count > len(payload)),
        order_by=order_by,
        excluded_columns=hidden,
    )


def _resolve_order_by(table: str, column_names: list[str]) -> str:
    """确定预览排序依据：已知报表用固定时间列；未知表按候选名探测；都无则 rowid。"""
    known = _TIME_COLUMNS.get(table)
    if known and known in column_names:
        return known
    for candidate in _TIME_COLUMN_CANDIDATES:
        if candidate in column_names:
            return candidate
    return "rowid"


# ── Schema ──────────────────────────────────────────────────────────────────

async def get_schema(user_id: int, report_code: str) -> SchemaOut:
    """Schema 描述：结构化来自 `schema_json`；文本为**完整注入文本**（交付物 3 落地）。

    文本与注入给模型的说明逐字一致（UIUX 5.3 区域 4）：预置表取自 `schema_text.md` 原文
    （全局头 + 本表字段 + 口径红线 + SQLite 函数 + 常用问题示例），上传表由 `schema_service`
    生成（含口径未知声明），并受 2000 tokens 预算约束。
    """
    async with control_session() as session:
        report = await fetch_visible_report(session, user_id, report_code)
        structured = json.loads(report.schema_json) if report.schema_json else None
        if structured is None:
            # 未回填时即时用表结构兜底，避免接口直接 404（正常路径不会走到）
            structured = await _fallback_structure(report)
        payload = await schema_service.ensure_injection_text(session, report)
        await session.commit()
        result = SchemaOut(
            report_code=report.report_code,
            table_name=report.table_name,
            caliber_version=report.caliber_version,
            structured=structured,
            text=payload["text"],
            text_source=payload["text_source"],
            est_tokens=payload["est_tokens"],
            truncated=payload["truncated"],
        )
    return result


async def _fallback_structure(report: UploadedReport) -> dict[str, Any]:
    """兜底：由 `PRAGMA table_info` 生成最小结构化描述。"""
    async with report_ro_conn() as conn:
        info = (await conn.execute(text(f"PRAGMA table_info({report.table_name})"))).mappings().all()
    return {
        "table": report.table_name,
        "title": report.filename,
        "caliber_version": report.caliber_version,
        "row_count": report.row_count,
        "time_range": [report.time_start, report.time_end],
        "columns": [
            {"name": row["name"], "type": row["type"] or "TEXT", "unit": "—", "agg": "—",
             "description": ""}
            for row in info
        ],
    }


# ── 质量 ────────────────────────────────────────────────────────────────────

async def get_quality(user_id: int, report_code: str) -> QualityOut:
    """质量报告：`uploaded_reports.quality_json`（按表裁剪的数据集级质量 + 相关校验）。"""
    async with control_session() as session:
        report = await fetch_visible_report(session, user_id, report_code)

    quality = json.loads(report.quality_json) if report.quality_json else {}
    return QualityOut(
        report_code=report.report_code,
        table_name=report.table_name,
        caliber_version=report.caliber_version,
        quality=quality,
    )
