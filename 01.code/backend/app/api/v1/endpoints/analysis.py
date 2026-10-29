"""分析接口（PRD 9.1 / 9.2，交付物 4 第一批）。

`POST /analysis/chat`：**SSE 流式**输出对话分析结果。

实现方式（本轮决策 3；**v1.19.11 改为真流式**）：业务逻辑全部在 `analysis_service.run_once()` 里，
事件在**产生处**经 `EventSink` 即刻回调到本端点的队列，端点边收边推 —— 首字在毫秒级
（链路第一个事件 `status(parsing)` 在任何 await 之前就产生），满足 PRD N1/G3「首字 ≤2s」。
**此前实现是"等链路跑完再回放 `result.events`"**，首字要等到 6–9s 之后，实测不达标。
好处仍是**测试可以直接断言 service 的返回值**（JSON 级），SSE 帧格式只需少量用例覆盖。

事件顺序（成功）：`status(parsing)` → `slot` → `code` → `status(executing)` → `table`
→ `chart` → `narrative` → `meta` → `done`；失败时以 `error` 收尾（**不编造**：不发 narrative/table）。

`chart`（交付物 4 第三批）：载荷为 `{"figure": {...Plotly figure JSON} | null, "chart_type", "fallback",
"reason", "unit", "top_n", "merged_count", ...}`。**画不出来时 `figure=null` + `fallback="table"` +
`reason`**，`status` 仍为 `success` —— 图表是增值项，不阻断主流程。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from app.agent import dispatch
from app.api.v1.deps import CurrentUser, client_ip, client_user_agent, request_id
from app.core.errors import BizError
from app.schemas.analysis import ChatIn, ClarifyIn
from app.schemas.common import ok
from app.services import analysis_record_service, analysis_service

logger = logging.getLogger("zdxb.analysis")

router = APIRouter(tags=["analysis"])


def _error_frame(exc: BizError) -> dict[str, str]:
    return {
        "event": "error",
        "data": json.dumps(
            {"code": exc.error_code, "message": exc.message, "detail": exc.data},
            ensure_ascii=False,
        ),
    }


async def _stream_run(
    runner: Callable[[analysis_service.EventPush], Awaitable[Any]],
) -> AsyncIterator[dict[str, str]]:
    """把一次分析链路**边跑边推**成 SSE 帧（chat 与 clarify 共用，真流式 v1.19.11）。

    机制：链路放进 `asyncio.Task` 并发跑，事件由 `EventSink` 回调用同步 `put_nowait` 入队，
    本生成器随即取出成帧 yield —— 因此**首帧（`status: parsing`）在毫秒级**送达，而不是等链路结束。

    收尾三种情形：正常 `end`、`BizError` → `error` 帧（业务失败如实回报）、
    未知异常 → `E_INTERNAL` 帧（**不让前端干等**）。客户端断开时**取消链路**（少烧 token 与算力）。
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def push(item: dict[str, Any]) -> None:
        queue.put_nowait(("event", item))

    async def drive() -> None:
        try:
            await runner(push)
        except BizError as exc:
            queue.put_nowait(("biz_error", exc))
        except Exception as exc:  # noqa: BLE001 —— 未知异常也必须收尾，避免前端一直挂着
            logger.exception("分析链路异常（SSE 将以 E_INTERNAL 收尾）")
            queue.put_nowait(("fatal", exc))
        finally:
            queue.put_nowait(("end", None))

    task = asyncio.create_task(drive())
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "end":
                break
            if kind == "event":
                yield {
                    "event": payload["event"],
                    "data": json.dumps(_jsonable(payload["data"]), ensure_ascii=False, default=str),
                }
            elif kind == "biz_error":
                yield _error_frame(payload)
            else:  # fatal
                yield {
                    "event": "error",
                    "data": json.dumps(
                        {
                            "code": "E_INTERNAL",
                            "message": "服务端异常，请稍后重试",
                            "detail": {"type": type(payload).__name__},
                        },
                        ensure_ascii=False,
                    ),
                }
    finally:
        # 客户端断开（用户离开页面/点了停止）时链路可能还在跑：取消它，别继续烧 token
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


@router.post("/analysis/chat", summary="对话分析（SSE 流式返回）")
async def chat(payload: ChatIn, request: Request, user: CurrentUser) -> EventSourceResponse:
    """对话分析（SSE）。请求体见 PRD 9.2：`conversation_code` / `report_code?` / `message`
    / `slot_patch?`（条件芯片编辑后的回填，决策 20-1）。

    多轮 slot 继承（决策 18-4）：以会话 `slot_json` 为基线做**增量抽取**合并；当触发澄清
    （决策 18-1）时以 `clarify` 事件收尾（`done.record_id=null`），前端回填后调
    `POST /analysis/clarify` 继续执行。
    """
    rid = request_id(request)

    async def runner(push: analysis_service.EventPush) -> Any:
        # 上下文级失败（会话/报表不存在、参数非法）：由 `_stream_run` 统一转成 error 事件收尾，
        # 避免前端拿到一个"空流"而无法提示原因
        return await dispatch.run_once_dispatch(
            user.id,
            payload.conversation_code,
            payload.message,
            report_code=payload.report_code,
            slot_patch=payload.slot_patch,
            ip=client_ip(request),
            user_agent=client_user_agent(request),
            request_id=rid,
            emit=push,
        )

    headers: dict[str, Any] = {"X-Request-ID": rid, "Cache-Control": "no-cache"}
    return EventSourceResponse(_stream_run(runner), headers=headers)


@router.post("/analysis/clarify", summary="澄清回填后就地继续执行（SSE）")
async def clarify(payload: ClarifyIn, request: Request, user: CurrentUser) -> EventSourceResponse:
    """澄清回填后就地继续执行（PRD F2.6 / F4.4，返回 **SSE**）。

    - 请求体 `{conversation_code, question, slot_patch}`：回填值先合入会话 `slot_json`
      并持久化（用户点选不丢），再以合并后的完整 slot 走完整链路（**跳过二次增量抽取**）；
    - 事件序列与 chat 相同；若回填后仍缺关键条件会再次收到 `clarify`（自然行为）；
    - 归属隔离：会话不存在 / 不属于本人 → error 事件 `E_NOT_FOUND`。
    """
    rid = request_id(request)

    async def runner(push: analysis_service.EventPush) -> Any:
        return await dispatch.run_clarify_dispatch(
            user.id,
            payload.conversation_code,
            payload.question,
            slot_patch=payload.slot_patch,
            ip=client_ip(request),
            user_agent=client_user_agent(request),
            request_id=rid,
            emit=push,
        )

    headers: dict[str, Any] = {"X-Request-ID": rid, "Cache-Control": "no-cache"}
    return EventSourceResponse(_stream_run(runner), headers=headers)


def _jsonable(payload: Any) -> Any:
    """SSE 事件载荷统一为可序列化结构（表格里的 datetime/Decimal 走 default=str）。"""
    if isinstance(payload, dict):
        return {key: _jsonable(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_jsonable(item) for item in payload]
    return payload


# ── 分析记录（PRD 9.1 / UIUX 5.4 区域 3）────────────────────────────────────

@router.get("/analysis/records", summary="分析记录列表（多条件筛选 + 分页）")
async def list_records(
    request: Request,
    user: CurrentUser,
    conversation_code: str | None = Query(default=None, description="按会话筛选"),
    report_code: str | None = Query(default=None, description="按报表筛选"),
    status: str | None = Query(default=None, description="success / failed"),
    q: str | None = Query(default=None, description="问题关键词（模糊）"),
    date_from: str | None = Query(default=None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(default=None, description="截止日期 YYYY-MM-DD（含当天）"),
    # 不在 Query 上做范围校验：越界统一由 service 抛 400 `E_VALIDATION`
    # （与 `/reports/{code}/preview` 的既有约定一致，避免框架的 422 与业务错误码混用）
    page: int = Query(default=1, description="页码，从 1 开始"),
    page_size: int = Query(default=20, description="每页条数，上限 100"),
) -> dict[str, Any]:
    """分析记录列表：offset 分页 + total + 组合筛选（排序固定 created_at DESC, id DESC）。"""
    result = await analysis_record_service.list_records(
        user.id,
        conversation_code=conversation_code,
        report_code=report_code,
        status=status,
        q=q,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )
    return ok(result.model_dump(), request_id=request_id(request))


@router.get("/analysis/records/{record_code}", summary="分析记录详情（含逐阶段模型调用明细）")
async def get_record(record_code: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    """记录详情：meta + slot + 实际执行的 SQL + 结果快照 + 解读 + 各阶段模型调用明细。"""
    result = await analysis_record_service.get_record(user.id, record_code)
    return ok(result.model_dump(), request_id=request_id(request))


@router.post("/analysis/records/{record_code}/replay", summary="重跑（锁定原报表，另存新记录）")
async def replay_record(record_code: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    """重跑：原问题整轮重跑（**报表锁定为原记录的报表**），落一条新记录并返回其详情。

    非流式（PRD 9.1）；响应里的 `replayed_from` 标明来源记录，便于并排比较 prompt 改动前后。
    """
    result = await analysis_record_service.replay_record(
        user.id,
        record_code,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
        request_id=request_id(request),
    )
    return ok(result.model_dump(), "重跑完成", request_id(request))


@router.delete("/analysis/records/{record_code}", summary="删除分析记录")
async def delete_record(record_code: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    """删除分析记录（`llm_calls` 级联删除；关联消息保留、仅解除引用）。"""
    result = await analysis_record_service.delete_record(user.id, record_code)
    return ok(result.model_dump(), "分析记录已删除", request_id(request))
