"""编排分发层：按 `ORCHESTRATOR` 在「显式 async 编排」与「LangGraph 状态图」之间灰度切换。

为什么单独一个模块（而不是改 `analysis_service`）
------------------------------------------------
`analysis_service.run_once` 是 1533 行、被 303 条用例覆盖的核心链路，**大修期间一行不改**；
分发层只是"换个入口"，把调用方与两种编排解耦。切换粒度是**每次调用**（而非进程启动），
因此可以做到"同一进程内 A/B 对比"，回退只需把开关改回 `legacy`。

读取时机（硬约束，见难点 D-003）
--------------------------------
`ORCHESTRATOR` **只在调用时读取**：
1. 先看进程环境变量（便于 `uvicorn` 启动时临时指定、也便于测试注入）；
2. 再看 `.env`（经 `Settings.orchestrator`，与项目其它配置同一入口）；
3. 都没有 → `legacy`（默认，行为与改造前完全一致）。

两条路径的等价性由 `tests/test_langgraph_parity.py` 逐字段保证（含 SSE 事件序列）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.core.config import get_settings
from app.services import analysis_service, slot_service
from app.db.session import control_session

logger = logging.getLogger(__name__)

#: 环境变量名（与 `.env` 同名）
ENV_KEY = "ORCHESTRATOR"
MODE_LEGACY = "legacy"
MODE_LANGGRAPH = "langgraph"


def orchestration_mode() -> str:
    """当前编排模式（**调用时**读取；未知值一律退回 legacy 并告警）。"""
    raw = os.environ.get(ENV_KEY)
    if not raw:
        raw = getattr(get_settings(), "orchestrator", None)
    mode = (raw or MODE_LEGACY).strip().lower()
    if mode not in (MODE_LEGACY, MODE_LANGGRAPH):
        logger.warning("未知的 %s=%r，按 %s 处理", ENV_KEY, raw, MODE_LEGACY)
        return MODE_LEGACY
    return mode


async def run_once_dispatch(*args: Any, **kwargs: Any) -> analysis_service.AnalysisResult:
    """一轮问答：按开关分流（签名与 `analysis_service.run_once` 完全一致）。"""
    if orchestration_mode() == MODE_LANGGRAPH:
        from app.agent.graph import run_once_graph  # 延迟导入：legacy 模式下不加载图依赖

        return await run_once_graph(*args, **kwargs)
    return await analysis_service.run_once(*args, **kwargs)


async def run_clarify_dispatch(
    user_id: int,
    conversation_code: str,
    question: str,
    *,
    slot_patch: dict[str, Any],
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
    emit: analysis_service.EventPush | None = None,
) -> analysis_service.AnalysisResult:
    """澄清回填后继续执行（逐行对齐 `analysis_service.run_clarify`）。

    `legacy` 模式直接调用既有实现；`langgraph` 模式按同一语义（校验 → 合并 → 持久化 →
    以 `preset_slot` 继续，且 `include_user_message=False`）走图。
    """
    if orchestration_mode() != MODE_LANGGRAPH:
        return await analysis_service.run_clarify(
            user_id,
            conversation_code,
            question,
            slot_patch=slot_patch,
            ip=ip,
            user_agent=user_agent,
            request_id=request_id,
            emit=emit,
        )

    # langgraph 模式：交给 **HITL 恢复**（阶段 4）——把补丁作为 `Command(resume=...)`
    # 送进挂起的线程，从断点继续；线程没有挂起中断时，`resume_clarify_graph` 内部
    # 会自动回退为"整轮重跑"，功能不降级。
    from app.agent.graph import resume_clarify_graph  # 延迟导入（同上）

    return await resume_clarify_graph(
        user_id,
        conversation_code,
        question,
        slot_patch=slot_patch,
        ip=ip,
        user_agent=user_agent,
        request_id=request_id,
        emit=emit,
    )
