"""LangGraph 编排层：图的组装 + 运行器（`run_once_graph` / `resume_clarify_graph`）。

与既有 `analysis_service.run_once` 的关系
----------------------------------------
`ORCHESTRATOR=legacy` → 显式 async 编排；`ORCHESTRATOR=langgraph` → 本模块（**2026-10-13 起默认**）。
两条路径语义等价，由 `tests/test_langgraph_parity.py` 逐字段 + 逐事件保证。

五条硬约束
----------
1. **按需构建图**：不在 import 期读配置/编译图（超时常量必须"调用时"读，见难点 D-003）；
2. **事件即时推送**：节点经 `GraphContext.emit_event()` 推送（custom stream → 既有 SSE 回调），
   运行器用 `stream_mode=["values", "custom"]` 边产生边转发（ADR-018）；
3. **异常语义对齐**：会话不可用（`ctx.conversation_loaded` 为假）→ 向上抛；其余失败落失败状态并留痕；
4. **检查点（阶段 3）**：`thread_id = conversation_code`；每轮用 `DEFAULT_STATE` 显式重置过程态；
5. **Human-in-the-loop（阶段 4）**：澄清不再靠"第二跳请求重跑整轮"，而是
   `clarify_wait` 里 `interrupt()` 挂起 → 用户回填 → `/analysis/clarify` 用 `Command(resume=...)`
   **从断点继续**（建卡/落库/发事件放在中断之前的 `clarify_card`，不会重复执行）。
   **契约不变**：SSE 仍以 `clarify → meta → done(record_id=null)` 收尾；恢复请求也从
   `status(parsing)` 开始。线程没有挂起中断时**自动回退**为"整轮重跑"，功能不降级。

图结构
------

```
START → inject_schema → extract_slot ─┬─(缺关键条件)→ clarify_card → clarify_wait ⟲ interrupt
                                      │                                   │(resume=补丁)
                                      │                                   └→ extract_slot
                                      └─(条件齐备)→ generate ─┬─(sql)────→ guard ─┬─(被拦截)→ rewrite
                                                              └─(pandas)→ execute ─┬─(失败)→ rewrite
        execute ─┬─(成功·pandas)→ publish_table → chart → narrate → persist → END
                 └─(成功·sql)──→ shape_check ─┬─(形态不对)→ rewrite
                                              └─(形态 OK)→ publish_table ─→ chart …
```

节点与既有实现的对应关系见 `nodes/*.py` 的模块 docstring（都带行号）。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent import checkpointer as cp_module
from app.agent.adapters import GraphContext, state_to_result
from app.agent.nodes import (
    node_chart,
    node_clarify_card,
    node_clarify_wait,
    node_execute,
    node_extract_slot,
    node_generate,
    node_guard,
    node_inject_schema,
    node_narrate,
    node_persist,
    node_publish_table,
    node_rewrite,
    node_shape_check,
    route_after_execute,
    route_after_generate,
    route_after_guard,
    route_after_rewrite,
    route_after_shape,
    route_after_slot,
)
from app.agent.state import DEFAULT_STATE, AnalysisState
from app.core.config import get_settings
from app.core.errors import BizError, validation_error
from app.core.llm_client import get_llm
from app.db.session import control_session
from app.services import analysis_service, slot_service
from app.services.analysis_service import AnalysisResult, EventPush

logger = logging.getLogger(__name__)

#: 图节点名（供文档与测试引用，避免散落字符串）
NODE_INJECT_SCHEMA = "inject_schema"
NODE_EXTRACT_SLOT = "extract_slot"
NODE_CLARIFY_CARD = "clarify_card"
NODE_CLARIFY_WAIT = "clarify_wait"
NODE_GENERATE = "generate"
NODE_GUARD = "guard"
NODE_EXECUTE = "execute"
NODE_SHAPE_CHECK = "shape_check"
NODE_PUBLISH_TABLE = "publish_table"
NODE_REWRITE = "rewrite"
NODE_CHART = "chart"
NODE_NARRATE = "narrate"
NODE_PERSIST = "persist"

#: 带检查点的编译产物，按 `id(checkpointer)` 分桶（saver 本身每事件循环一个，见 checkpointer.py）
_PERSISTENT_GRAPHS: dict[int, Any] = {}


def _assemble() -> StateGraph:
    """组装图（未编译）——节点与条件边的**唯一定义处**。"""
    builder: StateGraph = StateGraph(AnalysisState)

    for name, fn in (
        (NODE_INJECT_SCHEMA, node_inject_schema),
        (NODE_EXTRACT_SLOT, node_extract_slot),
        (NODE_CLARIFY_CARD, node_clarify_card),
        (NODE_CLARIFY_WAIT, node_clarify_wait),
        (NODE_GENERATE, node_generate),
        (NODE_GUARD, node_guard),
        (NODE_EXECUTE, node_execute),
        (NODE_SHAPE_CHECK, node_shape_check),
        (NODE_PUBLISH_TABLE, node_publish_table),
        (NODE_REWRITE, node_rewrite),
        (NODE_CHART, node_chart),
        (NODE_NARRATE, node_narrate),
        (NODE_PERSIST, node_persist),
    ):
        builder.add_node(name, fn)

    # 入口段（含 HITL 澄清环：clarify_wait 恢复后回到 extract_slot 合并补丁）
    builder.add_edge(START, NODE_INJECT_SCHEMA)
    builder.add_edge(NODE_INJECT_SCHEMA, NODE_EXTRACT_SLOT)
    builder.add_conditional_edges(
        NODE_EXTRACT_SLOT,
        route_after_slot,
        # ⚠️ 映射的 key 必须等于**路由函数的返回值**（写错会 `KeyError`）；
        #    而路由函数的返回值又应当**就是节点名**——否则 Mermaid 导出里会少一条边
        #    （LangGraph 按节点名画条件边）。两条约定都在阶段 4 实测踩过。
        {NODE_CLARIFY_CARD: NODE_CLARIFY_CARD, NODE_GENERATE: NODE_GENERATE},
    )
    builder.add_edge(NODE_CLARIFY_CARD, NODE_CLARIFY_WAIT)
    builder.add_edge(NODE_CLARIFY_WAIT, NODE_EXTRACT_SLOT)

    # 生成 → 守卫 → 执行 → 形态自检 → 发表格
    builder.add_conditional_edges(
        NODE_GENERATE,
        route_after_generate,
        {NODE_GUARD: NODE_GUARD, NODE_EXECUTE: NODE_EXECUTE},
    )
    builder.add_conditional_edges(
        NODE_GUARD,
        route_after_guard,
        {NODE_REWRITE: NODE_REWRITE, NODE_EXECUTE: NODE_EXECUTE},
    )
    builder.add_conditional_edges(
        NODE_EXECUTE,
        route_after_execute,
        {
            NODE_REWRITE: NODE_REWRITE,
            NODE_SHAPE_CHECK: NODE_SHAPE_CHECK,
            NODE_PUBLISH_TABLE: NODE_PUBLISH_TABLE,
        },
    )
    builder.add_conditional_edges(
        NODE_SHAPE_CHECK,
        route_after_shape,
        {NODE_REWRITE: NODE_REWRITE, NODE_PUBLISH_TABLE: NODE_PUBLISH_TABLE},
    )
    builder.add_conditional_edges(
        NODE_REWRITE,
        route_after_rewrite,
        {NODE_PERSIST: NODE_PERSIST, NODE_GUARD: NODE_GUARD, NODE_EXECUTE: NODE_EXECUTE},
    )

    # 收尾段
    builder.add_edge(NODE_PUBLISH_TABLE, NODE_CHART)
    builder.add_edge(NODE_CHART, NODE_NARRATE)
    builder.add_edge(NODE_NARRATE, NODE_PERSIST)
    builder.add_edge(NODE_PERSIST, END)
    return builder


@lru_cache(maxsize=1)
def build_graph() -> Any:
    """编译**不带检查点**的图（测试与 `graph_mermaid()` 用；进程内只编译一次）。"""
    return _assemble().compile()


def build_graph_with_checkpointer(checkpointer: Any) -> Any:
    """编译**带检查点**的图（会话级持久化；HITL 澄清依赖它）。"""
    key = id(checkpointer)
    graph = _PERSISTENT_GRAPHS.get(key)
    if graph is None:
        graph = _assemble().compile(checkpointer=checkpointer)
        _PERSISTENT_GRAPHS[key] = graph
    return graph


def graph_mermaid() -> str:
    """导出 Mermaid 源码（阶段 5 用它替换架构文档 4.2 的手绘图：**文档即代码结构**）。"""
    return build_graph().get_graph().draw_mermaid()


def _graph_config(ctx: GraphContext, conversation_code: str) -> dict[str, Any]:
    """图的运行配置：运行时上下文 + **线程 = 会话**（检查点与 HITL 的锚点）。"""
    return {
        "configurable": {"ctx": ctx, "thread_id": conversation_code},
        "recursion_limit": 80,
    }


async def _stream(
    graph: Any, payload: Any, config: dict[str, Any], emit: EventPush | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """跑图并把**图内事件**边产生边转发给 `emit`；返回（最终状态, 收集到的事件）。"""
    events: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for mode, chunk in graph.astream(
        payload, config=config, stream_mode=["values", "custom"]
    ):
        if mode == "values":
            final = chunk
        elif mode == "custom" and isinstance(chunk, dict) and chunk.get("event"):
            events.append(chunk)
            if emit is not None:
                emit(chunk)
    return final, events


def _result_from_state(final: dict[str, Any], ctx: GraphContext, question: str) -> AnalysisResult:
    """把最终状态还原成 `AnalysisResult`（与 `run_once` 的返回同构）。"""
    result = state_to_result(final, ctx)
    result.conversation_code = ctx.conversation_code
    result.question = question
    result.elapsed_ms = int(final.get("elapsed_ms") or ctx.elapsed_ms)
    result.llm_tokens = int(final.get("llm_tokens") or 0)
    return result


async def _persist_failure(
    final: dict[str, Any],
    ctx: GraphContext,
    config: dict[str, Any],
    *,
    user_id: int,
    conversation_code: str,
    question: str,
    code: str,
    message: str,
    rule_id: str | None = None,
) -> dict[str, Any]:
    """异常兜底：把失败写进状态并**补一次留痕**（与 `run_once` 的 except 分支同构）。"""
    failed = {
        **final,
        "user_id": user_id,
        "conversation_code": conversation_code,
        "question": question,
        "status": "failed",
        "error_code": code,
        "error_message": message,
        "last_rule": rule_id,
    }
    await node_persist(failed, config)
    return failed


async def run_once_graph(
    user_id: int,
    conversation_code: str,
    question: str,
    *,
    report_code: str | None = None,
    switch_active_report: bool = True,
    slot_patch: dict[str, Any] | None = None,
    slot_base: dict[str, Any] | None = None,
    preset_slot: dict[str, Any] | None = None,
    include_user_message: bool = True,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    emit: EventPush | None = None,
) -> AnalysisResult:
    """`run_once` 的 LangGraph 版（**签名与返回值完全一致**，可直接替换调用点）。"""
    prompt_text = (question or "").strip()
    if not prompt_text:
        raise validation_error("问题不能为空")
    if len(prompt_text) > 2000:
        raise validation_error("问题过长（上限 2000 字），请精简后重试")

    ctx = GraphContext(llm=get_llm(), settings=get_settings(), emit=emit)
    ctx.conversation_code = conversation_code
    # 每轮起始：用 DEFAULT_STATE 显式重置过程态（带检查点后 state 会跨轮累积）
    init: AnalysisState = {
        **cp_module.per_turn_reset(),
        "user_id": user_id,
        "conversation_code": conversation_code,
        "question": prompt_text,
        "report_code": report_code,
        "switch_active_report": switch_active_report,
        "slot_patch": slot_patch,
        "slot_base": slot_base,
        "preset_slot": preset_slot,
        "include_user_message": include_user_message,
        "ip": ip,
        "user_agent": user_agent,
        "request_id": request_id,
    }

    checkpointer = await cp_module.get_checkpointer()
    graph = build_graph_with_checkpointer(checkpointer)
    config = _graph_config(ctx, conversation_code)
    # `final` 必须**先绑定**：`_stream` 抛异常时它不会被赋值，异常分支还要用它（实测踩到）
    final: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    try:
        final, events = await _stream(graph, init, config, emit)
    except BizError as exc:
        # ① 会话不可用（尚未加载成功）→ 无处留痕，向上抛，交端点按 HTTP 语义处理；
        # ② 其余失败（模型输出不可解析等"不重试"类错误）→ 落 failed 并补一次留痕
        if not ctx.conversation_loaded:
            raise
        ctx.last_error = exc
        final = await _persist_failure(
            final, ctx, config,
            user_id=user_id, conversation_code=conversation_code, question=prompt_text,
            code=exc.error_code, message=exc.message,
            rule_id=(exc.data or {}).get("rule_id"),
        )
        events = []
    except Exception as exc:  # noqa: BLE001 —— 未预期异常同样留痕，不静默
        logger.exception("LangGraph 编排未预期异常")
        if not ctx.conversation_loaded:
            raise
        final = await _persist_failure(
            final, ctx, config,
            user_id=user_id, conversation_code=conversation_code, question=prompt_text,
            code="E_INTERNAL", message=f"服务内部错误：{exc}",
        )
        events = []

    # —— HITL：是否停在澄清中断上（有挂起任务 = 正等用户回填）——
    snapshot = await graph.aget_state(config)
    if snapshot.next:
        card = (final or {}).get("clarify") or {}
        model = ctx.calls[0][1].model if ctx.calls else ""
        tokens = sum(reply.total_tokens for _, reply, _ in ctx.calls)
        # meta → done（顺序与 `_finish_clarify` 一致）；**既要推送、也要进返回对象的事件表**
        # ——非 SSE 调用方（评测/重跑）读的是 `result.events`，两者必须一致。
        for event in (
            {"event": "meta", "data": {
                "elapsed_ms": ctx.elapsed_ms, "retries": 0,
                "llm_tokens": tokens, "model": model, "llm_impl": ctx.llm.name,
            }},
            {"event": "done", "data": {"record_id": None}},
        ):
            events.append(event)
            if emit is not None:
                emit(event)
        result = _result_from_state(final, ctx, prompt_text)
        result.status = "success"      # 与既有实现一致：澄清轮 status 仍是 success
        result.clarify = card or None
        result.record_code = ""
        result.llm_model = model
        result.llm_tokens = tokens
        result.events = events  # type: ignore[assignment]
        logger.info(
            "澄清中断（HITL）：thread=%s missing=%s", conversation_code, (card or {}).get("missing_slot")
        )
        return result

    result = _result_from_state(final, ctx, prompt_text)
    result.events = events  # type: ignore[assignment]
    logger.info(
        "分析完成（langgraph）：%s status=%s route=%s rows=%d elapsed=%dms tokens=%d",
        result.record_code, result.status, result.route, result.row_count,
        result.elapsed_ms, result.llm_tokens,
    )
    return result


async def resume_clarify_graph(
    user_id: int,
    conversation_code: str,
    question: str,
    *,
    slot_patch: dict[str, Any],
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    emit: EventPush | None = None,
) -> AnalysisResult:
    """HITL 恢复：把回填补丁送进挂起的线程，**从断点继续**（阶段 4）。

    前置：先把补丁合入会话 `slot_json` 并持久化（与既有 `run_clarify` 一致——用户点选不丢）。
    若线程**没有**挂起的澄清（检查点被清理、换库、或中断来自旧版本），
    自动**回退**为"以合并后的完整 slot 重跑一轮"（= 阶段 1–3 的行为，功能不降级）。
    """
    # ① 与既有实现一致：补丁先合入会话 slot 并持久化
    patch = slot_service.validate_slot_patch(slot_patch)
    async with control_session() as session:
        conversation = await analysis_service._load_conversation(
            session, user_id, conversation_code
        )
        base = json.loads(conversation.slot_json) if conversation.slot_json else {}
        merged = slot_service.normalize_slot(slot_service.merge_slot(base, patch))
        conversation.slot_json = json.dumps(merged, ensure_ascii=False)
        await session.commit()

    ctx = GraphContext(llm=get_llm(), settings=get_settings(), emit=emit)
    ctx.conversation_code = conversation_code
    checkpointer = await cp_module.get_checkpointer()
    graph = build_graph_with_checkpointer(checkpointer)
    config = _graph_config(ctx, conversation_code)

    snapshot = await graph.aget_state(config)
    if not snapshot.next:  # 没有挂起的中断 → 回退为"整轮重跑"
        logger.warning("线程 %s 无挂起澄清，回退为整轮重跑（preset_slot）", conversation_code)
        return await run_once_graph(
            user_id, conversation_code, question,
            preset_slot=merged, include_user_message=False,
            ip=ip, user_agent=user_agent, request_id=request_id, emit=emit,
        )

    # ② 与 legacy 澄清延续的事件序列对齐：先发 `status(parsing)`（本轮要重新解析补丁）
    if emit is not None:
        emit({"event": "status", "data": {"stage": "parsing"}})
    # ③ 修正状态：原问题在澄清轮已落过 user 消息（决策 19-1），恢复时不要再写一遍
    await graph.aupdate_state(config, {"include_user_message": False})

    final: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    try:
        final, events = await _stream(graph, Command(resume=patch), config, emit)
    except BizError as exc:
        if not ctx.conversation_loaded:
            raise
        ctx.last_error = exc
        final = await _persist_failure(
            final, ctx, config,
            user_id=user_id, conversation_code=conversation_code, question=question,
            code=exc.error_code, message=exc.message,
            rule_id=(exc.data or {}).get("rule_id"),
        )
        events = []

    result = _result_from_state(final, ctx, question)
    result.events = events  # type: ignore[assignment]
    logger.info(
        "澄清恢复完成（HITL）：%s status=%s route=%s rows=%d",
        result.record_code, result.status, result.route, result.row_count,
    )
    return result
