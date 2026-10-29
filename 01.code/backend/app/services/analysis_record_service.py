"""智电析报 · 分析记录服务（PRD 9.1 四个记录接口；UIUX 5.4 区域 3）。

| 接口 | 说明 |
| --- | --- |
| `GET /analysis/records` | 列表：**offset 分页 + total** + 组合筛选（决策 1） |
| `GET /analysis/records/{record_code}` | 详情：全量 + `llm_calls` 明细（决策 3） |
| `POST /analysis/records/{record_code}/replay` | 重跑：**整轮重跑 + 新建记录 + 锁定原报表**（决策 2） |
| `DELETE /analysis/records/{record_code}` | 删除：硬删记录（`llm_calls` 级联、`messages.analysis_id` 置 NULL） |

归属与可见性：全部按 `user_id` 过滤，他人记录一律 404（不泄露资源是否存在）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import not_found, validation_error
from app.db.models import Analysis, Conversation, LLMCall, Message, UploadedReport
from app.db.session import control_session
from app.schemas.analysis_record import (
    LlmCallOut,
    RecordDeleteOut,
    RecordDetailOut,
    RecordItem,
    RecordListOut,
    ResultOut,
)

#: 评测批次会话编号前缀（与 `eval_service.EVAL_CONVERSATION_PREFIX` 同一约定；
#: 在此处重复一份是为了让"重跑"这个读路径不依赖评测模块的导入开销）
EVAL_CONVERSATION_PREFIX = "c_eval_"

logger = logging.getLogger("zdxb.records")

__all__ = ["list_records", "get_record", "delete_record", "replay_record"]

#: 列表分页约束（PRD 9.1 未指定，按 N7 的结果保护思路设上限）
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

#: 状态的合法取值（与 `analyses.status` 的 CHECK 一致）
_STATUSES = ("success", "failed")


# ── 列表 ────────────────────────────────────────────────────────────────────

async def list_records(
    user_id: int,
    *,
    conversation_code: str | None = None,
    report_code: str | None = None,
    status: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> RecordListOut:
    """分析记录列表（决策 1：offset 分页 + total + 组合筛选 + 稳定排序）。"""
    if page < 1:
        raise validation_error("page 从 1 开始")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise validation_error(f"page_size 取值范围为 1–{MAX_PAGE_SIZE}")
    if status and status not in _STATUSES:
        raise validation_error(f"status 只能是 {' / '.join(_STATUSES)}")

    async with control_session() as session:
        conditions = [Analysis.user_id == user_id]
        if conversation_code:
            conversation_id = await session.scalar(
                select(Conversation.id).where(Conversation.conversation_code == conversation_code)
            )
            if conversation_id is None:
                raise not_found(f"会话不存在：{conversation_code}")
            conditions.append(Analysis.conversation_id == conversation_id)
        if report_code:
            report_id = await session.scalar(
                select(UploadedReport.id).where(UploadedReport.report_code == report_code)
            )
            if report_id is None:
                raise not_found(f"报表不存在：{report_code}")
            conditions.append(Analysis.report_id == report_id)
        if q:
            conditions.append(Analysis.question.like(f"%{q}%"))
        if date_from:
            conditions.append(Analysis.created_at >= date_from)
        if date_to:
            # `created_at` 为 'YYYY-MM-DD HH:MM:SS'，按日筛选需包含当天全天
            conditions.append(Analysis.created_at <= f"{date_to} 23:59:59")

        # 状态条件**单独持有**，用于页签计数（见下）
        conditions_without_status = list(conditions)
        if status:
            conditions.append(Analysis.status == status)

        total = await session.scalar(
            select(func.count()).select_from(Analysis).where(*conditions)
        ) or 0
        # 状态分布不含状态条件本身：前端用「全部 / 成功 / 失败」做页签时，
        # 切换页签不应改变各页签上的数字（其余筛选条件仍然生效）
        status_rows = (
            await session.execute(
                select(Analysis.status, func.count())
                .where(*conditions_without_status)
                .group_by(Analysis.status)
            )
        ).all()

        rows = (
            await session.execute(
                select(Analysis)
                .where(*conditions)
                # 稳定排序：同一秒内也用 id 兜底，避免翻页错位
                .order_by(Analysis.created_at.desc(), Analysis.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        ).scalars().all()

        items = await _to_items(session, rows)

    return RecordListOut(
        total=int(total),
        page=page,
        page_size=page_size,
        status_count={row[0]: int(row[1]) for row in status_rows},
        items=items,
    )


async def _to_items(session: AsyncSession, rows: list[Analysis]) -> list[RecordItem]:
    """把 ORM 行转成列表项：批量补齐会话编号、报表编号/文件名与各记录的 token 合计。"""
    if not rows:
        return []
    conversation_ids = {row.conversation_id for row in rows if row.conversation_id}
    report_ids = {row.report_id for row in rows if row.report_id}
    record_ids = [row.id for row in rows]

    conversation_map: dict[int, str] = {}
    if conversation_ids:
        conversation_map = dict(
            (
                await session.execute(
                    select(Conversation.id, Conversation.conversation_code).where(
                        Conversation.id.in_(conversation_ids)
                    )
                )
            ).all()
        )
    report_map: dict[int, tuple[str, str]] = {}
    if report_ids:
        report_map = {
            row[0]: (row[1], row[2])
            for row in (
                await session.execute(
                    select(UploadedReport.id, UploadedReport.report_code, UploadedReport.filename)
                    .where(UploadedReport.id.in_(report_ids))
                )
            ).all()
        }
    token_map: dict[int, int] = {}
    if record_ids:
        token_map = {
            row[0]: int(row[1] or 0)
            for row in (
                await session.execute(
                    select(LLMCall.analysis_id, func.sum(LLMCall.total_tokens))
                    .where(LLMCall.analysis_id.in_(record_ids))
                    .group_by(LLMCall.analysis_id)
                )
            ).all()
        }

    items: list[RecordItem] = []
    for row in rows:
        report = report_map.get(row.report_id or 0)
        items.append(
            RecordItem(
                record_code=row.record_code,
                question=row.question,
                conversation_code=conversation_map.get(row.conversation_id or 0),
                report_code=report[0] if report else None,
                report_filename=report[1] if report else None,
                route=row.route,
                status=row.status,
                error_code=row.error_code,
                elapsed_ms=row.elapsed_ms,
                llm_tokens=token_map.get(row.id),
                llm_model=row.llm_model,
                created_at=row.created_at,
            )
        )
    return items


# ── 详情 ────────────────────────────────────────────────────────────────────

async def get_record(user_id: int, record_code: str) -> RecordDetailOut:
    """记录详情（决策 3：全量 + `llm_calls` 明细）。"""
    async with control_session() as session:
        analysis = await _fetch_own(session, user_id, record_code)
        detail = await _build_detail(session, analysis)
    return detail


async def _build_detail(
    session: AsyncSession, analysis: Analysis, *, replayed_from: str | None = None
) -> RecordDetailOut:
    report = (
        await session.get(UploadedReport, analysis.report_id)
        if analysis.report_id
        else None
    )
    conversation = (
        await session.get(Conversation, analysis.conversation_id)
        if analysis.conversation_id
        else None
    )
    calls = (
        await session.scalars(
            select(LLMCall).where(LLMCall.analysis_id == analysis.id).order_by(LLMCall.id)
        )
    ).all()

    return RecordDetailOut(
        record_code=analysis.record_code,
        question=analysis.question,
        conversation_code=conversation.conversation_code if conversation else None,
        report_code=report.report_code if report else None,
        report_filename=report.filename if report else None,
        route=analysis.route,
        status=analysis.status,
        error_code=analysis.error_code,
        error_message=analysis.error_message,
        llm_model=analysis.llm_model,
        elapsed_ms=analysis.elapsed_ms,
        prompt_version=analysis.prompt_version,
        schema_version=analysis.schema_version,
        retries=int(analysis.retries or 0),
        path_switched=bool(analysis.path_switched),
        created_at=analysis.created_at,
        slot=_loads(analysis.slot_json, {}) or {},
        sql=analysis.sql_text,
        code_text=analysis.code_text,
        result=ResultOut(
            columns=_loads(analysis.result_columns_json, []) or [],
            rows=_loads(analysis.result_rows_json, []) or [],
            row_count=analysis.result_row_count,
            truncated=bool(analysis.result_truncated),
        ),
        narrative=analysis.narrative,
        key_numbers=_loads(analysis.key_numbers_json, []) or [],
        anomalies=_loads(analysis.anomalies_json, []) or [],
        chart=_loads(analysis.chart_json, None),
        llm_calls=[
            LlmCallOut(
                stage=call.stage,
                model_name=call.model_name,
                prompt_tokens=call.prompt_tokens,
                completion_tokens=call.completion_tokens,
                total_tokens=call.total_tokens,
                elapsed_ms=call.elapsed_ms,
                status=call.status,
            )
            for call in calls
        ],
        llm_tokens=sum(call.total_tokens or 0 for call in calls),
        can_replay=bool(report and report.status == "ready"),
        replayed_from=replayed_from,
    )


# ── 重跑 ────────────────────────────────────────────────────────────────────

async def replay_record(
    user_id: int,
    record_code: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> RecordDetailOut:
    """重跑（决策 2）：原问题整轮重跑，**报表锁定为原记录的报表**，落一条新记录。

    - 不跟随会话当前活跃报表（否则结果不可比）→ 调用 `run_once(..., switch_active_report=False)`；
    - 原报表已删除/不可用 → 400 `E_VALIDATION`（提示无法重跑）；
    - 非流式（PRD 9.1 把 replay 标注为"否"），直接返回新记录的详情。
    """
    from app.agent import dispatch  # 延迟导入：与 run_once 的循环引用同理
    from app.services import analysis_service  # 延迟导入：避免与 run_once 的循环引用

    async with control_session() as session:
        origin = await _fetch_own(session, user_id, record_code)
        conversation = (
            await session.get(Conversation, origin.conversation_id)
            if origin.conversation_id
            else None
        )
        report_code = None
        if origin.report_id:
            report = await session.get(UploadedReport, origin.report_id)
            if report is None or report.status != "ready":
                raise validation_error(
                    "原报表已被删除或不可用，无法重跑；请重新上传数据后提问"
                )
            report_code = report.report_code
        if conversation is None:
            raise validation_error("原会话已被删除，无法重跑")
        question = origin.question
        origin_conversation_code = conversation.conversation_code
        # 决策 19-4：重跑的 slot 基线 = **原记录的 slot_json**（增量抽取照常跑，保住 PRD
        # 「整轮重跑」），会话后续追问改过的条件不污染「并排比较」；传 {} 表示显式不继承
        slot_base = json.loads(origin.slot_json) if origin.slot_json else {}

    # **只读会话的重跑去向（v1.19.12）**：评测批次会话（编号 `c_eval_*`）是系统生成、只用于
    # 留痕的会话，用户不会（也不应）在里面提问；把重跑结果写回去会让批次会话多出提问与回答，
    # 污染留痕。这类记录的重跑**另起一个新会话**承载，并在回显里说明去向。
    target_conversation = origin_conversation_code
    replay_note: str | None = None
    if origin_conversation_code.startswith(EVAL_CONVERSATION_PREFIX):
        from app.schemas.conversation import ConversationCreateIn  # 局部导入：避免顶层循环引用
        from app.services import conversation_service

        created = await conversation_service.create_conversation(
            user_id,
            ConversationCreateIn(
                title=f"重跑：{question[:36]}", active_report_code=report_code
            ),
        )
        target_conversation = created.conversation_code
        replay_note = (
            f"原记录属于评测批次会话（只读留痕），本次重跑已在新会话 {target_conversation} 中执行，"
            "不会污染评测批次"
        )
        logger.info(
            "重跑目的地为只读评测会话，改写入新会话：%s → %s",
            origin_conversation_code, target_conversation,
        )

    result = await dispatch.run_once_dispatch(
        user_id,
        target_conversation,
        question,
        report_code=report_code,
        switch_active_report=False,  # 锁定原报表，不改会话活跃报表
        slot_base=slot_base,
        ip=ip,
        user_agent=user_agent,
        request_id=request_id,
    )
    async with control_session() as session:
        new_analysis = await _fetch_own(session, user_id, result.record_code)
        detail = await _build_detail(session, new_analysis, replayed_from=record_code)
    detail.replay_note = replay_note
    logger.info("重跑完成：%s → %s（status=%s）", record_code, result.record_code, result.status)
    return detail


# ── 删除 ────────────────────────────────────────────────────────────────────

async def delete_record(user_id: int, record_code: str) -> RecordDeleteOut:
    """删除分析记录：硬删本行（`llm_calls` 级联删除、`messages.analysis_id` 置 NULL）。"""
    async with control_session() as session:
        analysis = await _fetch_own(session, user_id, record_code)
        call_count = await session.scalar(
            select(func.count()).select_from(LLMCall).where(LLMCall.analysis_id == analysis.id)
        ) or 0
        # 保留对话文本：把关联消息的分析记录引用置空（数据库设计 6.2 的既定行为）
        linked = (
            await session.scalars(select(Message).where(Message.analysis_id == analysis.id))
        ).all()
        for message in linked:
            message.analysis_id = None
        await session.delete(analysis)
        await session.commit()

    return RecordDeleteOut(
        record_code=record_code,
        deleted_messages=len(linked),
        deleted_llm_calls=int(call_count),
    )


# ── 公共 ────────────────────────────────────────────────────────────────────

async def _fetch_own(session: AsyncSession, user_id: int, record_code: str) -> Analysis:
    """按 `record_code` 取本人的分析记录；不存在或不属于本人一律 404。"""
    analysis = await session.scalar(
        select(Analysis).where(Analysis.record_code == record_code)
    )
    if analysis is None:
        raise not_found(f"分析记录不存在：{record_code}")
    if analysis.user_id != user_id:
        # 不泄露他人资源是否存在（与报表/会话接口一致的策略）
        raise not_found(f"分析记录不存在：{record_code}")
    return analysis


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("分析记录字段不是合法 JSON，已忽略：%s", raw[:80])
        return default
