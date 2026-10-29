"""智电析报 · 统一错误体系。

决策（本次 grilling 定案）：**双轨制**
- HTTP 层用语义化状态码（400/401/403/404/409/422/429/500）
- 响应包络的 `code` = HTTP 状态码（成功为 0）
- 业务错误码放 `data.error_code`（PRD 9.3 的 `E_*` 字符串）
- `message` 给用户可读的中文说明（文案规范见 UIUX 9.4）

对应文档：PRD 9.3 错误码规范、UIUX 9.2 错误码 → 呈现映射、架构文档 4.1「后端服务层」对外契约。

映射表（PRD 9.3 需同步补入）：

| HTTP | code | error_code | 场景 |
| --- | --- | --- | --- |
| 400 | 400 | E_VALIDATION | 请求参数不合法（业务级校验，如密码不含数字） |
| 401 | 401 | E_AUTH | 未登录 / token 缺失、非法或过期 |
| 401 | 401 | E_CREDENTIALS | 用户名或密码错误（不区分账号是否存在） |
| 403 | 403 | E_FORBIDDEN | 越权访问他人资源 |
| 403 | 403 | E_ACCOUNT_DISABLED | 账号已停用 |
| 404 | 404 | E_NOT_FOUND | 资源不存在 |
| 409 | 409 | E_USERNAME_TAKEN | 用户名已被占用 |
| 413 | 413 | E_FILE_TOO_LARGE | 上传文件超过 200MB 上限 |
| 422 | 422 | E_VALIDATION | 请求体结构/类型校验失败（框架层） |
| 429 | 429 | E_AUTH_LOCKED | 账号锁定中（连续失败 5 次，锁 5 分钟） |
"""

from __future__ import annotations

from typing import Any

# ── 鉴权与通用错误码（本步落地）────────────────────────────────────────────
E_AUTH = "E_AUTH"
E_FORBIDDEN = "E_FORBIDDEN"
E_VALIDATION = "E_VALIDATION"
E_USERNAME_TAKEN = "E_USERNAME_TAKEN"
E_CREDENTIALS = "E_CREDENTIALS"
E_AUTH_LOCKED = "E_AUTH_LOCKED"
E_ACCOUNT_DISABLED = "E_ACCOUNT_DISABLED"
E_NOT_FOUND = "E_NOT_FOUND"
E_INTERNAL = "E_INTERNAL"

# ── 上传写路径错误码（本轮决策：细分 6 个新码，每码带 data.hint 修复建议）──
#   UIUX 9.4 要求错误呈现为「原因 + 下一步」两行，hint 即"下一步"字段
E_FILE_TOO_LARGE = "E_FILE_TOO_LARGE"
E_FILE_TYPE = "E_FILE_TYPE"
E_HEADER_INVALID = "E_HEADER_INVALID"
E_NO_NUMERIC_COLUMN = "E_NO_NUMERIC_COLUMN"
E_PARSE = "E_PARSE"
E_INGEST_FAILED = "E_INGEST_FAILED"

# ── 上传 / Agent 阶段错误码（PRD 9.3 原有，交付物 3–4 使用）────────────────
E_NO_TIME_COLUMN = "E_NO_TIME_COLUMN"
E_ENCODING = "E_ENCODING"
E_SQL_FORBIDDEN = "E_SQL_FORBIDDEN"
E_SQL_EXEC = "E_SQL_EXEC"
E_SANDBOX_TIMEOUT = "E_SANDBOX_TIMEOUT"
E_OUT_OF_SCOPE = "E_OUT_OF_SCOPE"
E_LLM = "E_LLM"

# ── 图表批次错误码（交付物 4 第三批：PRD F3 / 9.1 `POST /charts/rerender`）──
#   `E_CHART_UNSUPPORTED`：**显式指定**的图表类型与数据形状不符（hint 建议可选类型）
#   `E_GRANULARITY_UNAVAILABLE`：快照里没有可再聚合的明细（或请求粒度比结果更细）
#   `E_NOT_IMPLEMENTED`：能力未启用（当前仅 `GET …/chart.png` —— kaleido 属预留能力）
E_CHART_UNSUPPORTED = "E_CHART_UNSUPPORTED"
E_GRANULARITY_UNAVAILABLE = "E_GRANULARITY_UNAVAILABLE"
E_NOT_IMPLEMENTED = "E_NOT_IMPLEMENTED"

# ── 沙箱/守卫批次错误码（交付物 5：PRD F2.3 / F2.4）─────────────────────────
#   `E_CODE_FORBIDDEN`：pandas 代码未通过 AST 预检（对应 SQL 路径的 E_SQL_FORBIDDEN）
#   `E_CODE_EXEC`：沙箱内代码运行报错（错误类型 + 行号 + 堆栈精简，供 F2.4 重试）
E_CODE_FORBIDDEN = "E_CODE_FORBIDDEN"
E_CODE_EXEC = "E_CODE_EXEC"

# ── 登录失败锁定策略（PRD F6.2）─────────────────────────────────────────────
MAX_LOGIN_FAILURES = 5
LOCK_MINUTES = 5


class BizError(Exception):
    """业务异常。由 `app.main` 的异常处理器统一转换为响应包络。

    `hint` 是给用户的**可执行修复建议**（UIUX 9.4「说清原因 + 给出下一步」的第二行），
    会与错误码一起放进 `data.hint`。
    """

    def __init__(
        self,
        http_status: int,
        error_code: str,
        message: str,
        data: dict[str, Any] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        self.message = message
        self.data = dict(data or {})
        if hint:
            self.data.setdefault("hint", hint)


# ── 常用异常的便捷构造（保持调用点简洁、文案统一）──────────────────────────

def unauthorized(message: str = "登录状态已失效，请重新登录") -> BizError:
    return BizError(401, E_AUTH, message)


def credentials_invalid() -> BizError:
    return BizError(401, E_CREDENTIALS, "用户名或密码错误")


def forbidden(message: str = "无权访问该资源") -> BizError:
    return BizError(403, E_FORBIDDEN, message)


def account_disabled() -> BizError:
    return BizError(403, E_ACCOUNT_DISABLED, "账号已停用，请联系管理员")


def not_found(message: str = "资源不存在") -> BizError:
    return BizError(404, E_NOT_FOUND, message)


def username_taken() -> BizError:
    return BizError(409, E_USERNAME_TAKEN, "该用户名已被占用，请更换")


def validation_error(message: str, hint: str | None = None) -> BizError:
    """业务级参数校验失败（`hint` 可选：给用户/调用方可执行的下一步）。"""
    return BizError(400, E_VALIDATION, message, None, hint=hint)


def account_locked(remain_seconds: int) -> BizError:
    minutes = max(1, round(remain_seconds / 60))
    return BizError(
        429,
        E_AUTH_LOCKED,
        f"连续登录失败次数过多，账号已锁定，请在 {minutes} 分钟后重试",
        {"locked_seconds": remain_seconds},
    )


# ── 上传 / 写路径异常的便捷构造（均带 `hint` 修复建议）──────────────────────

def file_too_large(size_mb: float, limit_mb: int) -> BizError:
    return BizError(
        413,
        E_FILE_TOO_LARGE,
        f"文件大小 {size_mb:.1f}MB 超过 {limit_mb}MB 上限",
        {"size_mb": round(size_mb, 2), "limit_mb": limit_mb},
        hint=f"可将数据按月或按年拆分成多个文件分别上传（单文件 ≤{limit_mb}MB）",
    )


def file_type_unsupported(filename: str, allowed: str = ".csv / .txt") -> BizError:
    return BizError(
        400,
        E_FILE_TYPE,
        f"不支持的文件类型：{filename}",
        {"filename": filename, "allowed": allowed},
        hint=f"仅支持 {allowed}；Excel 文件请先另存为 CSV（UTF-8）后上传",
    )


def header_invalid(reason: str, duplicates: list[str] | None = None) -> BizError:
    data: dict[str, Any] = {}
    if duplicates:
        data["duplicate_columns"] = duplicates
    return BizError(
        400,
        E_HEADER_INVALID,
        f"表头不合法：{reason}",
        data,
        hint="请确保首行是列名、列名非空且不重复（重复的列名请加序号区分，如 用电量_1 / 用电量_2）",
    )


def no_time_column(columns: list[str] | None = None) -> BizError:
    """无时间列（PRD 9.3 原码 `E_NO_TIME_COLUMN`，此处补 `hint` 与示例表头）。"""
    data: dict[str, Any] = {}
    if columns:
        data["found_columns"] = columns[:20]
    return BizError(
        400,
        E_NO_TIME_COLUMN,
        "未识别到时间列，无法按时间轴分析",
        data,
        hint="请在表头中加入日期/时间列，例如 Date,Time,Global_active_power 或 日期,时间,用电量",
    )


def no_numeric_column(columns: list[str] | None = None) -> BizError:
    data: dict[str, Any] = {}
    if columns:
        data["found_columns"] = columns[:20]
    return BizError(
        400,
        E_NO_NUMERIC_COLUMN,
        "未找到可用于分析的数值列",
        data,
        hint="请检查用电量/功率等列是否被文本符号污染（如 '1,2' 千分位、'kWh' 单位后缀）",
    )


def parse_error(reason: str, line_no: int | None = None) -> BizError:
    data: dict[str, Any] = {}
    if line_no is not None:
        data["line_no"] = line_no
    return BizError(
        400,
        E_PARSE,
        f"文件解析失败：{reason}",
        data,
        hint="请检查是否存在行列数不齐、字段内含未转义的分隔符；可先用 Excel 打开另存为 UTF-8 CSV 再上传",
    )


def encoding_unknown(hint_text: str = "") -> BizError:
    """编码无法识别（PRD 9.3 原码 `E_ENCODING`）。"""
    return BizError(
        400,
        E_ENCODING,
        "无法识别文件编码，已尝试 UTF-8 / UTF-8-BOM / GBK",
        {},
        hint=hint_text or "请用记事本或 Excel 将文件另存为 UTF-8 编码后重试",
    )


def ingest_failed(reason: str, rolled_back: bool = True) -> BizError:
    return BizError(
        500,
        E_INGEST_FAILED,
        f"入库失败：{reason}",
        {"rolled_back": rolled_back},
        hint="该报表已标记为失败可重新上传；若反复失败请检查磁盘空间（02.data 所在分区）",
    )


def chart_unsupported(chart_type: str, supported: list[str]) -> BizError:
    """显式指定的图表类型画不出来（rerender 的 `chart_type` 是指令，不是建议）。"""
    return BizError(
        400,
        E_CHART_UNSUPPORTED,
        f"当前结果无法绘制为「{chart_type}」图",
        {"chart_type": chart_type, "supported": supported},
        hint=f"可改用：{' / '.join(supported)}（也可不传 chart_type 由后端按数据形状自动选择）",
    )


def granularity_unavailable(reason: str, *, available: str | None = None) -> BizError:
    """无法从结果快照重聚合到目标粒度（决策：惰性重聚合，不重新执行 SQL）。"""
    data: dict[str, Any] = {}
    if available:
        data["available"] = available
    return BizError(
        400,
        E_GRANULARITY_UNAVAILABLE,
        f"无法切换聚合粒度：{reason}",
        data,
        hint="该结果不含可再聚合的明细时间列（或请求的粒度比结果更细），请重新提问以获得期望粒度的数据",
    )


def not_implemented(feature: str, hint_text: str) -> BizError:
    """能力未启用（如实回报，而不是静默降级或返回假结果）。"""
    return BizError(501, E_NOT_IMPLEMENTED, f"{feature}尚未启用", {}, hint=hint_text)


def preset_not_deletable(report_code: str) -> BizError:
    """预置报表不可删除（决策：语义是"规则不允许"，故用 403 而非 400）。"""
    return BizError(
        403,
        E_FORBIDDEN,
        "系统预置报表不可删除",
        {"report_code": report_code},
        hint="预置报表为全用户共享的数据源，如需停用请在报表列表中改用其他活跃报表",
    )


def code_forbidden(reason: str, rule_id: str) -> BizError:
    """pandas 代码未通过 AST 预检（与 `E_SQL_FORBIDDEN` 对应的代码路径拦截）。"""
    return BizError(
        400,
        E_CODE_FORBIDDEN,
        f"生成的代码未通过安全校验：{reason}",
        {"rule_id": rule_id},
        hint="系统只允许对当前报表数据做只读计算（禁止文件/网络/系统调用与反射）",
    )


def code_exec(
    error_type: str, message: str, *, line_no: int | None = None, trace_tail: list[str] | None = None
) -> BizError:
    """沙箱内代码运行报错（结构化错误供 F2.4 带错误信息重写）。"""
    data: dict[str, Any] = {"error_type": error_type}
    if line_no is not None:
        data["line_no"] = line_no
    if trace_tail:
        data["trace_tail"] = trace_tail
    return BizError(
        500,
        E_CODE_EXEC,
        f"分析代码执行失败（{error_type}）：{message}",
        data,
        hint="系统会带着错误信息自动重写一次；若仍失败可调整提问方式",
    )
