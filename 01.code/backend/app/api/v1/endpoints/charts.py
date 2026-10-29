"""图表接口（PRD 9.1 图表组，交付物 4 第三批）。

| 接口 | 说明 |
| --- | --- |
| `POST /charts/rerender` | 切换图表类型 / 聚合粒度（**不调 LLM**）并回写 `analyses.chart_json` |
| `GET /analysis/records/{record_code}/chart.png` | 服务端图片导出（**预留能力**，见下） |

`chart.png` 虽然位于记录 URL 空间（PRD 9.1 分析组），但它是**图表能力**，故与 `rerender` 同文件。

关于 `chart.png` 为什么返回 501：PRD F3.4 / 12.4 把 kaleido 服务端导出标为**预留能力**
（Word/PDF 报告属 P2），本批决策 ① 只装 `plotly`、不引入约 200MB 的 Chrome 依赖，
因此该接口**如实回报"未启用"**（`E_NOT_IMPLEMENTED` + 可执行的下一步提示），
而不是静默降级或返回一张假图；前端用 `Plotly.toImage()` 导出（UIUX 7.1：2 倍图、亮色底）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, Response

from app.api.v1.deps import CurrentUser, request_id
from app.schemas.chart import RerenderIn
from app.schemas.common import ok
from app.services import chart_service

logger = logging.getLogger("zdxb.chart")

router = APIRouter(tags=["charts"])


@router.post("/charts/rerender", summary="图表重绘（用已返回数据，不调用模型）")
async def rerender_chart(
    payload: RerenderIn, request: Request, user: CurrentUser
) -> dict[str, Any]:
    """切换图表类型 / 粒度并重绘（PRD F3.2、UIUX 7.1）。

    - **不调 LLM、不重新执行 SQL**：换类型用已返回的数据重绘；换粒度从**结果快照**重聚合；
    - 成功后把新 figure 回写该记录（决策 ⑤），使「我的分析记录 → 查看详情」与右栏一致；
    - `chart_type` 是指令：数据画不出该类型 → 400 `E_CHART_UNSUPPORTED`（含可用类型清单）；
    - 粒度不可再拆细 / 快照无明细 → 400 `E_GRANULARITY_UNAVAILABLE`；
    - 记录不存在或不属于本人 → **404**（与其余记录接口一致，不泄露资源是否存在）。
    """
    result = await chart_service.rerender_chart(
        user.id,
        payload.record_code,
        chart_type=payload.chart_type,
        granularity=payload.granularity,
        top_n=payload.top_n,
        sort=payload.sort,
    )
    return ok(result, _message(result), request_id(request))


def _message(result: dict[str, Any]) -> str:
    """回显这次到底做了什么（前端 Toast 直接可用）。"""
    if result.get("figure") is None:
        return "该结果不适合可视化，已用表格呈现"
    label = chart_service.TYPE_LABELS.get(result.get("chart_type") or "", "图表")
    if result.get("granularity"):
        return f"已按「{result['granularity']}」重新聚合并切换为{label}图"
    return f"已切换为{label}图"


@router.get("/analysis/records/{record_code}/chart.png", summary="导出图表 PNG（预留能力，当前返回 501）")
async def export_chart_png(record_code: str, request: Request, user: CurrentUser) -> Response:
    """图表图片导出（PRD 9.1；kaleido 属**预留能力**，当前返回 501）。

    归属校验与其余记录接口一致；未安装 kaleido 时返回 `E_NOT_IMPLEMENTED` + 提示，
    一旦环境里补上 kaleido（含 Chrome），本接口**无需改代码**即可真实导出。
    """
    png = await chart_service.export_chart_png(user.id, record_code)
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{record_code}.png"',
            "X-Request-ID": request_id(request),
        },
    )
