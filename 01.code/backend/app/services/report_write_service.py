"""报表写路径服务（PRD F1.1–F1.5、F1.6 删除；9.1 的三个写接口）。

三个入口，全部在**进程内写锁**（`app/core/locks.py`）内串行执行：

1. `upload_report`：`POST /reports/upload`
   校验 → 流式落盘（边写边算 sha256）→ sha256 去重 → **全量解析一次**（线程池）
   → 插入 `pending` 行 + `upload_files(status='parsed')` → 返回预览与质量报告；
2. `confirm_report`：`POST /reports/{report_code}/confirm`
   读规范化中间 CSV → 建表 → 分批灌数（每批提交）→ 建索引 → `status='ready'` → 删中间文件；
3. `delete_report`：`DELETE /reports/{report_code}`
   预置 403 → 标记 `deleted` + 审计 → `DROP TABLE` → 删元数据行（触发
   `conversations.active_report_id` 置 NULL、`upload_files` 级联）→ 删落盘目录。

设计约束（本轮 grilling 决策）：
- `reports.db` 的写入**只走 `report_rw_conn()`**（`mode=rw` + `BEGIN IMMEDIATE`），
  LLM 生成代码永远只读；
- **不做 VACUUM**：删表后的空闲页留给后续上传复用，响应里以 `freed_pages` 如实报告；
- 类型/依赖：解析用 pandas 但只做 IO 与向量化转换，长耗时段落放入 `asyncio.to_thread`，
  事件循环始终可响应（健康检查、列表、后续 SSE）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import UploadFile
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import locks
from app.core.config import get_settings
from app.core.errors import (
    BizError,
    file_too_large,
    file_type_unsupported,
    ingest_failed,
    not_found,
    preset_not_deletable,
    validation_error,
)
from app.db.models import Conversation, UploadedReport, UploadFileRecord
from app.db.session import control_session, report_ro_conn, report_write_conn
from app.schemas.report import (
    ConfirmOut,
    DeleteOut,
    PreviewColumn,
    UploadColumnOut,
    UploadOut,
    UploadPreviewOut,
)
from app.services import audit_service, schema_service, upload_parse

logger = logging.getLogger("zdxb.upload")

__all__ = ["upload_report", "confirm_report", "delete_report", "cleanup_expired_pending"]

#: 中间规范化文件名（与原始文件同目录，confirm 成功后删除）
NORMALIZED_FILENAME = "normalized.csv"

#: 未确认上传的暂存子目录（按用户隔离）
STAGING_DIR = "_staging"

#: 建索引的列（时间列 + 派生维度列，PRD 数据库设计 4.6）
INDEXED_COLUMNS = ("dt", "year", "month", "day", "hour")

#: 表名/列名合法性（拼 SQL 前必须自检：DDL 片段无法参数化）
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: `report_code` 前缀（业务编号，URL 标识约定）
REPORT_CODE_PREFIX = "r_up_"

#: 单次读取上传流的块大小
_UPLOAD_CHUNK = 1024 * 1024


# ── 上传（两段式第一步）────────────────────────────────────────────────────

async def upload_report(
    user_id: int,
    upload: UploadFile,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UploadOut:
    """解析上传文件并登记为 `pending` 报表（不建表，等 confirm）。"""
    settings = get_settings()
    filename = Path(upload.filename or "upload.csv").name  # 防路径穿越
    suffix = Path(filename).suffix.lower()
    if suffix not in upload_parse.ALLOWED_SUFFIXES:
        raise file_type_unsupported(filename, allowed=" / ".join(upload_parse.ALLOWED_SUFFIXES))

    staging = settings.upload_dir / str(user_id) / STAGING_DIR
    staging.mkdir(parents=True, exist_ok=True)
    temp_path = staging / f"{int(time.time() * 1000)!s}_{filename}"
    sha256, size = await _stream_to_file(upload, temp_path, settings.upload_max_mb)

    try:
        async with locks.report_write_lock():
            await cleanup_expired_pending()
            duplicate = await _find_duplicate(user_id, sha256)
            if duplicate is not None:
                logger.info("命中重复上传：sha256=%s → %s", sha256[:12], duplicate.report_code)
                return await _duplicate_response(duplicate)
            return await _parse_and_register(user_id, filename, temp_path, sha256, size)
    finally:
        temp_path.unlink(missing_ok=True)  # 成功时已被移动到正式目录


async def _stream_to_file(upload: UploadFile, target: Path, max_mb: int) -> tuple[str, int]:
    """流式落盘：边写边算 sha256，超限立即中断（PRD F1.1 大小校验）。"""
    digest = hashlib.sha256()
    size = 0
    limit = max_mb * 1024 * 1024
    with target.open("wb") as handle:
        while True:
            chunk = await upload.read(_UPLOAD_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                handle.close()
                target.unlink(missing_ok=True)
                raise file_too_large(size / 1024 / 1024, max_mb)
            digest.update(chunk)
            handle.write(chunk)
    return digest.hexdigest(), size


async def _parse_and_register(
    user_id: int, filename: str, temp_path: Path, sha256: str, size: int
) -> UploadOut:
    """探测 → 全量解析（线程池）→ 落库 → 迁移文件到正式目录。"""
    settings = get_settings()
    encoding = await asyncio.to_thread(upload_parse.detect_encoding, temp_path)
    first_line = await asyncio.to_thread(upload_parse.read_first_line, temp_path, encoding)
    delimiter = upload_parse.detect_delimiter(first_line)
    upload_parse.read_header(first_line, delimiter)  # 表头校验提前失败，避免白跑解析

    normalized_path = temp_path.with_name(f"{temp_path.stem}.{NORMALIZED_FILENAME}")
    try:
        result = await asyncio.to_thread(
            upload_parse.parse_and_normalize,
            temp_path,
            normalized_path,
            encoding=encoding,
            delimiter=delimiter,
            preview_rows=settings.upload_preview_rows,
        )
    except BizError:
        normalized_path.unlink(missing_ok=True)
        raise

    async with control_session() as session:
        taken = await _taken_table_names(session)
        table_name = upload_parse.build_table_name(
            filename, taken, fallback_suffix=f"{int(time.time()) % 10_000_000:07d}"
        )
        now = audit_service.now_text()
        report = UploadedReport(
            report_code=f"tmp-{sha256[:12]}",  # 占位，flush 后用自增 id 生成正式编号
            user_id=user_id,
            filename=filename,
            table_name=table_name,
            report_type="uploaded",
            row_count=result.row_count,
            column_count=len(result.columns),
            time_start=result.time_start,
            time_end=result.time_end,
            caliber_version=None,  # 上传数据未经本项目口径清洗，不冒充口径版本
            is_preset=0,
            is_active=0,
            status="pending",
            schema_json=json.dumps(_schema_json(table_name, filename, result), ensure_ascii=False),
            quality_json=json.dumps(result.quality, ensure_ascii=False),
            created_at=now,
            updated_at=now,
        )
        session.add(report)
        await session.flush()
        report.report_code = f"{REPORT_CODE_PREFIX}{report.id:06d}"
        await session.flush()

        report_dir = settings.upload_dir / str(user_id) / report.report_code
        report_dir.mkdir(parents=True, exist_ok=True)
        original_path = report_dir / filename
        shutil.move(str(temp_path), original_path)
        shutil.move(str(normalized_path), report_dir / NORMALIZED_FILENAME)

        session.add(
            UploadFileRecord(
                report_id=report.id,
                origin_filename=filename,
                stored_path=_data_relative(original_path),
                file_size=size,
                sha256=sha256,
                encoding=encoding,
                delimiter=delimiter,
                uploaded_at=now,
                status="parsed",
            )
        )
        await session.commit()

        code = report.report_code

    logger.info(
        "上传解析完成：%s（%s）%d 行 × %d 列，编码 %s，分隔符 %r",
        code, table_name, result.row_count, len(result.columns), encoding, delimiter,
    )
    return UploadOut(
        report_code=code,
        filename=filename,
        table_name=table_name,
        status="pending",
        encoding=encoding,
        delimiter=delimiter,
        row_count=result.row_count,
        column_count=len(result.columns),
        raw_column_count=result.raw_column_count,
        time_column=result.time_column,
        time_start=result.time_start,
        time_end=result.time_end,
        column_mapping={spec.origin_name: spec.name for spec in result.columns
                        if spec.origin_name != spec.name},
        columns=[UploadColumnOut(**spec.to_schema_dict()) for spec in result.columns],
        preview=UploadPreviewOut(
            columns=[PreviewColumn(**column) for column in result.preview_columns],
            rows=result.preview_rows,
            returned_rows=len(result.preview_rows),
            preview_rows=settings.upload_preview_rows,
        ),
        quality=result.quality,
        pending_expires_at=(datetime.now() + timedelta(hours=settings.upload_pending_ttl_hours))
        .strftime(audit_service.TIME_FMT),
    )


def _schema_json(table_name: str, filename: str, result: upload_parse.ParseResult) -> dict[str, Any]:
    """构造 `uploaded_reports.schema_json`（结构对齐数据库设计 4.2，列级含 `origin_name`）。

    交付物 3 起，**同时生成注入文本**（`injection_text` + `injection_tokens` + `injection_truncated`，
    零 DDL 改动）：生成逻辑放在上传链路，使 `pending` 报表在确认前就能提供预览与字典，
    且不在读取路径上重复拼装。
    """
    schema: dict[str, Any] = {
        "table": table_name,
        "title": Path(filename).stem,
        "description": f"用户上传报表：{filename}",
        "caliber_version": None,
        "row_count": result.row_count,
        "time_range": [result.time_start, result.time_end],
        "columns": [spec.to_schema_dict() for spec in result.columns],
    }
    text, truncated = schema_service.fit_to_budget(schema, result.quality)
    schema[schema_service.INJECTION_TEXT_KEY] = text
    schema["injection_tokens"] = schema_service.estimate_tokens(text)
    schema["injection_truncated"] = truncated
    return schema


async def _taken_table_names(session: AsyncSession) -> set[str]:
    """已被占用的物理表名 = `reports.db` 现有表 ∪ 全部元数据记录的 `table_name`。"""
    rows = (await session.scalars(select(UploadedReport.table_name))).all()
    async with report_ro_conn() as conn:
        physical = (
            await conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))
        ).scalars().all()
    return set(rows) | set(physical)


# ── 重复上传（决策 5：命中即复用）────────────────────────────────────────────

async def _find_duplicate(user_id: int, sha256: str) -> UploadedReport | None:
    """按 sha256 查同用户的未删除报表（**限本人**，不泄露他人是否传过同一文件）。"""
    async with control_session() as session:
        row = (
            await session.execute(
                select(UploadedReport)
                .join(UploadFileRecord, UploadFileRecord.report_id == UploadedReport.id)
                .where(
                    UploadFileRecord.sha256 == sha256,
                    UploadedReport.user_id == user_id,
                    UploadedReport.status.in_(("pending", "ready")),
                )
                .order_by(UploadedReport.id.desc())
            )
        ).scalars().first()
        return row


async def _duplicate_response(report: UploadedReport) -> UploadOut:
    """复用响应：不再解析，用已存的 schema/quality 回放预览（pending 读中间文件、ready 读表）。"""
    settings = get_settings()
    schema = json.loads(report.schema_json) if report.schema_json else {}
    quality = json.loads(report.quality_json) if report.quality_json else {}
    columns = schema.get("columns") or []
    rows = await _preview_rows(report, [column["name"] for column in columns])

    async with control_session() as session:
        record = (
            await session.execute(
                select(UploadFileRecord).where(UploadFileRecord.report_id == report.id)
            )
        ).scalars().first()

    return UploadOut(
        report_code=report.report_code,
        filename=report.filename,
        table_name=report.table_name,
        status=report.status,
        encoding=record.encoding if record else "",
        delimiter=record.delimiter if record else ",",
        row_count=report.row_count or 0,
        column_count=report.column_count or len(columns),
        raw_column_count=len(columns),
        time_column=upload_parse.TIME_COLUMN,
        time_start=report.time_start,
        time_end=report.time_end,
        is_duplicate=True,
        column_mapping={
            column["origin_name"]: column["name"]
            for column in columns
            if column.get("origin_name") and column["origin_name"] != column["name"]
        },
        columns=[
            UploadColumnOut(
                name=column["name"],
                origin_name=column.get("origin_name") or column["name"],
                type=column.get("type") or "TEXT",
                unit=column.get("unit") or "—",
                agg=column.get("agg") or "none",
                description=column.get("description") or "",
                derived=bool(column.get("derived")),
            )
            for column in columns
        ],
        preview=UploadPreviewOut(
            columns=[
                PreviewColumn(name=column["name"], type=column.get("type") or "TEXT")
                for column in columns
            ],
            rows=rows,
            returned_rows=len(rows),
            preview_rows=settings.upload_preview_rows,
        ),
        quality=quality,
        pending_expires_at=(datetime.now() + timedelta(hours=settings.upload_pending_ttl_hours))
        .strftime(audit_service.TIME_FMT),
    )


async def _preview_rows(report: UploadedReport, columns: list[str]) -> list[dict[str, Any]]:
    """预览取数：`pending` 读规范化中间文件（只读前 N 行）；`ready` 读 `reports.db`。"""
    limit = get_settings().upload_preview_rows
    normalized = _normalized_path(report)
    if normalized.exists():
        frame = await asyncio.to_thread(pd.read_csv, normalized, nrows=limit)
        return [
            {key: (None if _is_na(value) else value) for key, value in record.items()}
            for record in frame.to_dict(orient="records")
        ]
    if not _NAME_RE.match(report.table_name):
        return []
    table = report.table_name
    async with report_ro_conn() as conn:
        exists = (
            await conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='table' AND name = :name"),
                {"name": table},
            )
        ).first()
        if not exists:
            return []
        order = "dt" if "dt" in columns else "rowid"
        rows = (
            await conn.execute(text(f"SELECT * FROM {table} ORDER BY {order} LIMIT {limit}"))
        ).mappings().all()
    return [dict(row) for row in rows]


# ── 确认入库（两段式第二步）────────────────────────────────────────────────

async def confirm_report(
    user_id: int,
    report_code: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> ConfirmOut:
    """把 `pending` 报表落成 `reports.db` 中的动态表（动态建表，PRD F1.3）。"""
    settings = get_settings()
    async with locks.report_write_lock():
        async with control_session() as session:
            report = await _fetch_own_report(session, user_id, report_code)
            if report.status == "ready":
                # 幂等：重复确认不重建表，直接回报当前状态
                return ConfirmOut(
                    report_code=report.report_code,
                    table_name=report.table_name,
                    status=report.status,
                    row_count=report.row_count or 0,
                    inserted_rows=0,
                    elapsed_ms=0,
                    intermediate_removed=not _normalized_path(report).exists(),
                    already_confirmed=True,
                )
            if report.status not in ("pending", "failed"):
                raise not_found(f"报表不存在：{report_code}")

            normalized = _normalized_path(report)
            if not normalized.exists():
                raise validation_error(
                    f"报表 {report_code} 的解析中间文件已丢失，请重新上传该文件后再确认"
                )

            table = report.table_name
            if not _NAME_RE.match(table):
                raise validation_error(f"非法表名：{table}")
            columns = await asyncio.to_thread(_read_normalized_header, normalized)
            types = _column_types(report, columns)

            started = time.perf_counter()
            try:
                # 建表 + 分批灌数 + 建索引整体放入线程（同步 sqlite3 独占写连接，决策 8）
                inserted, indexes = await asyncio.to_thread(
                    _ingest_sync, table, columns, types, normalized, settings.ingest_batch_rows
                )
            except Exception as exc:  # noqa: BLE001 —— 统一回滚并落失败态
                await _abort_ingest(report, table, exc, user_id=user_id)
                raise ingest_failed(str(exc)[:300]) from exc

            report.status = "ready"
            report.row_count = inserted
            report.column_count = len(columns)
            report.error_message = None
            report.updated_at = audit_service.now_text()
            await session.commit()

            normalized.unlink(missing_ok=True)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info("入库完成：%s → %s（%d 行，%s，%dms）", report_code, table, inserted,
                        "/".join(indexes) or "无索引", elapsed_ms)
            return ConfirmOut(
                report_code=report.report_code,
                table_name=table,
                status="ready",
                row_count=inserted,
                inserted_rows=inserted,
                indexes=indexes,
                elapsed_ms=elapsed_ms,
                intermediate_removed=True,
            )


def _read_normalized_header(path: Path) -> list[str]:
    """读中间文件表头（列顺序的权威来源，与 `_assemble` 写出顺序一致）。"""
    frame = pd.read_csv(path, nrows=0)
    columns = [str(column) for column in frame.columns]
    for column in columns:
        if not _NAME_RE.match(column):
            raise validation_error(f"中间文件包含非法列名：{column}")
    if not columns:
        raise validation_error("中间文件没有列")
    return columns


def _column_types(report: UploadedReport, columns: list[str]) -> dict[str, str]:
    """列类型取自 `schema_json`（解析阶段推断），缺省 TEXT。"""
    schema = json.loads(report.schema_json) if report.schema_json else {}
    known = {item["name"]: item.get("type") or "TEXT" for item in schema.get("columns") or []}
    return {column: known.get(column, "TEXT") for column in columns}


def _ingest_sync(
    table: str,
    columns: list[str],
    types: dict[str, str],
    path: Path,
    batch_rows: int,
) -> tuple[int, list[str]]:
    """**同步**完成建表 → 分批灌数 → 建索引（在线程池内调用，故全同步实现）。

    - 灌数**每批提交**（决策 8：把写锁持有时间从十几秒切成 <1s 的小段，
      让只读查询有机会插入）；
    - 分批提交牺牲原子性，失败时的补偿是 `DROP TABLE`（动态表本身可丢弃）。
    """
    definitions = ", ".join(f'"{column}" {types.get(column, "TEXT")}' for column in columns)
    column_sql = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" * len(columns))
    insert_sql = f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})'

    inserted = 0
    with report_write_conn() as conn:
        conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY AUTOINCREMENT, {definitions})')
        reader = pd.read_csv(path, chunksize=batch_rows)
        for chunk in reader:
            # 向量化 NA → None（float 列的 NaN 会被 sqlite3 绑定为 NULL，无需逐行判断）
            frame = chunk.astype(object).where(chunk.notna(), None)
            rows = list(frame.itertuples(index=False, name=None))
            if not rows:
                continue
            conn.executemany(insert_sql, rows)
            conn.commit()  # 分批提交
            conn.execute("BEGIN IMMEDIATE")  # 提交后立即重新取写锁
            inserted += len(rows)

        indexes = _create_indexes_sync(conn, table, columns)
    return inserted, indexes


def _create_indexes_sync(conn: sqlite3.Connection, table: str, columns: list[str]) -> list[str]:
    """建索引：时间列 + 派生维度列（按实际列存在情况生成，PRD 数据库设计 4.6）。"""
    created: list[str] = []
    for column in INDEXED_COLUMNS:
        if column not in columns:
            continue
        name = f"ix_{table}_{column}"
        if not _NAME_RE.match(name):
            continue
        conn.execute(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ("{column}")')
        created.append(name)
    return created


async def _abort_ingest(
    report: UploadedReport, table: str, exc: Exception, *, user_id: int
) -> None:
    """失败补偿：`DROP TABLE` 清理已灌入的部分，元数据落 `failed` 待重传/重试。"""
    logger.warning("入库失败，回滚动态表 %s：%s", table, exc)
    try:
        await asyncio.to_thread(_drop_table_sync, table)
    except Exception as rollback_exc:  # noqa: BLE001
        logger.error("回滚动态表 %s 失败：%s", table, rollback_exc)

    async with control_session() as session:
        row = await _fetch_own_report(session, user_id, report.report_code)
        row.status = "failed"
        row.error_message = str(exc)[:500]
        row.updated_at = audit_service.now_text()
        await session.commit()


def _drop_table_sync(table: str) -> tuple[str | None, list[str], int]:
    """同步 DROP 动态表，返回（被删表名, 被删索引, 回收页数）。

    **不做 VACUUM**（决策 2）：空闲页留给后续上传复用，接口以 `freed_pages` 如实报告。
    """
    if not _NAME_RE.match(table):
        raise validation_error(f"非法表名：{table}")
    dropped: str | None = None
    indexes: list[str] = []
    with report_write_conn() as conn:
        before = conn.execute("PRAGMA freelist_count").fetchone()[0]
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
        ).fetchone()
        if exists:
            indexes = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name = ? "
                    "AND name NOT LIKE 'sqlite_%'",
                    (table,),
                ).fetchall()
            ]
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            dropped = table
        freed = max(0, conn.execute("PRAGMA freelist_count").fetchone()[0] - before)
    return dropped, indexes, freed


# ── 删除（含预置保护）──────────────────────────────────────────────────────

async def delete_report(
    user_id: int,
    report_code: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> DeleteOut:
    """删除报表：预置 403；否则标记 → DROP TABLE → 删元数据行 → 删落盘目录。"""
    settings = get_settings()
    async with locks.report_write_lock():
        async with control_session() as session:
            report = await _fetch_own_visible(session, user_id, report_code)
            if report.is_preset:
                raise preset_not_deletable(report_code)
            if report.status == "deleted":
                raise not_found(f"报表不存在：{report_code}")

            report_id = report.id
            filename = report.filename
            table_name = report.table_name
            was_pending = report.status == "pending"
            affected = await session.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.active_report_id == report_id)
            ) or 0

            # ① 先标记 + 落审计（数据库设计 6.4：先元数据、后物理表，失败可补偿）
            report.status = "deleted"
            report.updated_at = audit_service.now_text()
            session.add(
                audit_service.build_audit(
                    "cascade_delete",
                    "medium",
                    "allowed",
                    user_id=user_id,
                    rule_id="report:delete",
                    payload=json.dumps(
                        {
                            "report_code": report_code,
                            "table_name": table_name,
                            "was_pending": was_pending,
                            "affected_conversations": int(affected),
                        },
                        ensure_ascii=False,
                    ),
                    ip=ip,
                    user_agent=user_agent,
                )
            )
            await session.commit()

            # ② 受控事务内 DROP TABLE（pending 报表的物理表还没建，跳过）
            dropped: str | None = None
            dropped_indexes: list[str] = []
            freed_pages = 0
            if not was_pending:
                try:
                    dropped, dropped_indexes, freed_pages = await asyncio.to_thread(
                        _drop_table_sync, table_name
                    )
                except Exception as exc:  # noqa: BLE001
                    report = await _fetch_own_report(session, user_id, report_code)
                    report.status = "failed"
                    report.error_message = f"动态表清理失败：{exc}"[:500]
                    report.updated_at = audit_service.now_text()
                    await session.commit()
                    raise ingest_failed(f"动态表清理失败：{exc}", rolled_back=False) from exc

            # ③ 删元数据行：触发 upload_files 级联删除与 conversations.active_report_id 置 NULL
            report = await _fetch_own_report(session, user_id, report_code)
            await session.delete(report)
            await session.commit()

        files_removed = _remove_report_dir(user_id, report_code)
        logger.info(
            "报表已删除：%s（DROP %s，回收页 %d，影响会话 %d）",
            report_code, dropped or "无物理表", freed_pages, affected,
        )
        return DeleteOut(
            report_code=report_code,
            filename=filename,
            table_name=table_name,
            dropped_table=dropped,
            dropped_indexes=dropped_indexes,
            affected_conversations=int(affected),
            freed_pages=freed_pages,
            files_removed=files_removed,
        )


def _remove_report_dir(user_id: int, report_code: str) -> bool:
    """删除报表落盘目录（原始文件 + 中间文件）；目录不存在返回 False。"""
    if not _NAME_RE.match(report_code):
        return False
    directory = get_settings().upload_dir / str(user_id) / report_code
    if not directory.exists():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


# ── 待确认上传的惰性回收（决策 6）───────────────────────────────────────────

async def cleanup_expired_pending() -> int:
    """删除超过 TTL（默认 24h）仍未确认的 `pending` 报表与落盘文件。

    调用时机：应用启动 + 每次 `upload` 前（不引入后台定时器）。
    调用方须已持有写锁（启动路径除外，此时无并发）。
    """
    settings = get_settings()
    cutoff = (
        datetime.now() - timedelta(hours=settings.upload_pending_ttl_hours)
    ).strftime(audit_service.TIME_FMT)
    async with control_session() as session:
        rows = (
            await session.scalars(
                select(UploadedReport).where(
                    UploadedReport.status == "pending",
                    UploadedReport.created_at < cutoff,
                )
            )
        ).all()
        for row in rows:
            _remove_report_dir(row.user_id or 0, row.report_code)
            await session.delete(row)
        if rows:
            await session.commit()
    if rows:
        logger.info("清理过期 pending 上传：%d 条", len(rows))
    return len(rows)


# ── 公共工具 ────────────────────────────────────────────────────────────────

async def _fetch_own_report(
    session: AsyncSession, user_id: int, report_code: str
) -> UploadedReport:
    """取本人上传的报表（含 `deleted`/`failed`/`pending`），预置或他人资源一律 403/404。"""
    report = await session.scalar(
        select(UploadedReport).where(UploadedReport.report_code == report_code)
    )
    if report is None:
        raise not_found(f"报表不存在：{report_code}")
    if report.is_preset:
        raise validation_error("预置报表不可修改，请选择自己上传的报表")
    if report.user_id != user_id:
        raise not_found(f"报表不存在：{report_code}")  # 不泄露他人资源是否存在
    return report


async def _fetch_own_visible(
    session: AsyncSession, user_id: int, report_code: str
) -> UploadedReport:
    """取本人上传的未删除报表（删除入口用；预置报表允许取出，由调用方判定 403）。"""
    report = await session.scalar(
        select(UploadedReport).where(UploadedReport.report_code == report_code)
    )
    if report is None or report.status == "deleted":
        raise not_found(f"报表不存在：{report_code}")
    if not report.is_preset and report.user_id != user_id:
        raise not_found(f"报表不存在：{report_code}")
    return report


def _normalized_path(report: UploadedReport) -> Path:
    return (
        get_settings().upload_dir
        / str(report.user_id or 0)
        / report.report_code
        / NORMALIZED_FILENAME
    )


def _data_relative(path: Path) -> str:
    """转为相对 `02.data/` 的路径（PRD S5：入库不存绝对路径）。"""
    from app.core.config import DATA_DIR

    try:
        return path.relative_to(DATA_DIR).as_posix()
    except ValueError:  # 落在 02.data 之外（测试用临时目录）→ 退回文件名
        return path.name


def _is_na(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
