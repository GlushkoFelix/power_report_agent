"""报表相关响应模型（PRD 9.1 报表模块）。

列表契约：全量返回（不分页），不含 `schema_json` / `quality_json` 正文。
详情/预览/Schema/质量四个接口分别承担其余信息。

URL 标识统一用**业务编号** `report_code`（如 `r_daily`、`r_up_000003`）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    report_code: str
    filename: str
    report_type: str
    table_name: str
    time_start: str | None = None
    time_end: str | None = None
    row_count: int | None = None
    column_count: int | None = None
    status: str
    is_preset: int
    #: 1 = **系统默认活跃报表**（非"当前用户最近活跃"，见数据库设计文档 4.2 节说明）
    is_active: int
    created_at: str
    #: 物理表在 `reports.db` 中是否真实存在（元数据与实际不一致时暴露在接口层）
    table_exists: bool = False
    #: 是否允许删除（预置报表为 false）。前端不靠 `is_preset` 自行推断规则（决策 1）
    is_deletable: bool = True


class ReportListOut(BaseModel):
    total: int
    items: list[ReportOut]


class QualitySummary(BaseModel):
    """详情接口里的「质量摘要」（PRD 9.1：报表详情 = 元数据 + 质量摘要）。"""

    missing_rows: int | None = None
    missing_ratio: float | None = None
    time_axis_gap: int | None = None
    duplicate_timestamps: int | None = None
    voltage_out_of_range_minutes: int | None = None
    #: 与本报表相关的口径校验项及是否通过，如 {"A1_levels_equal": true}
    checks: dict[str, bool] = Field(default_factory=dict)


class ReportDetailOut(ReportOut):
    caliber_version: str | None = None
    quality_summary: QualitySummary = Field(default_factory=QualitySummary)


class PreviewColumn(BaseModel):
    """预览列信息，取自 `PRAGMA table_info`（真实表结构，不依赖 schema_json）。"""

    name: str
    type: str


class PreviewOut(BaseModel):
    report_code: str
    table_name: str
    columns: list[PreviewColumn]
    rows: list[dict[str, Any]]
    #: 取自元数据的 row_count，**不跑 COUNT(*)**（避免 207 万行扫描）
    total_rows: int | None = None
    returned_rows: int
    limit: int
    truncated: bool
    #: 排序依据列（时间列正序；未知表回退 rowid）
    order_by: str
    #: 被主动过滤的列（自增主键等代理键），保证预览列与 Schema 字典列一致
    excluded_columns: list[str] = Field(default_factory=list)


class SchemaOut(BaseModel):
    report_code: str
    table_name: str
    caliber_version: str | None = None
    #: `uploaded_reports.schema_json` 解析后的结构化描述
    structured: dict[str, Any]
    #: 与注入给模型的说明**逐字一致**的完整文本（交付物 3 起：全局头 + 本表字段 + 口径红线
    #  + SQLite 可用函数 + 常用问题示例 + 使用提示）
    text: str
    #: `schema_text.md`（预置表原文组装）或 `generated`（上传表生成，含口径未知声明）
    text_source: str
    #: 注入文本的估算 token 数（PRD F1.4 预算 ≤2000）
    est_tokens: int = 0
    #: 是否因超预算发生结构化裁剪（字段清单骨架永不裁剪）
    truncated: bool = False


class SchemaTextOut(BaseModel):
    """`GET /system/schema-text`：当前活跃报表的注入文本（Agent 前置注入用）。"""

    report_code: str
    table_name: str
    filename: str
    is_preset: int
    caliber_version: str | None = None
    #: `conversation` = 取自指定会话的活跃报表；`system_default` = 回落到系统默认活跃报表
    source: str
    text: str
    text_source: str
    est_tokens: int = 0
    truncated: bool = False


class QualityOut(BaseModel):
    report_code: str
    table_name: str
    caliber_version: str | None = None
    #: `uploaded_reports.quality_json` 解析后的内容（按表裁剪，含 dataset 与 checks 两段）
    quality: dict[str, Any]


# ── 上传写路径（PRD 9.1：upload / confirm / DELETE）─────────────────────────

class UploadColumnOut(BaseModel):
    """上传解析后的列信息（前端「字段映射」与确认页展示）。"""

    name: str                    # 安全列名（ASCII）
    origin_name: str             # 原始列名（中文原名原样保留）
    type: str
    unit: str = "—"
    #: `sum` = 可求和；`none` = 不可求和（比率列 / 维度列 / 文本列）
    agg: str = "none"
    description: str = ""
    #: 是否系统自动生成的时间戳/维度列（源表没有该列），前端字段面板可据此标注「派生」
    derived: bool = False


class UploadPreviewOut(BaseModel):
    """解析预览（PRD F1.1：列名、推断类型、前 20 行、行数）。"""

    columns: list[PreviewColumn]
    rows: list[dict[str, Any]]
    returned_rows: int
    preview_rows: int


class UploadOut(BaseModel):
    """`POST /reports/upload` 响应（两段式第一步，报表状态为 `pending`）。"""

    report_code: str
    filename: str
    table_name: str
    report_type: str = "uploaded"
    status: str
    encoding: str
    delimiter: str
    row_count: int
    column_count: int
    raw_column_count: int
    #: 识别出的时间列统一为 `dt`（PRD F1.2）
    time_column: str
    time_start: str | None = None
    time_end: str | None = None
    #: 命中 sha256 去重：本次未重复解析，直接复用已有报表（决策 5）
    is_duplicate: bool = False
    #: 原始列名 → 安全列名（仅列出发生改变的列）
    column_mapping: dict[str, str] = Field(default_factory=dict)
    columns: list[UploadColumnOut] = Field(default_factory=list)
    preview: UploadPreviewOut
    quality: dict[str, Any]
    #: `pending` 的存活截止时间（超过则被惰性清理回收，决策 6）
    pending_expires_at: str


class ConfirmOut(BaseModel):
    """`POST /reports/{report_code}/confirm` 响应（两段式第二步）。"""

    report_code: str
    table_name: str
    status: str
    row_count: int
    inserted_rows: int
    indexes: list[str] = Field(default_factory=list)
    elapsed_ms: int
    #: 中间规范化文件是否已删除
    intermediate_removed: bool = True
    #: 重复确认（已是 ready）时置 true，接口保持幂等
    already_confirmed: bool = False


class DeleteOut(BaseModel):
    """`DELETE /reports/{report_code}` 响应（预置报表返回 403）。"""

    report_code: str
    filename: str
    table_name: str | None = None
    #: 实际被 DROP 的物理表（表本就不存在时为 null，仍允许清理元数据）
    dropped_table: str | None = None
    dropped_indexes: list[str] = Field(default_factory=list)
    #: 因元数据行删除而活跃报表被置空（ON DELETE SET NULL）的会话数
    affected_conversations: int = 0
    #: 删除后回收站页数（**仅报告，不做 VACUUM**：空闲页留给后续上传复用，决策 2）
    freed_pages: int = 0
    files_removed: bool = False
