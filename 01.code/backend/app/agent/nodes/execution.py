"""生成 / 守卫 / 执行 / 形态自检 / 重写环 —— **拆成真正的条件边**（大修阶段 2）。

为什么拆
--------
阶段 1 里这一段是"一个节点包住整个 while 环"（直接复用 `analysis_service._generate_and_execute`），
图的形状有了、但**环还是藏在函数内部**。阶段 2 把它拆成显式节点 + 条件边，
让"失败如何自愈"这件事在图上**看得见、可单独测试、可单独改**：

```
generate ─┬─(sql)──→ guard ─┬─(被拦截/放行失败)→ rewrite ─┬─(重写/换路)→ guard | execute
          └─(pandas)→ execute ─┬─(成功·pandas)→ publish_table → chart
                               ├─(成功·sql)──→ shape_check ─┬─(形态不对)→ rewrite
                               │                            └─(形态 OK)→ publish_table → chart
                               └─(执行失败)──→ rewrite ─┬─(额度用尽)→ persist(failed)
                                                       └─(重写/换路)→ guard | execute
```

与原实现的**一一对应**（`analysis_service._generate_and_execute`，行号即依据）：

| 图节点 | 原实现位置 | 语义 |
| --- | --- | --- |
| `generate` | `run_once` 380–403 | 首次生成：模型自报 route（sql / pandas 二选一） |
| `guard` | 1045–1069 | SQL 守卫（表名白名单 / 只读）；**拦截不换路**，但照发 `code` 事件 |
| `execute` | 1070–1108 | 只读执行 / pandas 沙箱执行（AST 预检在沙箱内部） |
| `shape_check` | 1075–1084 | **三类结果形态自检**（占比 / 极值 / 筛选型追问），仅 SQL 路径 |
| `publish_table` | `run_once` 423–428 | 形态过关才发 `table` 事件（顺序：chart 之前） |
| `rewrite` | 1110–1198 | 重写环：**带错误重写一次 → 换路 pandas 一次 → 额度用尽如实失败** |

三条**必须严格保持**的原语义（阶段 2 的等价性验收点）：

1. `_NO_RETRY_CODES = {E_LLM, E_OUT_OF_SCOPE, E_VALIDATION}` → **不重写**，直接判失败；
2. **安全拦截（`E_SQL_FORBIDDEN` / `E_CODE_FORBIDDEN`）不换路**——否则"删表"这类请求
   会换来一次无关的 pandas 成功结果，且多给一次生成机会；
3. 生成调用**每个额度最多用一次**（首次 + 重写 1 + 换路 1 ⇒ 总生成调用 ≤3），
   用尽后抛**最后一次**的错误（由 `persist` 落 `status='failed'`）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent import prompts
from app.agent.adapters import (
    calls_to_records,
    context_from_config,
    result_to_state,
    state_to_result,
)
from app.agent.state import AnalysisState
from app.core import sql_guard
from app.core.config import get_settings
from app.core.errors import E_CODE_FORBIDDEN, E_SQL_EXEC, E_SQL_FORBIDDEN, BizError
from app.services import analysis_service as svc
from app.services import sandbox_service

logger = logging.getLogger(__name__)

#: 与 `analysis_service._NO_RETRY_CODES` 同源（这里显式再声明一次，便于本模块独立阅读）
_NO_RETRY_CODES = svc._NO_RETRY_CODES
#: 安全拦截类错误：**不换路**（见模块 docstring 第 2 条）
_BLOCKED_CODES = (E_SQL_FORBIDDEN, E_CODE_FORBIDDEN)


# ── 生成 ───────────────────────────────────────────────────────────────────


async def node_generate(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """首次生成（= run_once 380–403）：模型自报 route，SQL / pandas 二选一。"""
    ctx = context_from_config(config)
    prompt_text = state.get("question") or ""
    slot_json_text = json.dumps(state.get("slot") or {}, ensure_ascii=False)

    gen_replies = await svc._chat_generate(
        ctx.llm,
        [
            {"role": "system", "content": prompts.SQL_SYSTEM},
            {
                "role": "user",
                "content": prompts.sql_user_prompt(
                    prompt_text, state.get("schema_text") or "", slot_json_text
                ),
            },
        ],
        # 空输出**或**"解析得动但缺 sql/code"都重发一次（后者实测于 q119/q110）
        validate=svc._looks_like_generation,
    )
    for index, reply in enumerate(gen_replies):
        ctx.calls.append(("gen_sql", reply, index))
    gen_reply = gen_replies[-1]
    # 解析失败（E_LLM）属"不重试"类错误 → 直接抛给运行器（与 run_once 同构）
    payload = svc._safe_json(gen_reply.text, stage="gen_sql")

    route = str(payload.get("route") or "").strip().lower()
    raw_sql = str(payload.get("sql") or "").strip()
    raw_code = str(payload.get("code") or "").strip()
    if route not in ("sql", "pandas"):
        # 模型没自报路由 → 按"给了什么"推断，再兜底 sql（T2：pandas 只做增量出口）
        route = "pandas" if (raw_code and not raw_sql) else "sql"
    if route == "pandas" and not raw_code:
        route = "sql"

    return {
        "route": route,
        "sql": raw_sql or None,
        "code": raw_code or None,
        "llm_model": gen_reply.model,
        "calls": calls_to_records(ctx.calls),
    }


def route_after_generate(state: AnalysisState) -> str:
    """条件边：SQL 走守卫；pandas 直接进沙箱（预检在沙箱内部）。"""
    return "guard" if state.get("route") == "sql" else "execute"


# ── SQL 守卫 ───────────────────────────────────────────────────────────────


async def node_guard(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """SQL 守卫（= _ga 1045–1069）：表名白名单 + 只读；被拦截时**审计留痕**并落到失败状态。"""
    ctx = context_from_config(config)
    result = state_to_result(state, ctx)
    result.slot = state.get("slot") or {}
    raw_sql = state.get("sql") or ""

    async with svc.control_session() as session:
        conversation = await svc._load_conversation(
            session, state["user_id"], state["conversation_code"]
        )
        report = await svc._resolve_report(
            session, state["user_id"], conversation, state.get("report_code"), switch=False
        )
    guarded = sql_guard.guard_sql(raw_sql, {report.table_name})
    normalized_sql = guarded.sql or raw_sql
    result.sql = normalized_sql
    if guarded.null_guarded:
        # Top-N 极值自动补 NULL 排除：只落日志（落库 SQL 里两者同形，日志是唯一区分痕迹）
        logger.info(
            "守卫为 Top-N 极值补了 NULL 排除：%s（record=%s）",
            "、".join(guarded.null_guarded), result.record_code,
        )
    # 事件顺序与原实现一致：**先发 code（即使随后被拦截，前端也能看到那条 SQL）**
    ctx.emit_event({"event": "code", "data": {"route": "sql", "sql": normalized_sql}})

    if not guarded.allowed:
        await svc._audit_blocked(
            state["user_id"], state["conversation_code"], raw_sql, guarded,
            state.get("ip"), state.get("user_agent"),
        )
        message = f"生成的 SQL 未通过安全校验：{guarded.reason}"
        return {
            "sql": normalized_sql,
            "guard_blocked": True,
            "last_error_code": E_SQL_FORBIDDEN,
            "last_error_message": message,
            "last_error_data": {"rule_id": guarded.rule_id},
            "last_rule": guarded.rule_id,
        }

    return {
        "sql": normalized_sql,
        "guard_blocked": False,
        "limit_applied": bool(guarded.limit_applied),
        "null_guarded": list(guarded.null_guarded or []),
    }


def route_after_guard(state: AnalysisState) -> str:
    """条件边：被拦截 → 重写环（在那里判定"仍重写一次、但不换路"）；放行 → 执行。

    判断依据是 `guard_blocked`（守卫**本轮**的结论），**不是** `last_error_code`：
    后者在重写成功后仍是上一次的旧值，会导致"没执行就又进重写环"（真 bug，见 state.py 注释）。
    """
    return "rewrite" if state.get("guard_blocked") else "execute"


# ── 执行 ───────────────────────────────────────────────────────────────────


async def node_execute(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """执行（= _ga 1070–1108）：SQL 只读执行 / pandas 沙箱执行。失败落到 `last_error_*`。"""
    ctx = context_from_config(config)
    route = state.get("route") or "sql"

    try:
        if route == "sql":
            # SQL 路径：`code` 事件已由守卫发出（原实现同序）→ 这里只发 status
            ctx.emit_event({"event": "status", "data": {"stage": "executing"}})
            payload = await svc._execute(
                state.get("sql") or "", limit_applied=bool(state.get("limit_applied"))
            )
            return {
                "columns": payload["columns"],
                "rows": payload["rows"],
                "row_count": payload["row_count"],
                "truncated": bool(payload["truncated"]),
                "last_error_code": None,
                "last_error_message": None,
                "last_error_data": None,
            }

        # pandas：AST 预检在沙箱内部（不过预检根本不进沙箱）。
        # 事件顺序必须与原实现一致：**先 `code` 再 `status(executing)`**（等价性用例抓过此处）
        code = state.get("code") or ""
        ctx.emit_event({"event": "code", "data": {"route": "pandas", "code": code}})
        ctx.emit_event({"event": "status", "data": {"stage": "executing"}})
        settings = get_settings()
        async with svc.control_session() as session:
            conversation = await svc._load_conversation(
                session, state["user_id"], state["conversation_code"]
            )
            report = await svc._resolve_report(
                session, state["user_id"], conversation, state.get("report_code"), switch=False
            )
        outcome = await sandbox_service.run_pandas_code(
            code, report.table_name, db_path=settings.reports_db
        )
        ctx.sandbox_figure = outcome.figure  # 不可序列化 → 放 ctx（chart 节点兜底用）
        return {
            "code": code,
            "columns": outcome.columns,
            "rows": outcome.rows,
            "row_count": outcome.row_count,
            "truncated": bool(outcome.truncated),
            "last_error_code": None,
            "last_error_message": None,
            "last_error_data": None,
        }
    except BizError as exc:
        if exc.error_code == E_CODE_FORBIDDEN:
            # 代码 AST 预检拦截落审计（与 `node_guard` 里的 `_audit_blocked` 对位，v1.19.28）
            await svc._audit_code_blocked(
                state["user_id"], state["conversation_code"], state.get("code") or "",
                str((exc.data or {}).get("rule_id") or ""), exc.message,
                state.get("ip"), state.get("user_agent"),
            )
        return {
            "last_error_code": exc.error_code,
            "last_error_message": exc.message,
            "last_error_data": dict(exc.data or {}),
        }


def route_after_execute(state: AnalysisState) -> str:
    """条件边：失败 → 重写环；pandas 成功 → 直接发表格（**不做形态自检**，与原实现一致）；SQL 成功 → 形态自检。"""
    if state.get("last_error_code"):
        return "rewrite"
    return "publish_table" if state.get("route") == "pandas" else "shape_check"


# ── 结果形态自检（仅 SQL 路径）───────────────────────────────────────────


async def node_shape_check(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """三类结果形态自检（= _ga 1075–1084）：算错形态**当成一次失败**，交给重写环。"""
    table_payload = {
        "columns": state.get("columns") or [],
        "rows": state.get("rows") or [],
        "row_count": state.get("row_count") or 0,
        "truncated": bool(state.get("truncated")),
    }
    shape = svc._shape_issue(
        state.get("slot") or {}, state.get("question") or "", state.get("sql") or "", table_payload
    )
    if shape:
        rule_id, message = shape
        return {
            "shape_issue": rule_id,
            "last_error_code": E_SQL_EXEC,
            "last_error_message": message,
            "last_error_data": {"rule_id": rule_id},
        }
    return {"shape_issue": None}


def route_after_shape(state: AnalysisState) -> str:
    """条件边：形态不对 → 带原因重写；形态 OK → 发表格。"""
    return "rewrite" if state.get("shape_issue") else "publish_table"


# ── 发表格（成功路径）────────────────────────────────────────────────────


async def node_publish_table(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """发 `table` 事件（= run_once 423–428）：位置固定在 chart 之前（UIUX 8.1）。"""
    ctx = context_from_config(config)
    payload = {
        "columns": state.get("columns") or [],
        "rows": state.get("rows") or [],
        "row_count": state.get("row_count") or 0,
        "truncated": bool(state.get("truncated")),
    }
    ctx.emit_event({"event": "table", "data": payload})
    return {
        "table_ready": True,
        "route": state.get("route") or "sql",
        "retries": int(state.get("retries") or 0),
        "path_switched": int(state.get("path_switched") or 0),
    }


# ── 重写环（带错误重写一次 → 换路一次 → 用尽）─────────────────────────────


async def node_rewrite(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
    """重写环（= _ga 1110–1198）。

    `retries` / `path_switched` 在 state 里累积：语义与既有 `analyses` 列一致
    （`retries` = 是否用过"带错误重写"额度；`path_switched` = 是否从 SQL 换到 pandas）。
    """
    ctx = context_from_config(config)
    prompt_text = state.get("question") or ""
    schema_text = state.get("schema_text") or ""
    slot_json_text = json.dumps(state.get("slot") or {}, ensure_ascii=False)
    route = state.get("route") or "sql"
    sql = state.get("sql") or ""
    code = state.get("code") or ""
    retried = bool(state.get("retries"))
    switched = bool(state.get("path_switched"))
    error_code = state.get("last_error_code")
    blocked = error_code in _BLOCKED_CODES

    def failed(**extra: Any) -> dict[str, Any]:
        """额度用尽 / 不可重试 → 落**最终失败字段**（对应 `run_once` 的 except 分支）。

        必须在这里把 `last_error_*` 转成 `status/error_code/error_message/last_rule`：
        `persist` 节点只认后者（`error` 事件的 detail 行与 `analyses` 留痕都依赖它），
        否则会出现"守卫明明拦截了、记录却是 success"（阶段 2 首次跑等价性用例时抓到的真 bug）。
        """
        data = dict(state.get("last_error_data") or {})
        return {
            "retry_exhausted": True,
            "status": "failed",
            "error_code": error_code,
            "error_message": state.get("last_error_message"),
            "last_rule": data.get("rule_id"),
            "retries": int(retried),
            "path_switched": int(switched),
            **extra,
        }

    # 不重试类错误（E_LLM / E_OUT_OF_SCOPE / E_VALIDATION）→ 直接判失败。
    # ⚠️ 注意：**安全拦截不属于"不重试"** —— 原实现在被拦截时**仍会带着错误上下文重写一次**
    #    （只是禁止"换路"，见 `_ga` 1116 与 1142 的 `not blocked`）。这一点是等价性用例
    #    `test_parity_blocked_sql_leaves_failed_record` 抓出来的（legacy 的 `retries=1`）。
    if error_code in _NO_RETRY_CODES:
        return failed()

    digest = svc._error_digest(
        BizError(500, error_code or "E_INTERNAL", state.get("last_error_message") or "",
                 dict(state.get("last_error_data") or {}))
    )

    if not retried:
        retried = True
        if route == "sql":
            reply = await ctx.llm.chat(
                [
                    {"role": "system", "content": prompts.SQL_SYSTEM},
                    {
                        "role": "user",
                        "content": prompts.sql_rewrite_user_prompt(
                            prompt_text, schema_text, slot_json_text,
                            previous_sql=sql, previous_error=digest,
                        ),
                    },
                ],
                purpose="gen_sql",
                json_mode=True,
            )
            ctx.calls.append(("gen_sql", reply, 1))
            payload = svc._safe_json(reply.text, stage="gen_sql")
            new_sql = str(payload.get("sql") or "").strip()
            new_code = str(payload.get("code") or "").strip()
            new_route = str(payload.get("route") or "").strip().lower()
            if not blocked and new_code and (new_route == "pandas" or not new_sql):
                # 执行失败时允许模型改报 pandas 路由（换路额度同时用掉）
                return {
                    "route": "pandas", "code": new_code, "sql": None,
                    "retries": int(retried), "path_switched": 1,
                    "retry_exhausted": False, "shape_issue": None,
                    "calls": calls_to_records(ctx.calls),
                }
            if not (new_sql or sql):
                return failed(calls=calls_to_records(ctx.calls))
            return {
                "sql": new_sql or sql,
                "retries": int(retried),
                "retry_exhausted": False, "shape_issue": None,
                "calls": calls_to_records(ctx.calls),
            }

        reply = await ctx.llm.chat(
            [
                {"role": "system", "content": prompts.PANDAS_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.pandas_code_user_prompt(
                        prompt_text, schema_text, slot_json_text,
                        previous_code=code, previous_error=digest,
                    ),
                },
            ],
            purpose="gen_code",
            json_mode=True,
        )
        ctx.calls.append(("gen_code", reply, 1))
        payload = svc._safe_json(reply.text, stage="gen_code")
        new_code = str(payload.get("code") or "").strip()
        if not new_code:
            return failed(calls=calls_to_records(ctx.calls))
        return {
            "code": new_code, "sql": None,
            "retries": int(retried),
            "retry_exhausted": False, "shape_issue": None,
            "calls": calls_to_records(ctx.calls),
        }

    # 换路：仅 SQL 路径的**执行失败**（拦截不换路）
    if route == "sql" and not switched and not blocked:
        switched = True
        reply = await ctx.llm.chat(
            [
                {"role": "system", "content": prompts.PANDAS_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.pandas_code_user_prompt(
                        prompt_text, schema_text, slot_json_text, previous_error=digest
                    ),
                },
            ],
            purpose="gen_code",
            json_mode=True,
        )
        ctx.calls.append(("gen_code", reply, 1))
        payload = svc._safe_json(reply.text, stage="gen_code")
        new_code = str(payload.get("code") or "").strip()
        if not new_code:
            return failed(calls=calls_to_records(ctx.calls))
        return {
            "route": "pandas", "code": new_code, "sql": None,
            "retries": int(retried), "path_switched": int(switched),
            "retry_exhausted": False, "shape_issue": None,
            "calls": calls_to_records(ctx.calls),
        }

    # 额度用尽：如实失败（错误信息即最后一次的错误）
    return failed()


def route_after_rewrite(state: AnalysisState) -> str:
    """条件边：额度用尽 → 留痕失败；否则按 route 回到守卫（SQL）或执行（pandas）。"""
    if state.get("retry_exhausted"):
        return "persist"
    return "guard" if state.get("route") == "sql" else "execute"


def failed_state_from(route: str, code: str, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造"直接判失败"的状态增量（供测试与复用；与节点返回同形）。"""
    return {
        "route": route,
        "last_error_code": code,
        "last_error_message": message,
        "last_error_data": data or {},
    }


def result_from_state_for_test(state: AnalysisState, ctx: Any) -> Any:  # pragma: no cover - 兼容钩子
    """保留一个稳定的转换入口（等价性测试偶有需要），避免测试直接依赖 adapters 内部。"""
    return result_to_state(state_to_result(state, ctx))
