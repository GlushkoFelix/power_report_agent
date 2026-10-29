"""系统类接口：健康检查、Schema 注入文本（对应 PRD 9.1）。

- `GET /system/health`：进程存活（PRD 9.1 提到的「含 LLM 连通性」待 `llm_client` 接入后补充）
- `GET /system/schema-text`：当前活跃报表的注入文本（交付物 3 落地）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import text

from app.api.v1.deps import CurrentUser, request_id
from app.core.llm_client import llm_status
from app.db.session import control_session, report_ro_conn
from app.schemas.common import ok
from app.schemas.report import SchemaTextOut
from app.services import schema_service

router = APIRouter(tags=["system"])


@router.get("/system/health", summary="健康检查（进程 + 两库 + 模型配置）")
async def health(request: Request) -> dict[str, Any]:
    """健康检查：进程存活 + 两库可达性 + **LLM 配置状态**（PRD 9.1）。

    LLM 状态是**如实回报**：未配置模型 Key（`LLM_API_KEY` / 兼容旧名 `DASHSCOPE_API_KEY`）时
    `llm.configured=false`、`impl=scripted`（此时分析链路走离线脚本模型，仍可端到端跑通，
    但结果非真实模型产出）；配置后另有 `provider`（厂商标识）、`protocol`
    （恒为 `openai-compatible`）与 `key_source`（Key 来自哪个变量，换厂商排错用）。
    """
    request_id_value = getattr(request.state, "request_id", None)
    payload: dict[str, Any] = {"status": "healthy", "llm": llm_status()}
    try:
        async with report_ro_conn() as conn:
            await conn.execute(text("SELECT 1"))
        async with control_session() as session:
            await session.execute(text("SELECT 1"))
        payload["databases"] = {"reports": "ok", "power_agent": "ok"}
    except Exception as exc:  # noqa: BLE001 —— 健康检查只回报，不抛异常
        payload["status"] = "degraded"
        payload["databases"] = {"error": str(exc)[:200]}
    return ok(payload, request_id=request_id_value)


@router.get("/system/schema-text", summary="注入用 Schema 文本（按会话取当前数据源）")
async def schema_text(
    request: Request,
    user: CurrentUser,
    conversation_code: str | None = Query(
        default=None,
        description="会话编号；传则取该会话的活跃报表，不传则回落系统默认活跃报表",
    ),
) -> dict[str, Any]:
    """当前活跃报表的 Schema 注入文本。

    取值规则（决策 2）：`conversation_code` 优先（会话级活跃报表是唯一权威），缺省回落
    `uploaded_reports.is_active = 1` 的系统默认活跃报表；响应回显 `report_code` 与 `source`，
    调用方永远知道拿到的是哪张表的口径。
    """
    async with control_session() as session:
        report, source = await schema_service.resolve_active_report(
            session, user.id, conversation_code
        )
        payload = await schema_service.ensure_injection_text(session, report)
        await session.commit()
        result = SchemaTextOut(
            report_code=report.report_code,
            table_name=report.table_name,
            filename=report.filename,
            is_preset=report.is_preset,
            caliber_version=report.caliber_version,
            source=source,
            text=payload["text"],
            text_source=payload["text_source"],
            est_tokens=payload["est_tokens"],
            truncated=payload["truncated"],
        )
    return ok(result.model_dump(), request_id=request_id(request))
