"""会话服务：新建 / 列表 / 详情 / 修改（重命名、置顶、切换活跃报表）/ 删除 / 消息历史。

对应：PRD 9.1 会话模块、F6.3（会话与记录管理）、F6.4（数据隔离与删除）、UIUX 5.1（左栏会话列表）。

关键约定：
- 活跃报表的唯一权威是 `conversations.active_report_id`（决策）；
- 置顶每用户最多 5 条，超出自动挤出「最早置顶」的那条（决策）；
- 会话列表**不分页**（与报表列表一致），支持 `?q=` 与时间范围筛选，置顶组在前。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import checkpointer
from app.core.errors import forbidden, not_found, validation_error
from app.db.models import Analysis, Conversation, Message, UploadedReport
from app.db.session import control_session
from app.schemas.conversation import (
    ActivateReportOut,
    ConversationCreateIn,
    ConversationCreateOut,
    ConversationDetailOut,
    ConversationListOut,
    ConversationOut,
    ConversationPatchIn,
    ConversationPatchOut,
    MessageListOut,
    MessageOut,
)
from app.services.audit_service import build_audit, now_text

#: 会话置顶上限（UIUX 5.1：置顶分组最多 5 条）
MAX_PINNED = 5

#: 消息历史默认条数与上限（决策：游标式分页）
MESSAGE_DEFAULT_LIMIT = 50
MESSAGE_MAX_LIMIT = 200

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


# ── 内部工具 ────────────────────────────────────────────────────────────────

async def _owned_conversation(
    session: AsyncSession, user_id: int, conversation_code: str
) -> Conversation:
    """取会话并校验归属：不存在 → 404；属于他人 → 403（不泄露资源是否存在）。"""
    conversation = await session.scalar(
        select(Conversation).where(Conversation.conversation_code == conversation_code)
    )
    if conversation is None:
        raise not_found(f"会话不存在：{conversation_code}")
    if conversation.user_id != user_id:
        raise forbidden()
    return conversation


async def _resolve_report_by_code(
    session: AsyncSession, user_id: int, report_code: str
) -> UploadedReport:
    """按 `report_code` 取报表（预置共享 / 本人上传），不可见则 403。"""
    report = await session.scalar(
        select(UploadedReport).where(UploadedReport.report_code == report_code)
    )
    if report is None or report.status == "deleted":
        raise not_found(f"报表不存在：{report_code}")
    if not report.is_preset and report.user_id != user_id:
        raise forbidden()
    return report


async def _resolve_active_report(
    session: AsyncSession, user_id: int, explicit_code: str | None
) -> tuple[UploadedReport | None, str]:
    """三级回落（决策）：显式 > 该用户最近会话 > 系统默认活跃报表。"""
    if explicit_code:
        return await _resolve_report_by_code(session, user_id, explicit_code), "explicit"

    latest = await session.scalar(
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .where(Conversation.active_report_id.is_not(None))
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(1)
    )
    if latest is not None:
        report = await session.get(UploadedReport, latest.active_report_id)
        if report is not None and report.status != "deleted":
            return report, "last_conversation"

    system_default = await session.scalar(
        select(UploadedReport)
        .where(UploadedReport.is_preset == 1)
        .where(UploadedReport.is_active == 1)
        .limit(1)
    )
    return system_default, "system_default"


async def _next_conversation_code(session: AsyncSession, now: datetime) -> str:
    """生成 `c_YYYYMMDD_NNNN`：取当日最大序号 +1。"""
    prefix = f"c_{now.strftime('%Y%m%d')}_"
    latest = await session.scalar(
        select(Conversation.conversation_code)
        .where(Conversation.conversation_code.like(f"{prefix}%"))
        .order_by(Conversation.conversation_code.desc())
        .limit(1)
    )
    seq = 1
    if latest:
        try:
            seq = int(latest.rsplit("_", 1)[1]) + 1
        except (IndexError, ValueError):
            seq = 1
    return f"{prefix}{seq:04d}"


async def _report_code_map(
    session: AsyncSession, report_ids: set[int | None]
) -> dict[int, str]:
    """批量解析 `active_report_id` → `report_code`（避免逐条查询）。"""
    ids = {rid for rid in report_ids if rid is not None}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(UploadedReport.id, UploadedReport.report_code).where(UploadedReport.id.in_(ids))
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _message_counts(session: AsyncSession, conversation_ids: list[int]) -> dict[int, int]:
    """一次分组 COUNT 取回各会话的消息数（列表接口用，实时且不跑 N+1）。"""
    if not conversation_ids:
        return {}
    rows = (
        await session.execute(
            select(Message.conversation_id, func.count(Message.id))
            .where(Message.conversation_id.in_(conversation_ids))
            .group_by(Message.conversation_id)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


def _to_out(
    conversation: Conversation,
    *,
    report_codes: dict[int, str],
    message_count: int,
) -> ConversationOut:
    item = ConversationOut.model_validate(conversation)
    item.active_report_code = report_codes.get(conversation.active_report_id or -1)
    item.message_count = message_count
    return item


# ── 新建 ────────────────────────────────────────────────────────────────────

async def create_conversation(
    user_id: int, payload: ConversationCreateIn
) -> ConversationCreateOut:
    async with control_session() as session:
        report, source = await _resolve_active_report(session, user_id, payload.active_report_code)
        timestamp = now_text()
        # 先把报表取值**拷成普通标量**再进重试循环：循环里 `rollback()` 会让 ORM 对象过期，
        # 之后再访问 `report.id` 会触发一次懒加载——在 async 会话里这就是
        # `MissingGreenlet`（真机上表现为"新建会话偶发 500"）。
        report_id = report.id if report is not None else None
        report_code_value = report.report_code if report is not None else None

        for _attempt in range(3):  # 编号冲突时重试（同进程演示场景足够）
            code = await _next_conversation_code(session, datetime.now())
            conversation = Conversation(
                conversation_code=code,
                user_id=user_id,
                title=payload.title,
                active_report_id=report_id,
                slot_json=None,
                message_count=0,
                is_pinned=0,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(conversation)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                continue
            break
        else:  # pragma: no cover - 极端并发下才会走到
            raise validation_error("会话编号生成失败，请重试")

        # 显式构造：`active_report_source` 无法从 ORM 对象推导（必须由服务层给出）
        base = ConversationOut.model_validate(conversation)
        base.active_report_code = report_code_value
        base.message_count = 0
        await session.commit()
        return ConversationCreateOut(**base.model_dump(), active_report_source=source)


# ── 列表 / 详情 ─────────────────────────────────────────────────────────────

async def list_conversations(
    user_id: int,
    *,
    keyword: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ConversationListOut:
    """会话列表：置顶组在前（`pinned_at DESC`），其余按 `updated_at DESC`。

    支持关键词（标题模糊）与时间范围（`updated_at`）筛选（PRD F6.3）。
    """
    conditions = [Conversation.user_id == user_id]
    if keyword:
        conditions.append(Conversation.title.like(f"%{keyword}%"))
    if date_from:
        conditions.append(Conversation.updated_at >= date_from)
    if date_to:
        conditions.append(Conversation.updated_at <= f"{date_to} 23:59:59")

    async with control_session() as session:
        conversations = (await session.scalars(select(Conversation).where(and_(*conditions)))).all()
        counts = await _message_counts(session, [c.id for c in conversations])
        codes = await _report_code_map(session, {c.active_report_id for c in conversations})

    pinned = sorted(
        (c for c in conversations if c.is_pinned),
        key=lambda c: (c.pinned_at or "", c.updated_at),
        reverse=True,
    )
    others = sorted(
        (c for c in conversations if not c.is_pinned),
        key=lambda c: (c.updated_at, c.id),
        reverse=True,
    )
    items = [
        _to_out(c, report_codes=codes, message_count=counts.get(c.id, 0))
        for c in (*pinned, *others)
    ]
    return ConversationListOut(total=len(items), items=items)


async def get_conversation(user_id: int, conversation_code: str) -> ConversationDetailOut:
    async with control_session() as session:
        conversation = await _owned_conversation(session, user_id, conversation_code)
        counts = await _message_counts(session, [conversation.id])
        codes = await _report_code_map(session, {conversation.active_report_id})

        detail = ConversationDetailOut.model_validate(conversation)
        detail.active_report_code = codes.get(conversation.active_report_id or -1)
        detail.message_count = counts.get(conversation.id, 0)
        detail.slot = json.loads(conversation.slot_json) if conversation.slot_json else {}
        return detail


# ── 修改（重命名 / 置顶 / 切换活跃报表）─────────────────────────────────────

async def _apply_active_report(
    session: AsyncSession, conversation: Conversation, user_id: int, report_code: str
) -> None:
    """把会话的活跃报表切到指定报表（`activate` 与 `PATCH` 共用，决策）。

    切换即**清空会话 slot**（决策 18-6）：旧表的 metric/dimension/filters 对新表可能根本
    不存在（跨表脏 slot 会诱导模型编造列名），且不维护「哪些字段跨表通用」的白名单；
    前端提示「已切换到 …，条件已重置」。
    """
    report = await _resolve_report_by_code(session, user_id, report_code)
    conversation.active_report_id = report.id
    conversation.slot_json = None
    conversation.updated_at = now_text()


async def _evict_extra_pinned(session: AsyncSession, user_id: int) -> str | None:
    """置顶超过上限时，取消「最早置顶」的一条；返回被挤出的会话编号。"""
    pinned = (
        await session.scalars(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .where(Conversation.is_pinned == 1)
            .order_by(Conversation.pinned_at.asc(), Conversation.id.asc())
        )
    ).all()
    evicted: str | None = None
    while len(pinned) > MAX_PINNED:
        oldest = pinned.pop(0)
        oldest.is_pinned = 0
        oldest.pinned_at = None
        evicted = oldest.conversation_code
    return evicted


async def patch_conversation(
    user_id: int, conversation_code: str, payload: ConversationPatchIn
) -> ConversationPatchOut:
    """重命名 / 置顶 / 切换活跃报表（三者可单独或组合提交）。"""
    if payload.title is None and payload.active_report_code is None and payload.is_pinned is None:
        raise validation_error("至少需要指定 title、active_report_code、is_pinned 之一")

    async with control_session() as session:
        conversation = await _owned_conversation(session, user_id, conversation_code)
        timestamp = now_text()

        if payload.title is not None:
            title = payload.title.strip()
            if not title:
                raise validation_error("标题不能为空")
            conversation.title = title

        if payload.active_report_code is not None:
            await _apply_active_report(session, conversation, user_id, payload.active_report_code)

        evicted: str | None = None
        if payload.is_pinned is not None:
            conversation.is_pinned = 1 if payload.is_pinned else 0
            conversation.pinned_at = timestamp if payload.is_pinned else None
            if payload.is_pinned:
                await session.flush()
                evicted = await _evict_extra_pinned(session, user_id)

        conversation.updated_at = timestamp
        await session.flush()

        counts = await _message_counts(session, [conversation.id])
        codes = await _report_code_map(session, {conversation.active_report_id})
        result = ConversationPatchOut.model_validate(conversation)
        result.active_report_code = codes.get(conversation.active_report_id or -1)
        result.message_count = counts.get(conversation.id, 0)
        result.evicted_pinned_code = evicted
        await session.commit()
        return result


async def set_active_report(
    user_id: int,
    conversation_code: str,
    report_code: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> ActivateReportOut:
    """报表视角的活跃报表切换（`POST /reports/{report_code}/activate`）。"""
    async with control_session() as session:
        conversation = await _owned_conversation(session, user_id, conversation_code)
        await _apply_active_report(session, conversation, user_id, report_code)
        await session.commit()
    return ActivateReportOut(
        conversation_code=conversation_code, active_report_code=report_code
    )


# ── 删除 ────────────────────────────────────────────────────────────────────

async def delete_conversation(
    user_id: int,
    conversation_code: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> dict[str, int]:
    """删除会话（消息与分析记录由外键级联清理），并落一条 `cascade_delete` 审计。"""
    async with control_session() as session:
        conversation = await _owned_conversation(session, user_id, conversation_code)
        message_total = (
            await session.execute(
                select(func.count(Message.id)).where(Message.conversation_id == conversation.id)
            )
        ).scalar() or 0

        session.add(
            build_audit(
                "cascade_delete",
                "low",
                "allowed",
                user_id=user_id,
                conversation_id=conversation.id,
                rule_id="delete_conversation",
                payload=f"conversation_code={conversation_code}, messages={message_total}",
                ip=ip,
                user_agent=user_agent,
            )
        )
        await session.execute(
            delete(Conversation).where(Conversation.id == conversation.id)
        )
        # 检查点级联（阶段 3）：会话没了，它的图状态（thread_id = 会话编号）也不该留着。
        # 在**同一事务内**删除，避免另开连接撞 database is locked。
        await checkpointer.delete_threads_in_session(session, [conversation_code])
        await session.commit()
    return {"deleted_messages": int(message_total)}


# ── 消息历史 ────────────────────────────────────────────────────────────────

async def list_messages(
    user_id: int,
    conversation_code: str,
    *,
    limit: int | None = None,
    before_seq: int | None = None,
) -> MessageListOut:
    """游标式消息历史：默认最近 50 条；`before_seq` 取更早的消息（决策）。"""
    effective = MESSAGE_DEFAULT_LIMIT if limit is None else limit
    if effective < 1 or effective > MESSAGE_MAX_LIMIT:
        raise validation_error(f"limit 取值范围为 1–{MESSAGE_MAX_LIMIT}，当前为 {effective}")

    async with control_session() as session:
        conversation = await _owned_conversation(session, user_id, conversation_code)

        query = select(Message).where(Message.conversation_id == conversation.id)
        if before_seq is not None:
            query = query.where(Message.seq_no < before_seq)
        # 取“最近 N 条”用 seq_no DESC，返回时再正序（符合对话阅读顺序）
        rows = (
            await session.scalars(query.order_by(Message.seq_no.desc()).limit(effective + 1))
        ).all()

    has_more = len(rows) > effective
    page = sorted(rows[:effective], key=lambda m: m.seq_no)

    # `analysis_id`（数字主键）→ `record_code`（业务编号）：前端刷新后据此还原表格与图表
    analysis_ids = [m.analysis_id for m in page if m.analysis_id]
    code_by_id: dict[int, str] = {}
    if analysis_ids:
        async with control_session() as session:
            pairs = await session.execute(
                select(Analysis.id, Analysis.record_code).where(Analysis.id.in_(analysis_ids))
            )
            code_by_id = {int(row[0]): str(row[1]) for row in pairs.all()}

    return MessageListOut(
        conversation_code=conversation_code,
        items=[
            MessageOut.model_validate(m).model_copy(
                update={"record_code": code_by_id.get(m.analysis_id or 0)}
            )
            for m in page
        ],
        limit=effective,
        has_more=has_more,
        oldest_seq=page[0].seq_no if page else None,
        newest_seq=page[-1].seq_no if page else None,
    )
