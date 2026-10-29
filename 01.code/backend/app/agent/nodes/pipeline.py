"""LangGraph 节点实现（**阶段 1：1:1 复刻既有 `run_once` 的顺序**，行为必须一致）。

设计口径
--------
- 节点**薄封装**既有函数（`analysis_service` 的私有函数与 `slot_service` / `schema_service`），
  目的是"换编排、不换行为"——303 条用例与既有调用方全部保持有效（见大修进度文档）。
- 每个节点出入都经过 `adapters`：入口 `state_to_result()` 组装既有 `AnalysisResult`，
  出口 `result_to_state()` 把可序列化字段镜像回 state。
- **不可序列化的东西一律放 `GraphContext`**（LLM 客户端、calls 累加器、sandbox 产生的 Plotly figure、
  告警收集），它们经 `config["configurable"]["ctx"]` 传递，不进 state（阶段 3 用 SqliteSaver 的前提）。
- 事件：全走 `ctx.emit_event()`（优先 custom stream，回落既有 SSE 回调），保持"产生即推送"。

与 `run_once` 的行号对照（改造时的依据，便于逐行核对）：

| 节点 | `analysis_service.run_once` 对应行 |
| --- | --- |
| `node_inject_schema` | 249–267（加载会话/报表、Schema 注入、活跃报表切换） |
| `node_extract_slot` | 269–369（slot_patch 合并、快通道、LLM slot、超时兜底、澄清检查、`slot` 事件） |
| `node_clarify` | 371–378（澄清卡 + 澄清轮收尾） |
| `node_generate_execute` | 380–428（生成 + 守卫 + 执行 + 重写环 + `table` 事件） |
| `node_chart` | 430–435（图表，本地计算不调 LLM） |
| `node_narrate` | 437–497（解读 + 超时兜底 + 未授权数字检测 + `narrative` 事件） |
| `node_persist` | 512–566（meta/error 事件、留痕、审计告警、`done` 事件） |
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agent import prompts
from app.agent.adapters import (
    GraphContext,
    calls_to_records,
    context_from_config,
    result_to_state,
    state_to_result,
)
from app.agent.state import AnalysisState
from app.core.errors import BizError
from langchain_core.runnables import RunnableConfig
from app.services import analysis_service as svc
from app.services import schema_service, slot_service

logger = logging.getLogger(__name__)


# ── ① 会话与报表解析 + Schema 注入 ────────────────────────────────────────


async def node_inject_schema(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """加载会话与报表、注入 Schema、按需切换活跃报表（= run_once 249–267）。"""
    ctx = context_from_config(config)
    async with svc.control_session() as session:
        conversation = await svc._load_conversation(
            session, state["user_id"], state["conversation_code"]
        )
        report = await svc._resolve_report(
            session,
            state["user_id"],
            conversation,
            state.get("report_code"),
            switch=state.get("switch_active_report", True),
        )
        schema_text = schema_service.render_injection_text(report)["text"]
        schema = json.loads(report.schema_json) if report.schema_json else {}
        payload: dict[str, Any] = {
            "report_code": report.report_code,
            "table_name": report.table_name,
            "report_filename": report.filename,
            "schema_text": schema_text,
            "schema": schema,
            # 只读守卫的表名白名单 = 当前会话可用报表（见 sql_guard 的用法）
            "allowed_tables": [report.table_name],
            "quality_json": report.quality_json,
        }
    ctx.conversation_loaded = True
    ctx.emit_event({"event": "status", "data": {"stage": "parsing"}})
    return payload


# ── ② slot 抽取（含澄清判定）────────────────────────────────────────────


async def node_extract_slot(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """slot 增量抽取 → 合并 → 澄清判定（= run_once 269–369）。"""
    ctx = context_from_config(config)
    prompt_text = state.get("question") or ""
    result = state_to_result(state, ctx)
    preset_slot = state.get("preset_slot")

    increment_intent: str | None = None
    model_missing: str | None = None
    slot_source = "preset"

    clarify_resume = state.get("clarify_resume")
    if preset_slot is not None:
        # clarify 继续执行（legacy 口径）：slot 已由「会话条件 + 回填」合并归一化，跳过增量抽取
        merged = dict(preset_slot)
    elif clarify_resume:
        # HITL 恢复（阶段 4）：用户在澄清卡上回填 → `interrupt()` 返回补丁 →
        # 与**中断前的部分 slot** 合并即可（等价既有 `preset_slot` 语义：跳过二次抽取、不调模型）
        base_slot_for_resume = dict(state.get("slot") or {})
        merged = slot_service.normalize_slot(
            slot_service.merge_slot(
                base_slot_for_resume, slot_service.validate_slot_patch(clarify_resume)
            )
        )
        increment_intent = None
        model_missing = None
        slot_source = "patch"
    else:
        async with svc.control_session() as session:
            conversation = await svc._load_conversation(
                session, state["user_id"], state["conversation_code"]
            )
            slot_patch = slot_service.validate_slot_patch(state.get("slot_patch"))
            if slot_patch:
                # 条件芯片编辑即刻生效（决策 20-1）：即使用户后续追问触发澄清，改动也不丢
                merged_patch = slot_service.merge_slot(
                    json.loads(conversation.slot_json) if conversation.slot_json else {},
                    slot_patch,
                )
                conversation.slot_json = json.dumps(
                    slot_service.normalize_slot(merged_patch), ensure_ascii=False
                )
                await session.commit()
                slot_source = "patch"

        slot_base = state.get("slot_base")
        if slot_base is None:
            base_slot = json.loads(conversation.slot_json) if conversation.slot_json else {}
        else:
            base_slot = slot_base

        # ①.0 规则快通道优先（省一次 LLM 往返，slot 阶段实测 1.5–2.0s）
        fast_increment = slot_service.fast_slot(prompt_text, base_slot)
        if fast_increment is not None:
            increment_intent = fast_increment.get("intent")
            model_missing = None
            merged = slot_service.merge_slot(base_slot, fast_increment)
            slot_source = "fastpath" if slot_source != "patch" else "patch"
            logger.info(
                "slot 快通道命中（跳过 LLM）：metric=%s time_range=%s",
                fast_increment.get("metric"),
                fast_increment.get("time_range"),
            )
        else:
            try:
                slot_reply = await asyncio.wait_for(
                    ctx.llm.chat(
                        [
                            {"role": "system", "content": prompts.SLOT_SYSTEM},
                            {
                                "role": "user",
                                "content": prompts.slot_user_prompt(
                                    prompt_text,
                                    svc._available_hints(state.get("schema") or {}),
                                    current_slot=json.dumps(base_slot, ensure_ascii=False),
                                ),
                            },
                        ],
                        purpose="slot",
                        model=svc._model_for(ctx.llm, ctx.settings.llm_model_light),
                        json_mode=True,
                    ),
                    timeout=svc.SLOT_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # ①.1 超时兜底：长尾不干等，用宽松规则构造增量继续；认不出指标才如实失败
                fallback = slot_service.fallback_slot(prompt_text, base_slot)
                if fallback is None:
                    raise BizError(
                        504,
                        "E_LLM",
                        f"槽位解析超时（超过 {svc.SLOT_TIMEOUT_SECONDS:.0f}s），且无法从问题中识别指标",
                        {"purpose": "slot", "timeout_s": svc.SLOT_TIMEOUT_SECONDS},
                        hint="请把问题写得明确一些（指明指标，如「总用电量」「峰段用电量」）后重试",
                    ) from None
                increment_intent = fallback.get("intent")
                model_missing = None
                merged = slot_service.merge_slot(base_slot, fallback)
                slot_source = "fallback"
            else:
                ctx.calls.append(("slot", slot_reply, 0))
                payload = svc._safe_json(slot_reply.text, stage="slot")
                increment_intent = payload.get("intent")
                model_missing = payload.get("missing_slot")
                increment = svc._normalize_slot(payload)
                merged = slot_service.merge_slot(base_slot, increment)
                slot_source = "llm"

    # ①.5 澄清检查（模型优先，规则 metric 缺失兜底，指标词面兜底）
    clarify_missing = slot_service.resolve_clarify(
        increment_intent=increment_intent,
        model_missing=model_missing,
        merged=merged,
        question=prompt_text,
    )
    if not clarify_missing and not merged.get("metric"):
        guessed_metric = slot_service.guess_metric_from_question(prompt_text)
        if guessed_metric:
            merged["metric"] = guessed_metric
            logger.info("澄清兜底命中指标：%s（question=%.40s）", guessed_metric, prompt_text)

    if clarify_missing:
        # 澄清轮的 slot 事件展示**部分 slot**（合并后、填默认前），芯片如实显示空位
        partial = dict(merged)
        if partial.get("intent") == slot_service.CLARIFY_INTENT:
            partial["intent"] = None
        result.slot = partial
        result.llm_model = ctx.calls[0][1].model if ctx.calls else ""
        out = {"slot": partial, "slot_missing": clarify_missing, "slot_source": slot_source}
    else:
        result.slot = slot_service.normalize_slot(merged)
        out = {"slot": result.slot, "slot_missing": None, "slot_source": slot_source}

    ctx.emit_event({"event": "slot", "data": result.slot})
    return out


def route_after_slot(state: AnalysisState) -> str:
    """条件边：缺关键条件 → 澄清卡（HITL）；否则继续生成。

    返回值直接用**节点名**：LangGraph 的 Mermaid 导出按节点名画条件边，
    用其它字符串会出现"运行正常、但导出的图里少一条边"的怪象（阶段 4 实测踩到）。
    """
    return "clarify_card" if state.get("slot_missing") else "generate"


# ── ③ 澄清卡（分支出口）──────────────────────────────────────────────────


async def node_clarify_card(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """澄清卡（阶段 4 · HITL 第一步）：建卡 → 落 messages×2 → 发 `clarify` 事件。

    为什么把它放在**中断之前的独立节点**：LangGraph 恢复时会**重跑被中断的那个节点**
    （`interrupt()` 之前的代码会再执行一遍）。若把"建卡 / 落库 / 发事件"塞进中断节点，
    用户回填后就会重复落库、重复发事件。拆开之后本节点只执行一次，其状态写入被检查点保存；
    中断节点里只剩 `interrupt()` 一个动作（见 `node_clarify_wait`）。

    与既有实现的对齐：澄清轮**不写 `analyses`**、`status` 仍是 `success`、`record_id=null`，
    靠 `clarify` 字段区分；`meta` / `done` 由运行器在请求收尾时补发（顺序与 `_finish_clarify` 一致）。
    """
    ctx = context_from_config(config)
    card = await svc._build_clarify_card(
        state.get("slot_missing") or "", user_id=state["user_id"], schema=state.get("schema") or {}
    )
    # 事件顺序与 `_finish_clarify` 一致：clarify →（请求收尾）meta → done
    ctx.emit_event(
        {"event": "clarify", "data": {
            "question": card["question"],
            "options": card["options"],
            "missing_slot": card["missing_slot"],
            "partial_slot": state.get("slot") or {},
        }}
    )
    try:
        async with svc.control_session() as session:
            conversation = await svc._load_conversation(
                session, state["user_id"], state["conversation_code"]
            )
            await svc._persist_clarify(
                session, state["user_id"], conversation, state.get("question") or "", card
            )
    except Exception:  # noqa: BLE001 —— 与既有链路同样的"留痕宽容"：落库失败不影响事件流
        logger.exception("澄清轮消息落库失败 conversation=%s", state.get("conversation_code"))
    return {"clarify": card, "status": "success"}


async def node_clarify_wait(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """澄清等待（阶段 4 · HITL 第二步）：`interrupt()` 挂起，等用户回填后恢复。

    恢复时本节点**整体重跑**（LangGraph 的设计），所以这里除了读 state 与 `interrupt()`
    不做任何副作用；回填值经 `clarify_resume` 交给 `extract_slot` 合并（跳过二次抽取）。
    检查点是它的前置条件：中断状态就存在线程（`thread_id = 会话编号`）里（阶段 3）。
    """
    from langgraph.types import interrupt  # 局部导入：让本模块在不跑图时也轻量

    patch = interrupt(
        {"clarify": state.get("clarify"), "partial_slot": state.get("slot") or {}}
    )
    return {"clarify_resume": patch if isinstance(patch, dict) else None}


# ── ⑤ 图表（本地计算，不调 LLM）────────────────────────────────────────


async def node_chart(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """图表载荷（= run_once 430–435；失败只降级、不抛异常）。"""
    ctx = context_from_config(config)
    result = state_to_result(state, ctx)
    chart_payload = svc._chart_payload(
        state.get("schema") or {}, result, sandbox_figure=ctx.sandbox_figure
    )
    result.chart = chart_payload.get("figure")
    ctx.emit_event({"event": "chart", "data": chart_payload})
    return {"chart": result.chart}


# ── ⑥ 文字解读 ─────────────────────────────────────────────────────────


async def node_narrate(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """解读 + 超时兜底 + 未授权数字检测（= run_once 437–497）。"""
    ctx = context_from_config(config)
    result = state_to_result(state, ctx)
    prompt_text = state.get("question") or ""
    table_payload = {
        "columns": state.get("columns") or [],
        "rows": state.get("rows") or [],
        "row_count": state.get("row_count") or 0,
        "truncated": bool(state.get("truncated")),
    }
    narrative_timeout = svc._narrative_timeout()
    skipped_note: str | None = None
    try:
        narrative_reply = await asyncio.wait_for(
            ctx.llm.chat(
                [
                    {"role": "system", "content": prompts.NARRATIVE_SYSTEM},
                    {
                        "role": "user",
                        "content": prompts.narrative_user_prompt(
                            prompt_text,
                            state.get("sql") or "",
                            table_payload,
                            truncated=bool(state.get("truncated")),
                            quality_hint=svc._quality_hint(state.get("quality_json")),
                            code=state.get("code"),
                        ),
                    },
                ],
                purpose="narrative",
                # 解读用**专用档**（`LLM_MODEL_NARRATIVE`，未配置则回落轻量档）：
                # 实测主力"推理型"模型在此处最贵且今日会"推理洪水"（completion 165 → 2,536、正文解析失败），
                # 故解读走输出节制、JSON 稳定的模型；**与 slot 解耦**（见 `_narrative_model` 注释）。
                model=svc._narrative_model(ctx.llm, ctx.settings),
                json_mode=True,
            ),
            timeout=narrative_timeout,
        )
    except TimeoutError:
        # 超时**不再"无解读收场"**（需求方 2026-10-14 明确：解读是本项目的交付物）：
        # 用结果表能直接算出来的事实生成「自动摘要」，并在正文里如实标注来源。
        logger.warning("narrative 超时（>%.0fs）→ 改用自动摘要", narrative_timeout)
        skipped_note = (
            f"模型解读超时（超过 {narrative_timeout:.0f}s），已改用自动摘要；表格与图表结果不受影响"
        )
        narrative = svc._rule_narrative(
            table_payload, reason=f"模型解读超时（超过 {narrative_timeout:.0f}s）"
        )
    else:
        ctx.calls.append(("narrative", narrative_reply, 0))
        try:
            narrative = svc._safe_json(narrative_reply.text, stage="narrative")
        except svc.BizError:
            # 解读解析失败**不能拖垮整题**（解读是增值项）：退化为自动摘要（此前会整题失败）
            logger.warning("narrative 解析失败 → 改用自动摘要", exc_info=True)
            skipped_note = "模型解读输出无法解析，已改用自动摘要；表格与图表结果不受影响"
            narrative = svc._rule_narrative(table_payload, reason="模型解读输出无法解析")

    text = str(narrative.get("text") or "").strip()
    key_numbers = svc._as_list(narrative.get("key_numbers"))
    anomalies = [str(item) for item in svc._as_list(narrative.get("anomalies"))]
    if skipped_note:
        anomalies = [*anomalies, skipped_note]
    stray_numbers = svc._unauthorized_numbers(text, table_payload, key_numbers)
    if stray_numbers:
        anomalies = [
            *anomalies,
            "解读中出现未在查询结果里的数字（"
            + "、".join(stray_numbers[:5])
            + "），请以表格与图表为准",
        ]
        ctx.narrative_warnings = stray_numbers

    ctx.emit_event(
        {"event": "narrative", "data": {"text": text, "key_numbers": key_numbers, "anomalies": anomalies}}
    )
    return {
        "narrative": {"text": text},
        "key_numbers": key_numbers,
        "anomalies": anomalies,
        "calls": calls_to_records(ctx.calls),
    }


# ── ⑦ 留痕与收尾 ───────────────────────────────────────────────────────


async def node_persist(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """meta/error 事件 + 全量留痕 + 审计告警 + done（= run_once 512–566）。"""
    ctx = context_from_config(config)
    result = state_to_result(state, ctx)
    # 状态收敛：图的默认状态是 `running`（过程态），而留痕表只允许 success / failed
    # （`analyses.status` 有 CHECK 约束）→ 到这里仍为 running 就说明本轮走完了正常链路。
    if result.status in (None, "running"):
        result.status = "success"
    result.slot = state.get("slot") or {}
    result.narrative = (state.get("narrative") or {}).get("text") or ""
    result.key_numbers = state.get("key_numbers") or []
    result.anomalies = state.get("anomalies") or []
    result.error_code = state.get("error_code")
    result.error_message = state.get("error_message")
    result.elapsed_ms = ctx.elapsed_ms
    result.llm_tokens = sum(reply.total_tokens for _, reply, _ in ctx.calls)

    if result.status == "success":
        ctx.emit_event(
            {"event": "meta", "data": {
                "elapsed_ms": result.elapsed_ms,
                "retries": result.retries,
                "llm_tokens": result.llm_tokens,
                "model": result.llm_model,
                "llm_impl": ctx.llm.name,
            }}
        )
    else:
        ctx.emit_event(
            {"event": "error", "data": {
                "code": result.error_code,
                "message": result.error_message,
                "detail": f"route={result.route} rule={result.last_rule or '-'}",
                "retries": result.retries,
                "path_switched": result.path_switched,
            }}
        )

    async with svc.control_session() as session:
        conversation = await svc._load_conversation(
            session, state["user_id"], state["conversation_code"]
        )
        report = None
        try:
            report = await svc._resolve_report(
                session, state["user_id"], conversation, state.get("report_code"), switch=False
            )
        except BizError:
            logger.warning("留痕时报表不可用，report 置 NULL：%s", state.get("report_code"))
        try:
            result.record_code = await svc._persist(
                session,
                state["user_id"],
                conversation,
                report,
                result,
                ctx.calls,
                state.get("request_id"),
                include_user_message=state.get("include_user_message", True),
            )
        except Exception:  # noqa: BLE001 —— 结果已生成，落库失败不应让用户拿不到结果
            logger.exception("分析结果落库失败 record=%s", result.record_code)
            if not result.record_code:
                result.record_code = "a_000000"

    if ctx.narrative_warnings:
        try:
            await svc._audit_narrative_warning(
                user_id=state["user_id"],
                conversation_code=state["conversation_code"],
                record_code=result.record_code,
                numbers=ctx.narrative_warnings,
                ip=state.get("ip"),
                user_agent=state.get("user_agent"),
            )
        except Exception:  # noqa: BLE001 —— 告警失败不影响结果返回
            logger.exception("解读未授权数字告警落库失败 record=%s", result.record_code)

    ctx.emit_event({"event": "done", "data": {"record_id": result.record_code}})
    logger.info(
        "分析完成（langgraph）：%s status=%s route=%s rows=%d elapsed=%dms tokens=%d",
        result.record_code, result.status, result.route, result.row_count,
        result.elapsed_ms, result.llm_tokens,
    )
    return {
        "status": result.status,  # type: ignore[typeddict-item]
        "record_code": result.record_code or None,
        "elapsed_ms": result.elapsed_ms,
        "llm_tokens": result.llm_tokens,
        "llm_model": result.llm_model,
        "calls": calls_to_records(ctx.calls),
    }
