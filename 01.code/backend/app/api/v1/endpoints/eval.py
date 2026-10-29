"""评测接口（PRD 9.1 交付物 8 / M5）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/eval/questions` | 题库列表（可按题型/难度/是否陷阱筛，分页） |
| POST | `/eval/runs` | 发起评测批次（`impl=auto/real/selfcheck`） |
| GET | `/eval/runs` | 批次列表（仅本人） |
| GET | `/eval/runs/{run_code}` | 批次详情（指标 + 逐题明细） |
| GET | `/eval/runs/{run_code}/report` | 评测报告（Markdown 下载） |
| GET | `/eval/runs/{run_code}/report.pdf` | 评测报告（**PDF 下载**，v1.19.12：与 Markdown 同源渲染） |

**批次是异步的**：120 题 × 完整链路远超单次请求时限，`POST` 立即返回 `run_code`
（`status=pending/running`），前端轮询 `GET /eval/runs/{run_code}` 直到 `finished/failed`
（PRD 9.1 未定义评测进度 SSE 事件）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Response

from app.api.v1.deps import CurrentUser
from app.schemas.common import ok
from app.services import eval_service, report_pdf

router = APIRouter(tags=["eval"])


@router.get("/eval/questions", summary="题库列表（120 题全局资产）")
async def list_questions(
    user: CurrentUser,
    qtype: str | None = Query(None, description="stat/agg/compare/anomaly/structure/multi_turn"),
    difficulty: str | None = Query(None, description="easy/medium/hard"),
    is_trap: bool | None = Query(None, description="是否口径陷阱题"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """题库列表（题库为全局资产，不做用户隔离；需登录）。"""
    data = await eval_service.list_questions(
        qtype=qtype, difficulty=difficulty, is_trap=is_trap, page=page, page_size=page_size
    )
    return ok(data)


@router.post("/eval/runs", summary="发起评测批次（异步，立即返回 run_code）")
async def create_run(payload: dict[str, Any], user: CurrentUser) -> dict[str, Any]:
    """发起评测批次。

    请求体（全部可选）：`{"impl": "auto"|"real"|"selfcheck", "name": "…",
    "question_codes": ["q001", …], "limit": 20, "include_narrative": true}`。
    `impl=real` 且未配置模型 Key（`LLM_API_KEY` / 兼容旧名 `DASHSCOPE_API_KEY`）→ 400（**不静默回落**到脚本模型）。
    """
    data = await eval_service.start_run(
        user.id,
        impl=str(payload.get("impl") or "auto"),
        name=payload.get("name"),
        question_codes=payload.get("question_codes"),
        limit=payload.get("limit"),
        include_narrative=bool(payload.get("include_narrative", True)),
    )
    return ok(data, message=f"评测批次 {data['run_code']} 已创建，可轮询该批次查看进度")


@router.get("/eval/runs", summary="评测批次列表（仅本人）")
async def list_runs(
    user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """批次列表（按创建时间倒序，仅本人可见）。"""
    return ok(await eval_service.list_runs(user.id, page=page, page_size=page_size))


@router.get("/eval/runs/{run_code}", summary="批次详情（指标汇总 + 逐题明细）")
async def get_run(
    run_code: str,
    user: CurrentUser,
    include_questions: bool = Query(True, description="是否返回逐题明细"),
) -> dict[str, Any]:
    """批次详情：指标汇总 + 逐题明细（`status=running` 时明细为空，继续轮询）。"""
    return ok(await eval_service.get_run(user.id, run_code, include_questions=include_questions))


@router.get("/eval/runs/{run_code}/report", summary="下载评测报告（Markdown）")
async def get_report(run_code: str, user: CurrentUser) -> Response:
    """评测报告（Markdown）：批次未完成时 400，报告缺失时 404。"""
    filename, text = await eval_service.get_report_markdown(user.id, run_code)
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/eval/runs/{run_code}/report.pdf", summary="下载评测报告（PDF）")
async def get_report_pdf(run_code: str, user: CurrentUser) -> Response:
    """评测报告（PDF）：与 Markdown 报告**同源渲染**，直接可读可打印（用户诉求：md 要工具才排版）。

    `reportlab` 是同步 CPU 渲染 → 放线程池执行，避免阻塞事件循环（渲染几十毫秒级）。
    """
    filename, text = await eval_service.get_report_markdown(user.id, run_code)
    content = await asyncio.to_thread(report_pdf.to_pdf, text, title=f"评测报告 · {run_code}")
    pdf_name = filename[:-3] + ".pdf" if filename.endswith(".md") else f"{filename}.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{pdf_name}"'},
    )
