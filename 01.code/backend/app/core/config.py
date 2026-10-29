"""智电析报 · 全局配置与路径解析。

设计要点（对应《系统架构设计文档》8.3 可维护性 与 PRD 6.5）：
1. 所有路径以「项目根」为基准解析，**不依赖启动时的当前工作目录（CWD）**；
2. 密钥、模型名、端口、库路径全部来自 `.env`，代码内不出现硬编码密钥；
3. 口径常量（峰平谷时段 / 异常阈值 / SQL LIMIT / 执行超时）在交付物 3「Schema 模块」
   接入本文件的「口径常量区」，见 PRD 6.5 与 8.4。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── 目录基准（自本文件向上回溯，与启动目录无关）──────────────────────────
#   .../01.code/backend/app/core/config.py
#     parents[0] = core     parents[1] = app        parents[2] = backend
#     parents[3] = 01.code  parents[4] = 项目根
BACKEND_DIR = Path(__file__).resolve().parents[2]
CODE_DIR = BACKEND_DIR.parent  # 01.code
PROJECT_ROOT = CODE_DIR.parent  # 项目根（power_report_agent）
DATA_DIR = PROJECT_ROOT / "02.data"

#: `.env.example` 里的模板占位值（原样复制进 `.env` 时视为"未配置"，见 `Settings.api_key`）
_PLACEHOLDER_KEYS = frozenset({"请填入", "changeme", "todo", "placeholder", "your-api-key"})
_PLACEHOLDER_KEY_RE = re.compile(r"^(sk-)?x{4,}$", re.IGNORECASE)


class Settings(BaseSettings):
    """环境变量配置。字段名与 `.env` 中的大写变量名按大小写不敏感匹配。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 应用 ──────────────────────────────────────────────────────────
    app_name: str = "智电析报 API"
    app_version: str = "0.1.0"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    api_v1_prefix: str = "/api/v1"
    debug: bool = True

    # ── LLM：**任意 OpenAI 兼容厂商**（默认阿里云百炼）─────────────────
    #   客户端只依赖「OpenAI 兼容协议」（`POST {base_url}/chat/completions` + Bearer Key），
    #   换厂商只需改 `.env`：DeepSeek / Moonshot / 智谱 / OpenAI / 本地 vLLM·Ollama 均可。
    #   变量命名保持中性：`LLM_API_KEY` 为正式名，`DASHSCOPE_API_KEY` 作为**向后兼容别名**
    #   （历史上用过这个名字，两者取其一即可；同时填写时 `LLM_API_KEY` 优先）。
    llm_api_key: str = ""
    dashscope_api_key: str = ""
    #: 厂商标识（仅用于健康检查展示与日志；不参与协议分支——协议统一是 OpenAI 兼容）
    llm_provider_label: str = "dashscope"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model_main: str = "qwen-max"
    llm_model_light: str = "qwen-turbo"
    llm_temperature: float = 0.0
    #: **推理型模型（如 Qwen3 系列）默认输出思考过程**：实测同一请求
    #: thinking 开 = 4.0s / 195 completion tokens（其中 187 为 reasoning_tokens）；
    #: 关 = 0.9s / 5 tokens。本项目三个环节都只要结构化短输出，思考过程纯属开销，
    #: 故默认关闭。`None` = 不发送该字段（其他厂商/模型不受影响）。
    llm_enable_thinking: bool | None = None
    #: 输出上限（0 = 不发送）。兜底防止模型长篇输出拖慢链路（N2 目标 ≤8s）。
    llm_max_tokens: int = 0
    #: `json_mode` 被端点拒绝（HTTP 400 且提示含 response_format）时是否**降级重试一次**
    #: （少数自建/网关端点不认 `response_format`，降级后靠三层解析容错兜住）
    llm_json_mode_fallback: bool = True
    #: 解读（narrative）阶段超时秒数：解读是**增值项**，超时即跳过（表格与图表照给、`status` 仍 success）。
    #: 默认 8.0（v1.19.11 的取值：实测解读最长 17s，不能让"解说词"把整轮拖到十几秒）。
    #: 2026-09-22 起**可配置**：强制思考的慢模型（单次 20–30s）在 8s 下每次都被取消，
    #: 表现为评测"结论命中率"无法测量（`narrative` 不落库）、前端也没有解读文字。
    #: 调大只影响"要不要给解读"，不影响正确性；产品默认仍应保持 8s。
    narrative_timeout_seconds: float = 8.0
    #: 生成（gen_sql）阶段**软超时**秒数。生成是主链路的关键调用，正常只需 3.7–5.8s（实测），
    #: 但同一端点出现过**单次 53.9s 的长尾**（2026-10-14 实测，见问题 P-032）→
    #: 给一个宽松上限，把长尾变成"干净的失败留痕（E_LLM）"而不是让整轮干等。
    #: 与解读不同：生成不能"跳过"，所以超时按**失败**处理（如实留痕、由重试环决定下一步）。
    generate_timeout_seconds: float = 25.0
    #: **解读阶段专用模型**（空 = 回落轻量档）。为什么要单独一档（2026-10-14）：
    #: 轻量档是给 **slot 抽取**用的（PRD 10.3 模型分级）；把解读也塞进轻量档后，
    #: 换解读模型就等于换 slot 模型（实测 120 题里 slot 行为随之变化，问题定位被搅乱）。
    #: 解耦后：解读→`glm-5.3-flash`（输出节制、JSON 稳定），slot→主力/轻量照旧。
    llm_model_narrative: str = ""

    # ── 编排方式（大修：LangGraph 接入的灰度开关）─────────────────────
    #: `legacy`（默认）= services/analysis_service.run_once 的显式 async 编排；
    #: `langgraph` = app/agent/graph.py 的 StateGraph 编排。
    #: 两条路径语义等价（由 tests/test_langgraph_parity.py 逐字段保证）。
    #: **只在调用时读取**（禁止 import 期读，见 `大修过程中的问题和难点` D-003）。
    orchestrator: str = "legacy"

    # ── 规则快通道 v2（A 方案的落地开关）──────────────────────────────
    #: `True`（**默认**；2026-10-16 真模型评测确认后转正）= 分流到 `slot_rules_v2.fast_slot`
    #: （结构化 block + 分组维度抽取 + 多指标放行 + 指标词表加固），题库口径下命中轮次 **19 → 66 / 135**；
    #: `False` = `slot_service.fast_slot` 走既有规则，**可一键回退**到历史口径与历史成绩。
    #: 转正依据（`02.data/tmp/_a_report.md`；真模型 314,416 tokens、ABBA 同时段配对、`prompt-v3.10`）：
    #: 靶向组 10 题 **关 10/10 vs 开 10/10**、对照组 **关 9/10 vs 开 9/10**（逐题全同 ⇒ **零退化**）；
    #: 20 题里**省掉 10 次 slot LLM**（用无 20 s 超时的 scripted 跑交叉验证：G1 10→5、G2 8→3）；
    #: 探路题 q030 实测 **90.7 s → 19.9 s**（免去一次 LLM 调用 + 潜在的 20 s 白等）。
    #: ⚠️ 成绩引用须带"**含规则快通道 v2**"：slot 调用数变少，不可与历史读数直接混引。
    #: **只在调用时读取**（禁止 import 期读，见 `大修过程中的问题和难点` D-003）。
    slot_fastpath_v2: bool = True

    # ── 鉴权（JWT，PRD F6.2 / S4）─────────────────────────────────────
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 24

    @field_validator("jwt_secret_key")
    @classmethod
    def _require_jwt_secret(cls, value: str) -> str:
        """密钥缺失即启动报错（决策：避免每次重启换密钥导致 token 全部失效）。"""
        if not value.strip() or value.strip() == "请填入":
            raise ValueError(
                "缺少 JWT_SECRET_KEY。请生成后写入 01.code/backend/.env：\n"
                '  python -c "import secrets;print(secrets.token_urlsafe(48))"'
            )
        return value

    # ── 数据库（相对项目根；绝对路径亦可）──────────────────────────────
    reports_db_path: str = "02.data/reports.db"
    power_agent_db_path: str = "02.data/power_agent.db"

    # ── 上传写路径（PRD F1.1 / F1.3、N4）──────────────────────────────
    #   upload_dir_path：落盘根目录，按 {user_id}/{report_code}/ 隔离（数据库设计 5.8）
    upload_dir_path: str = "02.data/uploads"
    #   单文件上限（PRD F1.1：扩展名与大小校验 ≤200MB）
    upload_max_mb: int = 200
    #   待确认（pending）上传的存活时长，超时由惰性清理回收（决策 6）
    upload_pending_ttl_hours: int = 24
    #   灌数的分批提交行数：把写锁持有时间切碎，给只读查询留窗口（决策 8）
    ingest_batch_rows: int = 50_000
    #   预览返回行数（PRD F1.1：返回前 20 行）
    upload_preview_rows: int = 20

    # ── 口径常量区（交付物 3「Schema 模块」时接入，见 PRD 6.5 / 8.4）──
    #   PEAK_PERIOD / FLAT_PERIOD / VALLEY_PERIOD、ANOMALY_* 、SQL_LIMIT、EXEC_TIMEOUT
    #   暂不落地，避免与 `02.data/schema/schema_text.md` 及清洗脚本口径漂移。

    # ── 派生属性：LLM 配置解析 ─────────────────────────────────────────
    @property
    def api_key(self) -> str:
        """生效的 API Key：`LLM_API_KEY` 优先，回退旧的 `DASHSCOPE_API_KEY`。

        **模板占位值一律视为"未配置"**：使用者常把 `.env.example` 直接复制成 `.env` 而忘记替换
        `sk-xxxxxx` / `请填入`——若把它当真实 Key，每次分析都会拿假 Key 去调厂商、整条链路
        全部以 `E_LLM` 失败；判成"未配置"则自动回落离线脚本模型（链路仍可跑通、健康检查如实回报）。
        """
        value = (self.llm_api_key or self.dashscope_api_key).strip()
        if (
            not value
            or value.lower() in _PLACEHOLDER_KEYS
            or _PLACEHOLDER_KEY_RE.match(value)
        ):
            return ""
        return value

    @property
    def llm_configured(self) -> bool:
        """是否配置了真实模型（未配置时链路自动走离线脚本模型）。"""
        return bool(self.api_key)

    # ── 派生属性：统一解析为绝对路径 ───────────────────────────────────
    @staticmethod
    def _resolve(path_str: str) -> Path:
        """相对路径按项目根解析，绝对路径原样返回。"""
        path = Path(path_str)
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @property
    def reports_db(self) -> Path:
        return self._resolve(self.reports_db_path)

    @property
    def power_agent_db(self) -> Path:
        return self._resolve(self.power_agent_db_path)

    # ── 上传落盘路径 ────────────────────────────────────────────────────
    @property
    def upload_dir(self) -> Path:
        """上传文件根目录（`02.data/uploads`），下按 `{user_id}/{report_code}/` 隔离。

        `upload_files.stored_path` 存**相对本目录所属的 `02.data/` 的路径**（PRD S5：
        路径不入库明文），例如 `uploads/1/r_up_000001/report_daily.csv`。
        """
        return self._resolve(self.upload_dir_path)

    # ── 只读资产路径（离线产物，部署时需一并拷贝）───────────────────────
    @property
    def schema_text_path(self) -> Path:
        """注入用的 Schema 说明文本（`02.data/schema/schema_text.md`）。

        `GET /reports/{report_code}/schema` 的 `text` 字段即从此文件按 `### <table_name>` 切段，
        保证前端「查看完整口径」看到的文本与注入给模型的文本**逐字一致**。
        """
        return DATA_DIR / "schema" / "schema_text.md"


@lru_cache
def get_settings() -> Settings:
    """进程内单例，避免重复读取 `.env`。"""
    return Settings()
