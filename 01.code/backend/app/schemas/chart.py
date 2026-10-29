"""图表模块的请求模型（PRD 9.1 图表组；UIUX 7.1 图表与表格联动）。

`POST /charts/rerender`：切换图表类型 / 聚合粒度（**不调 LLM**，PRD F3.2）。

> 请求体是**本批新增的契约**（PRD 9.1 只给了路径与一句话说明）：记录编号放在 body 里，
> 因为该接口不属于 `/analysis/records/{record_code}` 这棵 URL 树。取值范围**不在 Pydantic 上
> 校验**，统一由 service 抛 400 `E_VALIDATION`（与 `/reports/{code}/preview`、
> `/analysis/records` 的既有约定一致，避免框架 422 与业务错误码混用）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RerenderIn(BaseModel):
    """`POST /charts/rerender` 请求体。"""

    record_code: str = Field(description="分析记录编号，如 a_000123")
    #: 显式指定即"指令"：与数据形状不符会返回 400 `E_CHART_UNSUPPORTED`（不静默兜底）
    chart_type: str | None = Field(
        default=None,
        description="line / bar / area / pie / scatter / table；不传则沿用记录里的 chart_hint 与形状兜底",
    )
    #: 从**结果快照**重聚合（不改 SQL、不调 LLM）；不可再拆细时返回 400 `E_GRANULARITY_UNAVAILABLE`
    granularity: str | None = Field(
        default=None, description="小时 / 日 / 月 / 年 / 全期（只能由细变粗）"
    )
    top_n: int | None = Field(default=None, description="分类类图的 TopN，默认 12，范围 3–50")
    sort: str | None = Field(
        default=None, description="natural / value_desc / value_asc（分类轴排序）"
    )
